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

CRITICAL TOOL-SELECTION RULES:
- INVENTORY / COUNT questions ("how many X", "show me all X", "list X"): use `netbox_list_devices` with a role filter. NEVER use `netbox_search` for inventory.
- `netbox_search` is for SPECIFIC named things only — "tell me about device atl-core-01", "find prefix 10.10.0.0/16".

CRITICAL VOCABULARY (NetBox-specific):
- "role" = the device's functional role: ONLY 'core', 'distribution', 'access', 'edge', 'wireless', 'spine', 'leaf', 'firewall', 'server'.
- "switches" and "routers" are device TYPES, not roles. Do NOT pass them as the `role` parameter.
- "firewalls" → role='firewall'. "APs" / "wireless" → role='wireless'. "edge routers" / "SD-WAN" → role='edge'.
- "Nexus" / "Catalyst" / "Meraki" / "UCS" / "ASR" / "Secure Firewall" are MODEL families — use `netbox_list_devices(model_contains="Nexus")` etc. Do NOT iterate sites manually.
- Available site slugs: dc-1-atl (Atlanta DC), dc-2-rtp (RTP DR), branch-nyc (NYC HQ), branch-sfo (San Francisco), branch-sjc (San Jose), branch-ams (Amsterdam).

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


async def ai_defense_scan(content: str, where: str,
                          enabled_rules: list[str] | None = None) -> dict[str, Any]:
    """Returns the scan result dict, or a synthetic 'allow' if the gate is disabled.
    `enabled_rules` overrides the AI Defense connection's default policy per call —
    useful for demo-time toggling of which rules apply."""
    if (where == "input" and not GATE_INPUT) or \
       (where == "tool_args" and not GATE_TOOL_ARGS) or \
       (where == "output" and not GATE_OUTPUT):
        return {"is_safe": True, "action": "allow", "latency_ms": 0,
                "message": "gate disabled", "violations": [],
                "severity": None, "attack_technique": None}
    body: dict[str, Any] = {"content": content}
    if enabled_rules is not None:
        body["enabled_rules"] = enabled_rules
    r = await http.post(f"{AI_DEFENSE_MCP_URL}/scan/{where}", json=body)
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


@app.get("/about", response_class=HTMLResponse)
async def about(request: Request):
    return templates.TemplateResponse("about.html", {"request": request, "model": NIM_MODEL, "build_id": BUILD_ID})


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# ─── Tetragon eBPF event stream ─────────────────────────────────────────
# Tails the JSONL log Tetragon writes to /var/log/tetragon/tetragon.log
# (mounted from the tetragon_logs Docker volume, read-only) and streams
# filtered events to the UI via SSE.
#
# We surface only events relevant to the demo:
#   - process_kprobe with function_name=tcp_connect  (outbound network)
#   - process_exec inside our demo containers        (process start)
# Everything else (OS housekeeping, motd scripts, etc) is dropped.
TETRAGON_LOG = os.environ.get("TETRAGON_LOG", "/var/log/tetragon/tetragon.log")
DEMO_CONTAINER_PREFIXES = ("aidefense-demo-",)


import re as _re

# Patterns that look like secret-in-args (redis -a, postgres password env, bearer tokens, etc.).
# Tetragon faithfully captures process args, which can include secrets — redact defensively.
_SECRET_PATTERNS = [
    (_re.compile(r'(-a\s+)\S+'),              r'\1***'),   # redis-cli -a <pw>
    (_re.compile(r'(--password[=\s]+)\S+'),   r'\1***'),
    (_re.compile(r'(PGPASSWORD=)\S+'),        r'\1***'),
    (_re.compile(r'(Bearer\s+)\S+'),          r'\1***'),
    (_re.compile(r'(Token\s+)[A-Fa-f0-9]{20,}'), r'\1***'),
    (_re.compile(r'(nvapi-)\S+'),             r'\1***'),
    (_re.compile(r'(api[_-]?key[=\s]+)\S+', _re.I), r'\1***'),
]

def _redact(s: str) -> str:
    for pat, repl in _SECRET_PATTERNS:
        s = pat.sub(repl, s)
    return s


# Healthcheck / sidecar noise we don't want in the demo UI
_NOISE_BINARIES = {
    "runc", "containerd-shim", "ps", "init", "tetragon",
    "redis-cli", "pg_isready", "wget", "curl",       # healthchecks
    "sh", "bash", "grep", "stat", "cat", "id", "awk", "sort", "tail",  # sub-shell noise
    "date", "egrep", "update-motd-fsck-at-reboot", "update-motd-reboot-required",
    "97-overlayroot", "98-fsck-at-reboot", "98-reboot-required",
    "run-parts",
}

# Loopback / sidecar TCP traffic — drop. We only care about external + intra-stack hops.
_NOISE_DEST_PATTERNS = (
    "127.0.0.1:",  # loopback
)


