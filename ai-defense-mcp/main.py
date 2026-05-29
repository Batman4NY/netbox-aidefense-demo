"""
Cisco AI Defense MCP-style wrapper.

Three endpoints fired by the orchestrator at three points in every chat turn:
  POST /scan/input       — user message before LLM
  POST /scan/tool_args   — tool-call args before execution
  POST /scan/output      — assistant response before display

Underlying API:
  POST {base_url}/api/v1/inspect/chat
  Auth header: X-Cisco-AI-Defense-API-Key: <inspection-api-key>
  Body: {"messages": [{"role": "user"|"assistant", "content": "..."}], "model": "<label>"}
  Response:
    {"action": "allow"|"block",
     "category": "pii|injection|toxicity|harmful|...",
     "subcategory": "ssn|credit_card|...",
     "message": "...",
     "request_id": "uuid"}

Latency budget: 140-320ms per call (per AI Defense docs).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s ai-defense-mcp %(message)s")
log = logging.getLogger(__name__)

API_KEY = os.environ["AI_DEFENSE_API_KEY"]
BASE_URL = os.environ.get("AI_DEFENSE_BASE_URL", "https://us.api.inspect.aidefense.security.cisco.com")
MODEL_LABEL = os.environ.get("AI_DEFENSE_MODEL_LABEL", "aidefense-demo")
INSPECT_URL = f"{BASE_URL}/api/v1/inspect/chat"

app = FastAPI(title="Cisco AI Defense MCP", version="1.0")
client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))


class ScanRequest(BaseModel):
    content: str = Field(..., description="Text to inspect")
    role: Literal["user", "assistant"] = Field("user", description="user→input policy (13 rules); assistant→output policy (2 rules)")
    source_ip: str | None = None
    user_id: str | None = None


class ScanResult(BaseModel):
    action: Literal["allow", "block"]
    category: str | None = None
    subcategory: str | None = None
    message: str | None = None
    request_id: str | None = None
    latency_ms: int
    raw: dict[str, Any] = Field(default_factory=dict)


async def _inspect(content: str, role: str, source_ip: str | None, user_id: str | None) -> ScanResult:
    if not content or not content.strip():
        return ScanResult(action="allow", message="empty content, skipped", latency_ms=0)

    payload: dict[str, Any] = {
        "messages": [{"role": role, "content": content}],
        "model": MODEL_LABEL,
    }
    if source_ip:
        payload["source_ip"] = source_ip
    if user_id:
        payload["user_id"] = user_id

    import time
    t0 = time.perf_counter()
    try:
        r = await client.post(
            INSPECT_URL,
            json=payload,
            headers={"X-Cisco-AI-Defense-API-Key": API_KEY, "Content-Type": "application/json"},
        )
    except httpx.RequestError as e:
        log.error(f"AI Defense API request failed: {e}")
        raise HTTPException(status_code=502, detail=f"AI Defense API unreachable: {e}")
    latency_ms = int((time.perf_counter() - t0) * 1000)

    if r.status_code != 200:
        log.warning(f"AI Defense returned {r.status_code}: {r.text[:200]}")
        raise HTTPException(status_code=502, detail=f"AI Defense API status {r.status_code}: {r.text[:200]}")

    data = r.json()
    # API returns: action ("Allow"|"Block"), is_safe, severity, classifications[],
    # attack_technique, detected_pii[], processed_rules[], explanation.
    action_raw = (data.get("action") or "allow").lower()
    classifications = data.get("classifications") or []
    triggered = [pr for pr in (data.get("processed_rules") or []) if pr.get("classification") != "NONE_VIOLATION"]
    attack = data.get("attack_technique") or ""
    pii = data.get("detected_pii") or []

    # Category: prefer first classification, fall back to triggered rule name, then attack technique
    category = None
    if classifications:
        category = str(classifications[0]).lower().replace(" ", "_")
    elif triggered:
        category = triggered[0].get("rule_name", "").lower().replace(" ", "_") or None
    elif attack and attack != "NONE_ATTACK_TECHNIQUE":
        category = attack.lower()

    # Subcategory: first detected PII type, OR first triggered rule's entity type
    subcategory = None
    if pii and isinstance(pii[0], dict):
        subcategory = pii[0].get("type") or pii[0].get("entity_type")
    elif pii:
        subcategory = str(pii[0])
    elif triggered and triggered[0].get("entity_types"):
        subcategory = triggered[0]["entity_types"][0]

    return ScanResult(
        action=action_raw,
        category=category,
        subcategory=subcategory,
        message=data.get("explanation") or data.get("message"),
        request_id=data.get("event_id") or data.get("client_transaction_id"),
        latency_ms=latency_ms,
        raw=data,
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "ai-defense-mcp"}


@app.post("/scan/input", response_model=ScanResult)
async def scan_input(req: ScanRequest) -> ScanResult:
    """Scan a user message BEFORE it reaches the LLM. Fires input policy (13 rules)."""
    return await _inspect(req.content, role="user", source_ip=req.source_ip, user_id=req.user_id)


@app.post("/scan/tool_args", response_model=ScanResult)
async def scan_tool_args(req: ScanRequest) -> ScanResult:
    """Scan a tool call's JSON arguments BEFORE it executes against NetBox.
    Treated as user-role so the full 13-rule input policy applies — catches destructive verbs."""
    return await _inspect(req.content, role="user", source_ip=req.source_ip, user_id=req.user_id)


@app.post("/scan/output", response_model=ScanResult)
async def scan_output(req: ScanRequest) -> ScanResult:
    """Scan an assistant message BEFORE display. Deliberately uses role='user' so
    the FULL 13-rule input policy fires on outbound content (defense-in-depth).
    The default assistant/output policy only enables 2 rules with PII allowed —
    inadequate for protecting NetBox-sourced credentials/contact PII."""
    return await _inspect(req.content, role="user", source_ip=req.source_ip, user_id=req.user_id)


@app.post("/scan", response_model=ScanResult)
async def scan_generic(req: ScanRequest) -> ScanResult:
    """Generic scan with explicit role. Used for testing and flexibility."""
    return await _inspect(req.content, role=req.role, source_ip=req.source_ip, user_id=req.user_id)
