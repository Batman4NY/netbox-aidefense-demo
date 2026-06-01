"""
Cisco AI Defense MCP-style wrapper.

Fires at three points in every chat turn:
  POST /scan/input       — user message before LLM
  POST /scan/tool_args   — tool-call args before execution
  POST /scan/output      — assistant response before display (escalated to role=user
                            so the full 13-rule input policy applies on outbound data)

Response model (richer than the demo's first cut — backported from cognisphere):
  - is_safe: bool                    primary signal (cleaner than mapping Allow|Block)
  - action: "allow" | "block"        derived from is_safe (kept for backward compat)
  - severity: str | None             HIGH | MEDIUM | LOW (NONE_SEVERITY dropped)
  - attack_technique: str | None     e.g. INSTRUCTION_OVERRIDE, JAILBREAK
  - violations: list[Violation]      ALL triggered rules, not just the first
  - category, subcategory            convenience fields from first violation
  - latency_ms, request_id, raw

Config:
  AI_DEFENSE_API_KEY           inspection API key
  AI_DEFENSE_REGION            us | eu | ap | uae   (default: us)
  AI_DEFENSE_BASE_URL          override the region-derived URL entirely
  AI_DEFENSE_MODE              api (default) | gateway
  AI_DEFENSE_GATEWAY_URL       required when mode=gateway — custom egress proxy URL
  AI_DEFENSE_MODEL_LABEL       arbitrary string sent as `model` in payload
  AI_DEFENSE_DEFAULT_RULES     comma-sep list of rule names to enable per call (empty = policy default)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s ai-defense-mcp %(message)s")
log = logging.getLogger(__name__)

API_KEY = os.environ["AI_DEFENSE_API_KEY"]
REGION = os.environ.get("AI_DEFENSE_REGION", "us").lower()
MODE = os.environ.get("AI_DEFENSE_MODE", "api").lower()
GATEWAY_URL = os.environ.get("AI_DEFENSE_GATEWAY_URL", "").rstrip("/")
MODEL_LABEL = os.environ.get("AI_DEFENSE_MODEL_LABEL", "aidefense-demo")
DEFAULT_RULES = [r.strip() for r in os.environ.get("AI_DEFENSE_DEFAULT_RULES", "").split(",") if r.strip()]

# Multi-region URL map (backported from cognisphere)
_REGION_MAP = {
    "us":  "https://us.api.inspect.aidefense.security.cisco.com",
    "eu":  "https://eu.api.inspect.aidefense.security.cisco.com",
    "ap":  "https://ap.api.inspect.aidefense.security.cisco.com",
    "uae": "https://uae.api.inspect.aidefense.security.cisco.com",
}
_BASE_URL_OVERRIDE = os.environ.get("AI_DEFENSE_BASE_URL", "").rstrip("/")

if MODE == "gateway":
    if not GATEWAY_URL:
        raise RuntimeError("AI_DEFENSE_MODE=gateway requires AI_DEFENSE_GATEWAY_URL")
    BASE_URL = GATEWAY_URL
elif _BASE_URL_OVERRIDE:
    BASE_URL = _BASE_URL_OVERRIDE
else:
    if REGION not in _REGION_MAP:
        raise RuntimeError(f"Unknown AI_DEFENSE_REGION={REGION!r}, expected one of {list(_REGION_MAP)}")
    BASE_URL = _REGION_MAP[REGION]

INSPECT_URL = f"{BASE_URL}/api/v1/inspect/chat"

log.info(f"AI Defense MCP starting — mode={MODE} region={REGION} base={BASE_URL} default_rules={DEFAULT_RULES or 'policy-default'}")

app = FastAPI(title="Cisco AI Defense MCP", version="2.0")
client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))


class Violation(BaseModel):
    classification: str
    rule_name: str
    entity_types: list[str] = Field(default_factory=list)


class ScanRequest(BaseModel):
    content: str = Field(..., description="Text to inspect")
    role: Literal["user", "assistant"] = Field("user")
    source_ip: str | None = None
    user_id: str | None = None
    enabled_rules: list[str] | None = None  # per-call override of policy rules


class ScanResult(BaseModel):
    is_safe: bool
    action: Literal["allow", "block"]
    severity: str | None = None
    attack_technique: str | None = None
    violations: list[Violation] = Field(default_factory=list)

    # Convenience fields derived from violations[0] (for simple UI rendering)
    category: str | None = None
    subcategory: str | None = None

    message: str | None = None
    request_id: str | None = None
    latency_ms: int
    raw: dict[str, Any] = Field(default_factory=dict)


def _extract_violations(data: dict[str, Any]) -> list[Violation]:
    """Extract every triggered rule, not just the first.
    Pattern backported from cognisphere's processInspectionResults()."""
    out: list[Violation] = []
    for r in (data.get("processed_rules") or []):
        cls = r.get("classification")
        if not cls or cls == "NONE_VIOLATION":
            continue
        out.append(Violation(
            classification=cls,
            rule_name=r.get("rule_name", "unknown"),
            entity_types=[e for e in (r.get("entity_types") or []) if e],
        ))
    return out