def _tetragon_event_is_interesting(ev: dict[str, Any]) -> dict[str, Any] | None:
    """Filter + flatten + redact a raw Tetragon event into a UI-friendly dict.
    Returns None to drop the event."""
    if "process_kprobe" in ev:
        kp = ev["process_kprobe"]
        if kp.get("function_name") != "tcp_connect":
            return None
        proc = kp.get("process") or {}
        binary = (proc.get("binary") or "").split("/")[-1] or "?"
        if binary in _NOISE_BINARIES:
            return None
        # Extract the destination from args (the sock struct)
        dest = None
        for arg in kp.get("args") or []:
            sock = arg.get("sock_arg")
            if sock:
                dest = f"{sock.get('daddr', '?')}:{sock.get('dport', '?')}"
                break
        if dest and any(dest.startswith(p) for p in _NOISE_DEST_PATTERNS):
            return None
        return {
            "kind": "tcp_connect",
            "time": ev.get("time"),
            "binary": binary,
            "pid": proc.get("pid"),
            "container": (proc.get("pod") or {}).get("name") or proc.get("docker", "")[:12] or "host",
            "dest": dest,
        }
    if "process_exec" in ev:
        pe = ev["process_exec"]
        proc = pe.get("process") or {}
        cid = proc.get("docker", "")
        # Only surface exec events from our demo containers
        if not cid:
            return None
        binary = (proc.get("binary") or "").split("/")[-1] or "?"
        if binary in _NOISE_BINARIES:
            return None
        return {
            "kind": "process_exec",
            "time": ev.get("time"),
            "binary": binary,
            "pid": proc.get("pid"),
            "container": cid[:12],
            "args": _redact((proc.get("arguments") or ""))[:120],
        }
    return None


@app.get("/tetragon/stream")
async def tetragon_stream(request: Request):
    """SSE stream of filtered Tetragon events. Opens the JSONL log, seeks
    to the end, and tails new lines as Tetragon writes them."""
    import asyncio

    async def gen():
        # If Tetragon isn't running yet, surface a placeholder event so the UI shows status.
        if not os.path.exists(TETRAGON_LOG):
            yield sse("tetragon_unavailable", {"log": TETRAGON_LOG, "message": "Tetragon log not mounted. Is the tetragon container running?"})
            return
        yield sse("tetragon_ready", {"log": TETRAGON_LOG})
        # Open and seek to end (only tail new events)
        f = open(TETRAGON_LOG, "r")
        try:
            f.seek(0, 2)  # SEEK_END
            buf = ""
            while not await request.is_disconnected():
                chunk = f.read(8192)
                if not chunk:
                    await asyncio.sleep(0.3)
                    continue
                buf += chunk
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    out = _tetragon_event_is_interesting(ev)
                    if out:
                        yield sse("tetragon_event", out)
        finally:
            f.close()

    return EventSourceResponse(gen())


class ChatRequest(BaseModel):
    message: str
    history: list[dict[str, Any]] = []
    session_id: str | None = None
    enabled_rules: list[str] | None = None  # per-turn override of AI Defense policy rules


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
            in_scan = await ai_defense_scan(req.message, "input", req.enabled_rules)
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
                finish_reason = None
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
                    finish_reason = choice.finish_reason
                    log.info(
                        f"NIM hop={hop} attempt={attempt} finish={finish_reason} "
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
                        # Empty content + no tool_calls + finish_reason=stop = the model
                        # silently refused. This is Nemotron's safety training firing
                        # ABOVE AI Defense — a defense-in-depth win, not an error.
                        # (Verified pattern: completion_tokens=11, finish=stop, no
                        # refusal field. Direct probe on cred-exfil prompts reproduces.)
                        if finish_reason == "stop":
                            log.info(f"Model declined hop={hop} ({finish_reason}, 0 content, 0 tool_calls) — "
                                     f"Nemotron safety training refused.")
                            yield sse("model_declined", {
                                "model": NIM_MODEL,
                                "finish_reason": finish_reason,
                                "explanation": (
                                    "Nemotron's safety training refused this request before "
                                    "calling any tool. This is defense-in-depth: even when "
                                    "Cisco AI Defense's input policy doesn't categorize a "
                                    "prompt as a content-safety violation (e.g. credential "
                                    "exfiltration), the model's own training provides a "
                                    "second layer."
                                ),
                            })
                            yield sse("turn_end", {"reason": "model_declined"})
                            return
                        # Other empty-cases (very rare with retry guard) — surface as error.
                        log.warning(f"Empty completion from {NIM_MODEL} (hop={hop}, finish={finish_reason}) — non-stop, treating as error.")
                        yield sse("error", {"message": f"Model {NIM_MODEL} returned an empty response (finish_reason={finish_reason}). Please retry or rephrase."})
                        yield sse("turn_end", {"reason": "empty_response"})
                        return

                    # ---- GATE 3: output ----
                    yield sse("gate_start", {"where": "output", "n": 1, "content": final[:200]})
                    out_scan = await ai_defense_scan(final, "output", req.enabled_rules)
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
                    arg_scan = await ai_defense_scan(scan_text, "tool_args", req.enabled_rules)
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
