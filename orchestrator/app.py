"""
Chat orchestrator — the demo's only custom service.

For every turn:
    [1] AI Defense scan(input)       ─ block injection/PII/etc before NIM sees it
    NIM (Nemotron) → maybe tool_calls
    for each tool_call:
        [2] AI Defense scan(tool_args)  ─ block destructive verbs before execution
        NetBox MCP execute
    NIM (Nemotron) → final assistant message
    [3] AI Defense scan(output)        ─ block credential/PII leakage from NetBox

Every step streams as an SSE event so the UI can render the gate fires live.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from openai import AsyncOpenAI
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
NIM_API_KEY = os.environ["NIM_API_KEY"]
NIM_BASE_URL = os.environ.get("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")
NIM_MODEL = os.environ.get("NIM_MODEL", "nvidia/llama-3.1-nemotron-70b-instruct")
AI_DEFENSE_MCP_URL = os.environ.get("AI_DEFENSE_MCP_URL", "http://ai-defense-mcp:8200")
NETBOX_MCP_URL = os.environ.get("NETBOX_MCP_URL", "http://netbox-mcp:8210")

GATE_INPUT = os.environ.get("GATE_INPUT_ENABLED", "true").lower() == "true"
GATE_TOOL_ARGS = os.environ.get("GATE_TOOL_ARGS_ENABLED", "true").lower() == "true"
GATE_OUTPUT = os.environ.get("GATE_OUTPUT_ENABLED", "true").lower() == "true"

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s orch %(message)s")
log = logging.getLogger(__name__)

SYSTEM_PROMPT = """detailed thinking off

You are NetOps Assistant, a chatbot for a network operations team that uses NetBox as the source of truth for infrastructure.

You have access to NetBox tools for querying devices, sites, IP prefixes, circuits, and contacts.

CRITICAL VOCABULARY (NetBox-specific):
- "role" = the device's functional role: ONLY 'core', 'distribution', 'access', 'edge', 'wireless'.
- "switches", "routers", "firewalls" are device TYPES, not roles. Do NOT pass them as the `role` parameter.
- To answer "show me switches at site X" → call netbox_list_devices(site='x') with NO role filter; every returned device is a network device.
- Available site slugs: dc-1-atl, dc-2-rtp, branch-sjc, branch-ams.

When asked about infrastructure:
- ALWAYS use a tool rather than guessing
- NEVER fabricate device names, IPs, models, or contact info
- If a tool returns an error with a hint, READ the hint and adjust your call — don't blindly retry with different invalid values.
- If the tools return nothing, say "I couldn't find that in NetBox" — do not invent.

