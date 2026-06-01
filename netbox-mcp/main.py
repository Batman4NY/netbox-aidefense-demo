"""
NetBox MCP-style wrapper.

Exposes a small, hand-picked toolset over NetBox REST. Read tools are safe;
one destructive tool (delete_prefix) exists ONLY to demonstrate AI Defense
catching the call at the tool-arg gate before it ever fires.

Tool list is also published via OpenAI function-calling format at GET /tools
so the orchestrator can pass it straight to Nemotron.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s netbox-mcp %(message)s")
log = logging.getLogger(__name__)

NETBOX_URL = os.environ["NETBOX_URL"].rstrip("/")
NETBOX_TOKEN = os.environ["NETBOX_API_TOKEN"]

app = FastAPI(title="NetBox MCP", version="1.0")
client = httpx.AsyncClient(
    base_url=NETBOX_URL,
    headers={"Authorization": f"Token {NETBOX_TOKEN}", "Accept": "application/json"},
    timeout=httpx.Timeout(15.0, connect=5.0),
)


# ----------------------------------------------------------------------------
# OpenAI function-calling schema for every tool
# ----------------------------------------------------------------------------
TOOLS_SPEC: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "netbox_search",
            "description": "Search NetBox across devices, sites, prefixes, and circuits by free-text query. Use this first when the user asks an ambiguous question.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Free-text search string, e.g. 'DC-1' or 'nexus 93180'"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "netbox_list_devices",
            "description": "List network devices, optionally filtered by site / role / model family. Returns name, model, status, primary IP.",
            "parameters": {
                "type": "object",
                "properties": {
                    "site": {"type": "string", "description": "Site slug. Available: dc-1-atl, dc-2-rtp, branch-nyc, branch-sjc, branch-sfo, branch-ams"},
                    "role": {"type": "string", "enum": ["core", "distribution", "access", "edge", "wireless", "spine", "leaf", "firewall", "server"], "description": "Device functional role."},
                    "model_contains": {"type": "string", "description": "Substring of model name (e.g. 'Nexus' for all Nexus 9000, 'Catalyst' for all Catalysts, 'Meraki', 'UCS', 'ASR', 'Secure Firewall'). Case-insensitive. Use this for product-family questions."},
                    "limit": {"type": "integer", "default": 25, "minimum": 1, "maximum": 100},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "netbox_get_device",
            "description": "Get full detail on one device by name: model, serial, status, primary IP, comments, custom fields.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Exact device name"}},
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "netbox_list_prefixes",
            "description": "List IP prefixes (VLANs / subnets), optionally filtered by site or tenant slug.",
            "parameters": {
                "type": "object",
                "properties": {
                    "site": {"type": "string"},
                    "tenant": {"type": "string"},
                    "limit": {"type": "integer", "default": 25, "minimum": 1, "maximum": 100},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "netbox_list_circuits",
            "description": "List WAN circuits with provider, type, status, and termination sites.",
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "description": "Provider slug, e.g. 'lumen', 'att'"},
                    "status":   {"type": "string", "enum": ["active", "planned", "decommissioning", "offline"], "description": "Circuit status filter"},
                    "limit": {"type": "integer", "default": 25, "minimum": 1, "maximum": 100},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "netbox_get_site_contacts",
            "description": "Get the operational contacts (NOC, on-call) for a site, including name, email, phone. Used for incident escalation.",
            "parameters": {
                "type": "object",
                "properties": {"site": {"type": "string", "description": "Site slug, e.g. 'dc-1-atl'"}},
                "required": ["site"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "netbox_delete_prefix",
            "description": "DESTRUCTIVE: permanently delete an IP prefix from NetBox. Requires explicit user confirmation.",
            "parameters": {
                "type": "object",
                "properties": {"prefix": {"type": "string", "description": "Prefix in CIDR form, e.g. '10.0.0.0/8'"}},
                "required": ["prefix"],
            },
        },
    },
]


# ----------------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "netbox-mcp"}


@app.get("/tools")
async def list_tools() -> dict[str, Any]:
    return {"tools": TOOLS_SPEC}


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = {}


@app.post("/call")
async def call_tool(call: ToolCall) -> dict[str, Any]:
    impl = _IMPL.get(call.name)
    if impl is None:
        raise HTTPException(status_code=404, detail=f"Unknown tool: {call.name}")
    try:
        result = await impl(**call.arguments)
    except httpx.HTTPStatusError as e:
        # Return as a result with error rather than HTTP error so the LLM
        # can react gracefully (e.g. retry with a different role value)
        # instead of treating it as infrastructure failure.
        return {
            "tool": call.name,
            "result": {
                "error": f"netbox_invalid_query",
                "status": e.response.status_code,
                "detail": e.response.text[:300],
                "hint": "Re-check the tool parameters against the schema (esp. enum values).",
            },
        }
    except Exception as e:
        log.exception(f"Tool {call.name} crashed")
        raise HTTPException(status_code=500, detail=f"Tool {call.name} error: {e}")
    return {"tool": call.name, "result": result}


# ----------------------------------------------------------------------------
# Tool implementations (thin wrappers over NetBox REST)
# ----------------------------------------------------------------------------
async def _get(path: str, params: dict | None = None) -> dict[str, Any]:
    r = await client.get(path, params=params or {})
    r.raise_for_status()
    return r.json()


# Map common natural-language keywords to NetBox role slugs / model-name substrings.
# Used by netbox_search to recover when the LLM picks search for an inventory question.
_ROLE_SYNONYMS = {
    "firewall": "firewall", "firewalls": "firewall", "ngfw": "firewall", "fw": "firewall",
    "router": "edge",       "routers": "edge",       "wan": "edge",      "sd-wan": "edge",
    "ap": "wireless",       "aps": "wireless",       "wlc": "wireless",  "wifi": "wireless",
    "access-point": "wireless", "access-points": "wireless", "wireless": "wireless",
    "spine": "spine",       "spines": "spine",
    "leaf": "leaf",         "leaves": "leaf",
    "server": "server",     "servers": "server",     "ucs": "server",    "compute": "server",
    "core": "core",
    "distribution": "distribution", "dist": "distribution",
    "access": "access",
}
_MODEL_KEYWORDS = ["nexus", "catalyst", "meraki", "asr", "secure firewall", "ucs"]


async def t_search(query: str) -> dict[str, Any]:
    """Global search. Strategy:
    1. NetBox text search across devices/sites/prefixes/circuits (matches names + serials).
    2. If devices empty AND query contains a role keyword → fall through to role-filtered list.
    3. If devices empty AND query contains a model keyword (nexus/catalyst/etc) → device-type filter.
    This makes the tool robust to inventory questions like 'how many firewalls'."""
    hits: dict[str, list] = {}
    q_lower = query.lower().strip()

    # ── Phase 1: standard text search ──────────────────────────
    for kind, path in [
        ("devices",  "/api/dcim/devices/"),
        ("sites",    "/api/dcim/sites/"),
        ("prefixes", "/api/ipam/prefixes/"),
        ("circuits", "/api/circuits/circuits/"),
    ]:
        data = await _get(path, {"q": query, "limit": 25})
        hits[kind] = [{"id": x["id"], "display": x.get("display") or x.get("name") or str(x)}
                      for x in data.get("results", [])]

    # ── Phase 2: role-keyword fallback ─────────────────────────
    if not hits["devices"]:
        role_slug = None
        for word in q_lower.replace(",", " ").split():
            if word in _ROLE_SYNONYMS:
                role_slug = _ROLE_SYNONYMS[word]
                break
        if role_slug:
            try:
                data = await _get("/api/dcim/devices/", {"role": role_slug, "limit": 50})
                hits["devices"] = [
                    {"id": x["id"],
                     "display": x.get("name"),
                     "model": (x.get("device_type") or {}).get("display"),
                     "site": (x.get("site") or {}).get("display"),
                     "role": (x.get("role") or {}).get("display"),
                     "status": x.get("status", {}).get("value")}
                    for x in data.get("results", [])
                ]
                hits["matched_role"] = role_slug
            except httpx.HTTPStatusError:
                pass

    # ── Phase 3: model-keyword fallback ────────────────────────
    if not hits["devices"]:
        for kw in _MODEL_KEYWORDS:
            if kw in q_lower:
                data = await _get("/api/dcim/devices/", {"model__ic": kw, "limit": 50})
                hits["devices"] = [
                    {"id": x["id"],
                     "display": x.get("name"),
                     "model": (x.get("device_type") or {}).get("display"),
                     "site": (x.get("site") or {}).get("display")}
                    for x in data.get("results", [])
                ]
                if hits["devices"]:
                    hits["matched_model_keyword"] = kw
                    break

    return hits


async def t_list_devices(site: str | None = None, role: str | None = None,
                         model_contains: str | None = None, limit: int = 25) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if site:
        params["site"] = site
    if role:
        params["role"] = role
    if model_contains:
        # NetBox 4.x doesn't expose device_type__model__ic on /devices/, but we
        # can two-step: find device-types whose model matches, then filter devices.
        dt_resp = await _get("/api/dcim/device-types/", {"q": model_contains, "limit": 100})
        dt_ids = [str(x["id"]) for x in dt_resp.get("results", [])]
        if not dt_ids:
            return {"count": 0, "devices": [], "note": f"No device types match {model_contains!r}"}
        params["device_type_id"] = dt_ids
    data = await _get("/api/dcim/devices/", params)
    return {
        "count": data.get("count", 0),
        "devices": [
            {
                "name": d["name"],
                "model": d.get("device_type", {}).get("display"),
                "site": d.get("site", {}).get("display"),
                "role": d.get("role", {}).get("display"),
                "status": d.get("status", {}).get("value"),
                "primary_ip": (d.get("primary_ip") or {}).get("address"),
            }
            for d in data.get("results", [])
        ],
    }


async def t_get_device(name: str) -> dict[str, Any]:
    data = await _get("/api/dcim/devices/", {"name": name, "limit": 1})
    if not data.get("results"):
        return {"error": f"Device '{name}' not found"}
    d = data["results"][0]
    return {
        "name": d["name"],
        "model": d.get("device_type", {}).get("display"),
        "serial": d.get("serial"),
        "site": d.get("site", {}).get("display"),
        "role": d.get("role", {}).get("display"),
        "status": d.get("status", {}).get("value"),
        "primary_ip": (d.get("primary_ip") or {}).get("address"),
        "comments": d.get("comments"),
        "custom_fields": d.get("custom_fields", {}),
    }


async def t_list_prefixes(site: str | None = None, tenant: str | None = None, limit: int = 25) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if site:
        params["site"] = site
    if tenant:
        params["tenant"] = tenant
    data = await _get("/api/ipam/prefixes/", params)
    return {
        "count": data.get("count", 0),
        "prefixes": [
            {
                "prefix": p["prefix"],
                "site": (p.get("site") or {}).get("display"),
                "tenant": (p.get("tenant") or {}).get("display"),
                "description": p.get("description"),
                "status": p.get("status", {}).get("value"),
            }
            for p in data.get("results", [])
        ],
    }


async def t_list_circuits(provider: str | None = None, status: str | None = None, limit: int = 25) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if provider: params["provider"] = provider
    if status:   params["status"] = status
    data = await _get("/api/circuits/circuits/", params)
    return {
        "count": data.get("count", 0),
        "circuits": [
            {
                "cid": c["cid"],
                "provider": (c.get("provider") or {}).get("display"),
                "type": (c.get("type") or {}).get("display"),
                "status": c.get("status", {}).get("value"),
                "description": c.get("description"),
            }
            for c in data.get("results", [])
        ],
    }


async def t_get_site_contacts(site: str) -> dict[str, Any]:
    # NetBox contact-assignments returns the contact as a NestedContact (id+display only).
    # Need a follow-up GET on /tenancy/contacts/{id}/ to get email + phone.
    site_resp = await _get("/api/dcim/sites/", {"slug": site, "limit": 1})
    if not site_resp.get("results"):
        return {"error": f"Site '{site}' not found"}
    site_id = site_resp["results"][0]["id"]
    site_name = site_resp["results"][0]["display"]
    asn_resp = await _get(
        "/api/tenancy/contact-assignments/",
        {"object_type": "dcim.site", "object_id": site_id, "limit": 50},
    )
    contacts = []
    for a in asn_resp.get("results", []):
        contact_id = (a.get("contact") or {}).get("id")
        role_display = (a.get("role") or {}).get("display")
        if not contact_id:
            continue
        try:
            full = await _get(f"/api/tenancy/contacts/{contact_id}/")
        except Exception:
            continue
        contacts.append({
            "name":  full.get("name"),
            "role":  role_display,
            "email": full.get("email"),
            "phone": full.get("phone"),
        })
    return {"site": site_name, "contacts": contacts}


async def t_delete_prefix(prefix: str) -> dict[str, Any]:
    """Destructive — exists only so AI Defense can block the call at the tool-arg gate.
    If we somehow reach this code path in the demo, it means the gate didn't fire.
    Refuse explicitly here as a belt-and-suspenders safeguard."""
    log.warning(f"netbox_delete_prefix CALLED with prefix={prefix!r} — should have been gated upstream")
    return {
        "error": "destructive_action_refused",
        "message": f"Refusing to delete prefix {prefix}. This should have been blocked by AI Defense at the tool-args gate.",
    }


_IMPL = {
    "netbox_search":            t_search,
    "netbox_list_devices":      t_list_devices,
    "netbox_get_device":        t_get_device,
    "netbox_list_prefixes":     t_list_prefixes,
    "netbox_list_circuits":     t_list_circuits,
    "netbox_get_site_contacts": t_get_site_contacts,
    "netbox_delete_prefix":     t_delete_prefix,
}
