"""
Seed NetBox with realistic Cisco-flavored data for the AI Defense demo.

Idempotent: every create checks for existing by slug/name first.
Includes intentionally sensitive fields (contact email/phone, SNMP communities
in device comments) so the output gate fires on natural-looking queries.
"""
from __future__ import annotations

import logging
import os
import sys
import time

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s seed %(message)s")
log = logging.getLogger(__name__)

NETBOX_URL = os.environ["NETBOX_URL"].rstrip("/")
TOKEN = os.environ["NETBOX_API_TOKEN"]

client = httpx.Client(
    base_url=NETBOX_URL,
    headers={"Authorization": f"Token {TOKEN}", "Accept": "application/json", "Content-Type": "application/json"},
    timeout=httpx.Timeout(30.0, connect=10.0),
)


def wait_for_netbox(retries: int = 30) -> None:
    for i in range(retries):
        try:
            r = client.get("/api/status/")
            if r.status_code == 200:
                log.info(f"NetBox is up (try {i+1})")
                return
        except httpx.HTTPError:
            pass
        log.info(f"Waiting for NetBox... ({i+1}/{retries})")
        time.sleep(5)
    log.error("NetBox never came up")
    sys.exit(1)


def get_or_create(endpoint: str, lookup: dict, payload: dict | None = None) -> dict:
    """GET filter via `lookup` (use `_id` suffix for FK-by-id NetBox filters);
    if no match, POST merge of (lookup minus `_id`-suffix keys) + `payload`."""
    r = client.get(endpoint, params={**lookup, "limit": 1})
    if r.status_code != 200:
        log.error(f"Lookup failed {endpoint} params={lookup}: {r.status_code} {r.text[:300]}")
        r.raise_for_status()
    results = r.json().get("results", [])
    if results:
        return results[0]
    lookup_clean = {k: v for k, v in lookup.items() if not k.endswith("_id")}
    body = {**lookup_clean, **(payload or {})}
    r = client.post(endpoint, json=body)
    if r.status_code not in (200, 201):
        log.error(f"Create failed {endpoint} body={body}: {r.status_code} {r.text[:300]}")
        r.raise_for_status()
    obj = r.json()
    log.info(f"Created {endpoint} → {obj.get('display') or obj.get('name')}")
    return obj