You are speaking to authenticated network engineers. Be concise and factual. Render tables in Markdown when results have multiple rows. Refuse destructive actions unless the user explicitly confirms.
"""

# ----------------------------------------------------------------------------
# App + clients
# ----------------------------------------------------------------------------
app = FastAPI(title="NetBox+AIDefense Demo Orchestrator", version="1.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

nim_client = AsyncOpenAI(api_key=NIM_API_KEY, base_url=NIM_BASE_URL)
http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))

TOOLS_CACHE: list[dict[str, Any]] = []


async def load_tools() -> list[dict[str, Any]]:
    global TOOLS_CACHE
    if TOOLS_CACHE:
        return TOOLS_CACHE
    r = await http.get(f"{NETBOX_MCP_URL}/tools")
    r.raise_for_status()
    TOOLS_CACHE = r.json()["tools"]
    log.info(f"Loaded {len(TOOLS_CACHE)} NetBox tools")
    return TOOLS_CACHE


async def ai_defense_scan(content: str, where: str) -> dict[str, Any]:
    """Returns the scan result dict, or a synthetic 'allow' if the gate is disabled."""
    if (where == "input" and not GATE_INPUT) or \
       (where == "tool_args" and not GATE_TOOL_ARGS) or \
       (where == "output" and not GATE_OUTPUT):
        return {"action": "allow", "latency_ms": 0, "message": "gate disabled"}
    r = await http.post(f"{AI_DEFENSE_MCP_URL}/scan/{where}", json={"content": content})
    r.raise_for_status()
    return r.json()


async def netbox_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    r = await http.post(f"{NETBOX_MCP_URL}/call", json={"name": name, "arguments": arguments})
    if r.status_code != 200:
        return {"error": f"netbox-mcp returned {r.status_code}", "detail": r.text[:300]}
    return r.json().get("result", {})


def sse(event: str, data: dict[str, Any]) -> dict[str, str]:
    return {"event": event, "data": json.dumps(data)}


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
import time
BUILD_ID = str(int(time.time()))  # cache-bust static assets on each container restart


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "model": NIM_MODEL, "build_id": BUILD_ID})


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


class ChatRequest(BaseModel):
    message: str
    history: list[dict[str, Any]] = []
    session_id: str | None = None


@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    """SSE-streamed chat turn. Every gate, tool call, and synthesis step emits an event."""
    session_id = req.session_id or str(uuid.uuid4())
    history = req.history or []

    async def generator() -> AsyncIterator[dict[str, str]]:
        try:
            yield sse("turn_start", {"session_id": session_id, "model": NIM_MODEL})

            # ---- GATE 1: input ----
            yield sse("gate_start", {"where": "input", "n": 1, "content": req.message})
            in_scan = await ai_defense_scan(req.message, "input")
            yield sse("gate_result", {"where": "input", "n": 1, **in_scan})
            if in_scan.get("action") == "block":
                yield sse("blocked", {"where": "input", **in_scan})
                yield sse("turn_end", {"reason": "input_blocked"})
                return

            # ---- Build conversation, call NIM with tools ----
            tools = await load_tools()
            messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}, *history, {"role": "user", "content": req.message}]

            for hop in range(5):  # cap tool-loop depth
                yield sse("llm_call_start", {"hop": hop, "model": NIM_MODEL})

                # One transparent retry on empty completions — free-tier NIM
                # occasionally returns content='' AND tool_calls=[] without erroring.
                msg = None
                for attempt in range(2):
                    completion = await nim_client.chat.completions.create(
                        model=NIM_MODEL,
                        messages=messages,
                        tools=tools,
                        tool_choice="auto",
                        temperature=0.2,
                        max_tokens=1024,
                    )
                    choice = completion.choices[0]
                    msg = choice.message
                    log.info(
                        f"NIM hop={hop} attempt={attempt} finish={choice.finish_reason} "
                        f"tool_calls={len(msg.tool_calls) if msg.tool_calls else 0} "
                        f"content_len={len(msg.content or '')} "
                        f"usage={getattr(completion, 'usage', None)}"
                    )
                    if msg.tool_calls or (msg.content and msg.content.strip()):
                        break
                    log.warning(f"Empty completion (attempt {attempt+1}), retrying...")
                # Append assistant message (may include tool_calls)
                assistant_msg: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
                if msg.tool_calls:
                    assistant_msg["tool_calls"] = [
                        {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in msg.tool_calls
                    ]
                messages.append(assistant_msg)

                if not msg.tool_calls:
                    # Final answer ready
                    final = (msg.content or "").strip()
                    if not final:
                        # Super-class Nemotron with detailed-thinking-off occasionally returns
                        # nothing at all. Surface as an error rather than display blank.
                        log.warning(f"Empty completion from {NIM_MODEL} (hop={hop}). Suggesting retry.")
                        yield sse("error", {"message": f"Model {NIM_MODEL} returned an empty response. This happens occasionally with the Super-class model — please retry or rephrase."})
                        yield sse("turn_end", {"reason": "empty_response"})
                        return

                    # ---- GATE 3: output ----
                    yield sse("gate_start", {"where": "output", "n": 1, "content": final[:200]})
                    out_scan = await ai_defense_scan(final, "output")
                    yield sse("gate_result", {"where": "output", "n": 1, **out_scan})
                    if out_scan.get("action") == "block":
                        yield sse("blocked", {"where": "output", **out_scan})
                        yield sse("turn_end", {"reason": "output_blocked"})
                        return

                    yield sse("assistant_message", {"content": final})
                    yield sse("turn_end", {"reason": "ok", "hops": hop + 1})
                    return

                # ---- GATE 2: tool args (each tool call) ----
                for idx, tc in enumerate(msg.tool_calls):
                    fn_name = tc.function.name
                    raw_args = tc.function.arguments or "{}"
                    scan_text = f"Call to {fn_name} with arguments: {raw_args}"

                    yield sse("tool_call_proposed", {"hop": hop, "idx": idx, "name": fn_name, "arguments": raw_args})

                    yield sse("gate_start", {"where": "tool_args", "n": 2, "content": scan_text})
                    arg_scan = await ai_defense_scan(scan_text, "tool_args")
                    yield sse("gate_result", {"where": "tool_args", "n": 2, **arg_scan})
                    if arg_scan.get("action") == "block":
                        yield sse("blocked", {"where": "tool_args", "tool": fn_name, **arg_scan})
                        # Feed a refusal back as the tool result so the conversation can recover
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps({"error": "blocked_by_ai_defense", "category": arg_scan.get("category"), "subcategory": arg_scan.get("subcategory")}),
                        })
                        continue

                    # Execute
                    try:
                        args = json.loads(raw_args) if raw_args else {}
                    except json.JSONDecodeError:
                        args = {}
                    yield sse("tool_executing", {"hop": hop, "idx": idx, "name": fn_name})
                    tool_result = await netbox_call(fn_name, args)
                    yield sse("tool_result", {"hop": hop, "idx": idx, "name": fn_name, "result": tool_result})

                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(tool_result)[:4000]})

            # Tool-loop cap hit
            yield sse("error", {"message": "Tool-loop depth exceeded (5 hops)"})
            yield sse("turn_end", {"reason": "tool_loop_exceeded"})

        except Exception as e:
            log.exception("chat turn crashed")
            yield sse("error", {"message": str(e)})
            yield sse("turn_end", {"reason": "error"})

    return EventSourceResponse(generator())