async def _inspect(content: str, role: str, source_ip: str | None,
                   user_id: str | None, enabled_rules: list[str] | None) -> ScanResult:
    if not content or not content.strip():
        return ScanResult(is_safe=True, action="allow", message="empty content, skipped", latency_ms=0)

    payload: dict[str, Any] = {
        "messages": [{"role": role, "content": content}],
        "model": MODEL_LABEL,
        "config": {"enabled_rules": enabled_rules if enabled_rules is not None else DEFAULT_RULES},
        "metadata": {},
    }
    if source_ip: payload["source_ip"] = source_ip
    if user_id:   payload["user_id"] = user_id

    t0 = time.perf_counter()
    try:
        r = await client.post(
            INSPECT_URL,
            json=payload,
            headers={"X-Cisco-AI-Defense-API-Key": API_KEY,
                     "Content-Type": "application/json",
                     "Accept": "application/json"},
        )
    except httpx.RequestError as e:
        log.error(f"AI Defense API unreachable: {e}")
        raise HTTPException(status_code=502, detail=f"AI Defense API unreachable: {e}")
    latency_ms = int((time.perf_counter() - t0) * 1000)

    if r.status_code != 200:
        log.warning(f"AI Defense returned {r.status_code}: {r.text[:200]}")
        raise HTTPException(status_code=502, detail=f"AI Defense API status {r.status_code}: {r.text[:200]}")

    data = r.json()

    # is_safe is the primary signal — cleaner than mapping action: "Allow"|"Block"
    is_safe = bool(data.get("is_safe", True))

    violations = _extract_violations(data)
    severity = data.get("severity") if data.get("severity") not in (None, "NONE_SEVERITY") else None
    attack = data.get("attack_technique") if data.get("attack_technique") not in (None, "NONE_ATTACK_TECHNIQUE") else None

    # Backward-compat convenience fields
    category = None
    subcategory = None
    if violations:
        v = violations[0]
        category = v.rule_name.lower().replace(" ", "_") or None
        subcategory = v.entity_types[0] if v.entity_types else None

    return ScanResult(
        is_safe=is_safe,
        action="allow" if is_safe else "block",
        severity=severity,
        attack_technique=attack,
        violations=violations,
        category=category,
        subcategory=subcategory,
        message=data.get("explanation") or data.get("message"),
        request_id=data.get("event_id") or data.get("client_transaction_id"),
        latency_ms=latency_ms,
        raw=data,
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "ai-defense-mcp", "mode": MODE, "region": REGION, "base_url": BASE_URL}


@app.post("/scan/input", response_model=ScanResult)
async def scan_input(req: ScanRequest) -> ScanResult:
    """Scan a user message BEFORE it reaches the LLM. Fires input policy (13 rules)."""
    return await _inspect(req.content, role="user",
                          source_ip=req.source_ip, user_id=req.user_id,
                          enabled_rules=req.enabled_rules)


@app.post("/scan/tool_args", response_model=ScanResult)
async def scan_tool_args(req: ScanRequest) -> ScanResult:
    """Scan a tool call's JSON arguments BEFORE it executes against NetBox.
    role=user so the full 13-rule input policy applies — catches destructive verbs."""
    return await _inspect(req.content, role="user",
                          source_ip=req.source_ip, user_id=req.user_id,
                          enabled_rules=req.enabled_rules)


@app.post("/scan/output", response_model=ScanResult)
async def scan_output(req: ScanRequest) -> ScanResult:
    """Scan an assistant message BEFORE display.
    Uses role='user' so the FULL 13-rule input policy fires on outbound content
    (defense-in-depth — default assistant/output policy enables only 2 rules with PII allowed)."""
    return await _inspect(req.content, role="user",
                          source_ip=req.source_ip, user_id=req.user_id,
                          enabled_rules=req.enabled_rules)


@app.post("/scan", response_model=ScanResult)
async def scan_generic(req: ScanRequest) -> ScanResult:
    """Generic scan with explicit role. Used for testing and flexibility."""
    return await _inspect(req.content, role=req.role,
                          source_ip=req.source_ip, user_id=req.user_id,
                          enabled_rules=req.enabled_rules)