def main() -> None:
    wait_for_netbox()

    # ── Manufacturer ────────────────────────────────────────────────
    cisco = get_or_create("/api/dcim/manufacturers/", {"slug": "cisco"}, {"name": "Cisco"})

    # ── Device types ────────────────────────────────────────────────
    DEVICE_TYPES = [
        # Original fleet
        ("Catalyst 9300-48UXM",       "c9300-48uxm",   "WS-C9300-48UXM",   1),
        ("Catalyst 9500-32C",         "c9500-32c",     "C9500-32C",        1),
        ("Nexus 93180YC-FX3",         "n9k-93180yc",   "N9K-C93180YC-FX3", 1),
        ("Nexus 9504",                "n9k-9504",      "N9K-C9504",        7),
        ("Meraki MR46",               "mr46",          "MR46-HW",          1),
        ("ASR 1001-X",                "asr1001-x",     "ASR1001-X",        1),
        # Nexus 9000 fabric variety (DC spines/leaves)
        ("Nexus 9332D-GX2B",          "n9k-9332d-gx2b","N9K-C9332D-GX2B",  1),  # 400G spine
        ("Nexus 9336C-FX2",           "n9k-9336c-fx2", "N9K-C9336C-FX2",   1),  # 100G leaf
        ("Nexus 9508",                "n9k-9508",      "N9K-C9508",        13), # modular chassis
        # Catalyst 9000 — full enterprise campus line
        ("Catalyst 9300X-48HX",       "c9300x-48hx",   "C9300X-48HX",      1),  # high-density UPoE+
        ("Catalyst 9200L-48P-4G",     "c9200l-48p-4g", "C9200L-48P-4G",    1),  # smart switch / SMB access
        ("Catalyst 9400 Series",      "c9410r",        "C9410R",           10), # modular distribution
        ("Catalyst 9800-CL",          "c9800-cl",      "C9800-CL-K9",      1),  # wireless controller
        # SD-WAN edge
        ("Catalyst 8500L",            "c8500l",        "C8500L-8S4X",      2),  # SD-WAN edge router
        # Security
        ("Cisco Secure Firewall 3110","sf-3110",       "FPR-3110-NGFW-K9", 1),  # NGFW
        # Compute
        ("UCS-X210C-M7",              "ucs-x210c-m7",  "UCSX-210C-M7",     1),  # X-Series blade
        # Wireless (newer)
        ("Meraki MR56",               "mr56",          "MR56-HW",          1),  # Wi-Fi 6E AP
    ]
    dt_by_slug = {}
    for model, slug, part, u in DEVICE_TYPES:
        dt_by_slug[slug] = get_or_create(
            "/api/dcim/device-types/",
            {"slug": slug, "manufacturer_id": cisco["id"]},
            {"manufacturer": cisco["id"], "model": model, "part_number": part, "u_height": u},
        )

    # ── Device roles ────────────────────────────────────────────────
    ROLES = [
        ("Core",         "core",         "#1d4ed8"),
        ("Distribution", "distribution", "#0e7490"),
        ("Access",       "access",       "#15803d"),
        ("Edge",         "edge",         "#b45309"),
        ("Wireless",     "wireless",     "#9333ea"),
        ("Spine",        "spine",        "#3b82f6"),   # DC fabric spine
        ("Leaf",         "leaf",         "#06b6d4"),   # DC fabric leaf
        ("Firewall",     "firewall",     "#ef4444"),   # NGFW
        ("Server",       "server",       "#a855f7"),   # UCS compute
    ]
    role_by_slug = {}
    for name, slug, color in ROLES:
        role_by_slug[slug] = get_or_create(
            "/api/dcim/device-roles/",
            {"slug": slug},
            {"name": name, "color": color.lstrip("#")},
        )

    # ── Sites ──────────────────────────────────────────────────────
    SITES = [
        ("DC-1 ATL",     "dc-1-atl",    "active",  "Atlanta primary data center"),
        ("DC-2 RTP",     "dc-2-rtp",    "active",  "Research Triangle Park DR site"),
        ("Branch-SJC",   "branch-sjc",  "active",  "San Jose campus"),
        ("Branch-AMS",   "branch-ams",  "active",  "Amsterdam EMEA office"),
        ("Branch-NYC",   "branch-nyc",  "active",  "New York corporate HQ"),
        ("Branch-SFO",   "branch-sfo",  "active",  "San Francisco financial district"),
    ]
    site_by_slug = {}
    for name, slug, status, desc in SITES:
        site_by_slug[slug] = get_or_create(
            "/api/dcim/sites/",
            {"slug": slug},
            {"name": name, "status": status, "description": desc},
        )

    # ── Tenants ────────────────────────────────────────────────────
    TENANTS = [
        ("Salesforce-Eng",  "salesforce-eng",  "Salesforce engineering workloads"),
        ("Workday-Prod",    "workday-prod",    "Workday HR production"),
        ("Internal-Corp",   "internal-corp",   "Corporate IT services"),
    ]
    tenant_by_slug = {}
    for name, slug, desc in TENANTS:
        tenant_by_slug[slug] = get_or_create(
            "/api/tenancy/tenants/",
            {"slug": slug},
            {"name": name, "description": desc},
        )

    # ── Devices (with intentionally-sensitive comments for output-gate demo) ──
    DEVICES = [
        # name              site          role          device_type       serial            snmp_community
        # ── DC-1 ATL — primary data center, full Nexus 9k fabric + Catalyst campus ──
        ("atl-core-01",     "dc-1-atl",   "core",         "c9500-32c",      "FCW2147L0XK",    "atl-c0re-r0!"),
        ("atl-core-02",     "dc-1-atl",   "core",         "c9500-32c",      "FCW2147L0YM",    "atl-c0re-r0!"),
        ("atl-spine-01",    "dc-1-atl",   "spine",        "n9k-9332d-gx2b", "FDO241000S1",    "atl-fab-sp1n3"),
        ("atl-spine-02",    "dc-1-atl",   "spine",        "n9k-9332d-gx2b", "FDO241000S2",    "atl-fab-sp1n3"),
        ("atl-leaf-01",     "dc-1-atl",   "leaf",         "n9k-93180yc",    "FDO231701A1",    "atl-leaf-l3af"),
        ("atl-leaf-02",     "dc-1-atl",   "leaf",         "n9k-9336c-fx2",  "FDO241101L2",    "atl-leaf-l3af"),
        ("atl-leaf-03",     "dc-1-atl",   "leaf",         "n9k-9336c-fx2",  "FDO241101L3",    "atl-leaf-l3af"),
        ("atl-fab-01",      "dc-1-atl",   "core",         "n9k-9504",       "SAL2015AA01",    "atl-fab-N3xus"),
        ("atl-fab-02",      "dc-1-atl",   "core",         "n9k-9508",       "SAL2015AA02",    "atl-fab-N3xus"),
        ("atl-dist-01",     "dc-1-atl",   "distribution", "c9410r",         "FCW2231D1AB",    "atl-d1st-2024"),
        ("atl-dist-02",     "dc-1-atl",   "distribution", "c9300x-48hx",    "FCW2231D1AC",    "atl-d1st-2024"),
        ("atl-acc-01",      "dc-1-atl",   "access",       "c9300-48uxm",    "FCW2331E2BC",    "atl-acc-prod"),
        ("atl-acc-02",      "dc-1-atl",   "access",       "c9200l-48p-4g",  "FCW2331E2BD",    "atl-acc-smart"),
        ("atl-acc-03",      "dc-1-atl",   "access",       "c9200l-48p-4g",  "FCW2331E2BE",    "atl-acc-smart"),
        ("atl-fw-01",       "dc-1-atl",   "firewall",     "sf-3110",        "JAE25011FW1",    "atl-fw-Sec!2024"),
        ("atl-fw-02",       "dc-1-atl",   "firewall",     "sf-3110",        "JAE25011FW2",    "atl-fw-Sec!2024"),
        ("atl-wlc-01",      "dc-1-atl",   "wireless",     "c9800-cl",       "VIRT-9800-ATL01","atl-wlc-c1sc0"),
        ("atl-ap-01",       "dc-1-atl",   "wireless",     "mr56",           "Q2VV-AAAA-AP01", "atl-ap-w1f6"),
        ("atl-ap-02",       "dc-1-atl",   "wireless",     "mr56",           "Q2VV-AAAA-AP02", "atl-ap-w1f6"),
        ("atl-edge-01",     "dc-1-atl",   "edge",         "asr1001-x",      "JAE21380KKK",    "atl-edge-pub!"),
        ("atl-edge-02",     "dc-1-atl",   "edge",         "c8500l",         "FDO250100E1",    "atl-sdwan-edge"),
        ("atl-ucs-01",      "dc-1-atl",   "server",       "ucs-x210c-m7",   "FCH27001UCS",    "atl-ucs-imc1"),
        ("atl-ucs-02",      "dc-1-atl",   "server",       "ucs-x210c-m7",   "FCH27001UCS",    "atl-ucs-imc1"),

        # ── DC-2 RTP — DR data center, mirrored fabric ──
        ("rtp-core-01",     "dc-2-rtp",   "core",         "c9500-32c",      "FCW2147L0ZN",    "rtp-DR-c0re!"),
        ("rtp-spine-01",    "dc-2-rtp",   "spine",        "n9k-9332d-gx2b", "FDO241000R1",    "rtp-fab-sp1n3"),
        ("rtp-spine-02",    "dc-2-rtp",   "spine",        "n9k-9332d-gx2b", "FDO241000R2",    "rtp-fab-sp1n3"),
        ("rtp-leaf-01",     "dc-2-rtp",   "leaf",         "n9k-9336c-fx2",  "FDO241101R1",    "rtp-leaf-l3af"),
        ("rtp-leaf-02",     "dc-2-rtp",   "leaf",         "n9k-9336c-fx2",  "FDO241101R2",    "rtp-leaf-l3af"),
        ("rtp-fab-01",      "dc-2-rtp",   "core",         "n9k-9504",       "SAL2015BB02",    "rtp-fab-DR"),
        ("rtp-dist-01",     "dc-2-rtp",   "distribution", "c9300x-48hx",    "FCW2231D1BB",    "rtp-d1st-DR"),
        ("rtp-fw-01",       "dc-2-rtp",   "firewall",     "sf-3110",        "JAE25011FW3",    "rtp-fw-DR!"),
        ("rtp-edge-01",     "dc-2-rtp",   "edge",         "asr1001-x",      "JAE21380KLL",    "rtp-edge-DR!"),
        ("rtp-edge-02",     "dc-2-rtp",   "edge",         "c8500l",         "FDO250100E2",    "rtp-sdwan-edge"),
        ("rtp-ucs-01",      "dc-2-rtp",   "server",       "ucs-x210c-m7",   "FCH27002UCS",    "rtp-ucs-imc2"),

        # ── Branch-SJC ──
        ("sjc-acc-01",      "branch-sjc", "access",       "c9300-48uxm",    "FCW2440F3CD",    "sjc-acc-2024"),
        ("sjc-acc-02",      "branch-sjc", "access",       "c9200l-48p-4g",  "FCW2440F3CE",    "sjc-acc-smart"),
        ("sjc-wlc-01",      "branch-sjc", "wireless",     "mr46",           "Q2VV-9X8Y-WL01", "sjc-wlc-w1f1"),
        ("sjc-edge-01",     "branch-sjc", "edge",         "c8500l",         "FDO250100SJ1",   "sjc-sdwan-edge"),
        ("sjc-fw-01",       "branch-sjc", "firewall",     "sf-3110",        "JAE25011FWSJ",   "sjc-fw-pacific"),

        # ── Branch-AMS ──
        ("ams-acc-01",      "branch-ams", "access",       "c9300-48uxm",    "FCW2440F3EF",    "ams-acc-emea"),
        ("ams-acc-02",      "branch-ams", "access",       "c9200l-48p-4g",  "FCW2440F3EG",    "ams-acc-smart"),
        ("ams-wlc-01",      "branch-ams", "wireless",     "mr46",           "Q2VV-9X8Y-WL02", "ams-wlc-w1f1"),
        ("ams-edge-01",     "branch-ams", "edge",         "c8500l",         "FDO250100AM1",   "ams-sdwan-edge"),
        ("ams-fw-01",       "branch-ams", "firewall",     "sf-3110",        "JAE25011FWAM",   "ams-fw-emea2024"),

        # ── Branch-NYC — corporate HQ ──
        ("nyc-core-01",     "branch-nyc", "core",         "c9500-32c",      "FCW2147L0NY",    "nyc-c0re-hq!"),
        ("nyc-dist-01",     "branch-nyc", "distribution", "c9300x-48hx",    "FCW2231D1NY",    "nyc-d1st-hq"),
        ("nyc-acc-01",      "branch-nyc", "access",       "c9300x-48hx",    "FCW2331E2NY",    "nyc-acc-hq"),
        ("nyc-acc-02",      "branch-nyc", "access",       "c9200l-48p-4g",  "FCW2331E2NX",    "nyc-acc-smart"),
        ("nyc-wlc-01",      "branch-nyc", "wireless",     "c9800-cl",       "VIRT-9800-NYC01","nyc-wlc-c1sc0"),
        ("nyc-ap-01",       "branch-nyc", "wireless",     "mr56",           "Q2VV-AAAA-NY01", "nyc-ap-w1f6"),
        ("nyc-ap-02",       "branch-nyc", "wireless",     "mr56",           "Q2VV-AAAA-NY02", "nyc-ap-w1f6"),
        ("nyc-fw-01",       "branch-nyc", "firewall",     "sf-3110",        "JAE25011FWNY",   "nyc-fw-corp!"),
        ("nyc-edge-01",     "branch-nyc", "edge",         "c8500l",         "FDO250100NY1",   "nyc-sdwan-edge"),

        # ── Branch-SFO ──
        ("sfo-acc-01",      "branch-sfo", "access",       "c9200l-48p-4g",  "FCW2440F3SF",    "sfo-acc-smart"),
        ("sfo-acc-02",      "branch-sfo", "access",       "c9200l-48p-4g",  "FCW2440F3SG",    "sfo-acc-smart"),
        ("sfo-wlc-01",      "branch-sfo", "wireless",     "mr56",           "Q2VV-AAAA-SF01", "sfo-wlc-w1f6"),
        ("sfo-fw-01",       "branch-sfo", "firewall",     "sf-3110",        "JAE25011FWSF",   "sfo-fw-fin!"),
        ("sfo-edge-01",     "branch-sfo", "edge",         "c8500l",         "FDO250100SF1",   "sfo-sdwan-edge"),
    ]
    for name, site_slug, role_slug, dt_slug, serial, snmp in DEVICES:
        get_or_create(
            "/api/dcim/devices/",
            {"name": name},
            {
                "device_type": dt_by_slug[dt_slug]["id"],
                "role": role_by_slug[role_slug]["id"],
                "site": site_by_slug[site_slug]["id"],
                "status": "active",
                "serial": serial,
                # snmp community embedded in comments — output-gate demo target
                "comments": f"SNMPv2 community: `{snmp}`. Console: ttyS0 9600 8N1. Mgmt VLAN 99.",
            },
        )

    # ── IP prefixes ────────────────────────────────────────────────
    PREFIXES = [
        ("10.10.0.0/16",   "dc-1-atl",   "salesforce-eng",  "ATL Salesforce VLANs"),
        ("10.10.10.0/24",  "dc-1-atl",   "salesforce-eng",  "ATL Salesforce web tier"),
        ("10.10.20.0/24",  "dc-1-atl",   "salesforce-eng",  "ATL Salesforce app tier"),
        ("10.10.30.0/24",  "dc-1-atl",   "salesforce-eng",  "ATL Salesforce DB tier"),
        ("10.20.0.0/16",   "dc-2-rtp",   "workday-prod",    "RTP Workday production"),
        ("10.20.10.0/24",  "dc-2-rtp",   "workday-prod",    "RTP Workday DB tier"),
        ("10.99.0.0/24",   "dc-1-atl",   "internal-corp",   "ATL OOB management"),
        ("10.99.1.0/24",   "dc-2-rtp",   "internal-corp",   "RTP OOB management"),
        # Branch user VLANs
        ("192.168.10.0/24","branch-sjc", "internal-corp",   "SJC user VLAN"),
        ("192.168.20.0/24","branch-ams", "internal-corp",   "AMS user VLAN"),
        ("192.168.30.0/24","branch-nyc", "internal-corp",   "NYC user VLAN"),
        ("192.168.40.0/24","branch-sfo", "internal-corp",   "SFO user VLAN"),
        # DC fabric underlay (VXLAN)
        ("100.64.0.0/24",  "dc-1-atl",   "internal-corp",   "ATL VXLAN underlay /31s"),
        ("100.65.0.0/24",  "dc-2-rtp",   "internal-corp",   "RTP VXLAN underlay /31s"),
        ("10.255.0.0/24",  "dc-1-atl",   "internal-corp",   "ATL fabric loopbacks"),
        ("10.255.1.0/24",  "dc-2-rtp",   "internal-corp",   "RTP fabric loopbacks"),
    ]
    for prefix, site_slug, tenant_slug, desc in PREFIXES:
        get_or_create(
            "/api/ipam/prefixes/",
            {"prefix": prefix},
            {
                "site": site_by_slug[site_slug]["id"],
                "tenant": tenant_by_slug[tenant_slug]["id"],
                "description": desc,
                "status": "active",
            },
        )

    # ── Providers + Circuits ───────────────────────────────────────
    PROVIDERS = [("Lumen", "lumen"), ("AT&T", "att"), ("Verizon", "verizon"), ("Equinix Fabric", "equinix-fabric")]
    prov_by_slug = {}
    for name, slug in PROVIDERS:
        prov_by_slug[slug] = get_or_create("/api/circuits/providers/", {"slug": slug}, {"name": name})

    CIRCUIT_TYPES = [("Internet", "internet"), ("MPLS", "mpls"), ("DCI", "dci")]
    ct_by_slug = {}
    for name, slug in CIRCUIT_TYPES:
        ct_by_slug[slug] = get_or_create("/api/circuits/circuit-types/", {"slug": slug}, {"name": name})

    CIRCUITS = [
        ("LMN-ATL-1001", "lumen",         "internet", "ATL primary internet — 10G"),
        ("ATT-ATL-2001", "att",           "mpls",     "ATL MPLS to RTP — 1G"),
        ("VZN-RTP-3001", "verizon",       "internet", "RTP secondary internet — 10G"),
        ("EQX-ATL-4001", "equinix-fabric","dci",      "ATL↔RTP DCI — 100G"),
        ("LMN-SJC-5001", "lumen",         "internet", "SJC branch internet — 1G"),
        ("ATT-AMS-6001", "att",           "internet", "AMS branch internet — 1G"),
        ("VZN-NYC-7001", "verizon",       "internet", "NYC HQ primary internet — 10G"),
        ("LMN-NYC-7002", "lumen",         "internet", "NYC HQ secondary internet — 10G"),
        ("ATT-NYC-7003", "att",           "mpls",     "NYC ↔ ATL MPLS — 1G"),
        ("LMN-SFO-8001", "lumen",         "internet", "SFO branch internet — 1G"),
        ("VZN-SFO-8002", "verizon",       "internet", "SFO branch failover — 100M"),
    ]
    for cid, prov_slug, type_slug, desc in CIRCUITS:
        get_or_create(
            "/api/circuits/circuits/",
            {"cid": cid},
            {
                "provider": prov_by_slug[prov_slug]["id"],
                "type": ct_by_slug[type_slug]["id"],
                "status": "active",
                "description": desc,
            },
        )

    # ── Contact roles + Contacts (PII for output-gate demo) ─────────
    CONTACT_ROLES = [("NOC", "noc"), ("On-Call", "on-call"), ("Site Lead", "site-lead")]
    cr_by_slug = {}
    for name, slug in CONTACT_ROLES:
        cr_by_slug[slug] = get_or_create("/api/tenancy/contact-roles/", {"slug": slug}, {"name": name})

    CONTACTS = [
        # name                   email                                phone               role          sites
        ("ATL NOC Desk",         "atl-noc@example-corp.com",          "+1-404-555-0142",  "noc",        ["dc-1-atl"]),
        ("Sarah Chen",           "sarah.chen@example-corp.com",       "+1-404-555-0177",  "site-lead",  ["dc-1-atl"]),
        ("Marcus Reed",          "marcus.reed@example-corp.com",      "+1-404-555-0193",  "on-call",    ["dc-1-atl"]),
        ("RTP NOC Desk",         "rtp-noc@example-corp.com",          "+1-919-555-0231",  "noc",        ["dc-2-rtp"]),
        ("Priya Iyer",           "priya.iyer@example-corp.com",       "+1-919-555-0245",  "site-lead",  ["dc-2-rtp"]),
        ("David Kim",            "david.kim@example-corp.com",        "+1-919-555-0267",  "on-call",    ["dc-2-rtp"]),
        ("SJC Branch IT",        "sjc-it@example-corp.com",           "+1-408-555-0301",  "site-lead",  ["branch-sjc"]),
        ("AMS Branch IT",        "ams-it@example-corp.com",           "+31-20-555-0402",  "site-lead",  ["branch-ams"]),
        # NYC HQ
        ("NYC NOC Desk",         "nyc-noc@example-corp.com",          "+1-212-555-0500",  "noc",        ["branch-nyc"]),
        ("Elena Rodriguez",      "elena.rodriguez@example-corp.com",  "+1-212-555-0512",  "site-lead",  ["branch-nyc"]),
        ("James O'Brien",        "james.obrien@example-corp.com",     "+1-212-555-0533",  "on-call",    ["branch-nyc"]),
        # SFO branch
        ("SFO Branch IT",        "sfo-it@example-corp.com",           "+1-415-555-0610",  "site-lead",  ["branch-sfo"]),
        ("Aisha Patel",          "aisha.patel@example-corp.com",      "+1-415-555-0628",  "on-call",    ["branch-sfo"]),
    ]
    for name, email, phone, role_slug, site_slugs in CONTACTS:
        contact = get_or_create(
            "/api/tenancy/contacts/",
            {"name": name},
            {"email": email, "phone": phone},
        )
        for s in site_slugs:
            site = site_by_slug[s]
            # contact-assignments require object_type + object_id + contact + role
            existing = client.get(
                "/api/tenancy/contact-assignments/",
                params={
                    "object_type": "dcim.site",
                    "object_id": site["id"],
                    "contact_id": contact["id"],
                    "role_id": cr_by_slug[role_slug]["id"],
                    "limit": 1,
                },
            ).json().get("results", [])
            if existing:
                continue
            r = client.post("/api/tenancy/contact-assignments/", json={
                "object_type": "dcim.site",
                "object_id": site["id"],
                "contact": contact["id"],
                "role": cr_by_slug[role_slug]["id"],
            })
            if r.status_code not in (200, 201):
                log.warning(f"Contact assignment failed: {r.status_code} {r.text[:200]}")

    log.info("===========================================")
    log.info("Seed complete.")
    log.info(f"  Sites:    {len(SITES)}")
    log.info(f"  Devices:  {len(DEVICES)}")
    log.info(f"  Prefixes: {len(PREFIXES)}")
    log.info(f"  Circuits: {len(CIRCUITS)}")
    log.info(f"  Contacts: {len(CONTACTS)}")
    log.info("===========================================")


if __name__ == "__main__":
    main()
