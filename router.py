"""
router.py — Core logic for Sprout AutoRoute
  1. identify_client  — matches sender email → client record from Google Sheets
  2. route_ticket     — calls Claude API to pick the best Freshdesk scenario
  3. assign_ticket    — updates the Freshdesk ticket (agent, group, tags)
"""

import os
import json
import logging
import re
import gspread
import requests
from anthropic import Anthropic
from google.oauth2.service_account import Credentials
from functools import lru_cache
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# ── Environment variables (set these in Render dashboard) ────────────────────
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
FRESHDESK_DOMAIN    = os.environ["FRESHDESK_DOMAIN"]     # e.g. yourcompany.freshdesk.com
FRESHDESK_API_KEY   = os.environ["FRESHDESK_API_KEY"]
GOOGLE_SHEET_ID     = os.environ["GOOGLE_SHEET_ID"]      # the long ID from the Sheet URL
GOOGLE_CREDS_JSON   = os.environ["GOOGLE_CREDS_JSON"]    # full service account JSON as a string

# ── Freshdesk group name → group ID mapping ──────────────────────────────────
# Fill these in after running: python tools/list_fd_groups.py
FRESHDESK_GROUPS = {
    "P1_BILLINGS":      70000335500,
    "P1_Collection":    70000360397,
    "P2_Billings":      70000335500,
    "P2_Collection":    70000360397,
    "P2_Other request": 70000335500,
}

# ── Scenario definitions (must match your Freshdesk setup) ───────────────────
SCENARIOS = [
    {"id": "p1_bil_dispute",  "queue": "P1_BILLINGS",      "label": "Invoice Disputes, Client Request, Recon"},
    {"id": "p1_bil_revision", "queue": "P1_BILLINGS",      "label": "Invoice Revision"},
    {"id": "p1_col_posting",  "queue": "P1_Collection",    "label": "Posting Proof of Payment & BIR 2307"},
    {"id": "p2_bil_renewal",  "queue": "P2_Billings",      "label": "Billing of Renewal and New Client"},
    {"id": "p2_bil_request",  "queue": "P2_Billings",      "label": "Request Invoice"},
    {"id": "p2_col_cr_or",    "queue": "P2_Collection",    "label": "Request for CR, OR, Payment Extension, Recon"},
    {"id": "p2_other_2303",   "queue": "P2_Other request", "label": "Request for 2303 & Other Sprout Docs"},
]

anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)


# ════════════════════════════════════════════════════════════════════════════
# 1. CLIENT IDENTIFICATION  (Google Sheets)
# ════════════════════════════════════════════════════════════════════════════

_sheet_cache: dict = {}
_sheet_cache_expiry: datetime = datetime.min

def _load_sheet() -> list[dict]:
    """Load and cache client records from Google Sheets (refreshes every 10 min)."""
    global _sheet_cache, _sheet_cache_expiry

    if datetime.now() < _sheet_cache_expiry and _sheet_cache:
        return list(_sheet_cache.values())

    log.info("Refreshing Google Sheets client cache…")
    creds_info = json.loads(GOOGLE_CREDS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(GOOGLE_SHEET_ID).sheet1
    all_rows = ws.get_all_values()

    # Column indices (0-based):
    # O=14, V=21, W=22, X=23, Y=24, Z=25, AA=26, AH=33
    EMAIL_COLS = [14, 21, 22, 23, 24, 25, 26]
    PIC_COL    = 33
    NAME_COL   = 0   # Column A — adjust if company name is elsewhere

    clients: dict[str, dict] = {}  # keyed by company name for dedup

    for row in all_rows[1:]:  # skip header
        def get(idx):
            return row[idx].strip() if idx < len(row) else ""

        company = get(NAME_COL)
        pic     = get(PIC_COL)
        if not pic:
            continue  # skip rows without a Finance PIC

        emails = [get(i) for i in EMAIL_COLS if get(i)]

        if company not in clients:
            clients[company] = {"company": company, "pic": pic, "emails": []}
        clients[company]["emails"].extend(emails)
        # Deduplicate emails
        clients[company]["emails"] = list(dict.fromkeys(clients[company]["emails"]))

    _sheet_cache = clients
    _sheet_cache_expiry = datetime.now() + timedelta(minutes=10)
    log.info("Loaded %d client records from Google Sheets", len(clients))
    return list(clients.values())


def identify_client(sender_email: str) -> dict | None:
    """
    Match sender_email to a client record.
    Priority: 1) exact email match  2) email domain match
    Returns client dict or None if unknown.
    """
    if not sender_email:
        return None

    email_lower = sender_email.lower().strip()
    clients = _load_sheet()

    # 1. Exact match
    for c in clients:
        if email_lower in [e.lower() for e in c["emails"]]:
            return {**c, "match_type": "exact"}

    # 2. Domain match (skip generic domains)
    domain = email_lower.split("@")[-1] if "@" in email_lower else ""
    generic = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
               "icloud.com", "live.com", "ymail.com"}
    if domain and domain not in generic:
        for c in clients:
            if any(domain in e.lower() for e in c["emails"]):
                return {**c, "match_type": "domain"}

    return None


# ════════════════════════════════════════════════════════════════════════════
# 2. AI ROUTING  (Claude)
# ════════════════════════════════════════════════════════════════════════════

def route_ticket(subject: str, description: str, sender_email: str,
                 client: dict | None) -> dict:
    """
    Ask Claude to pick the correct Freshdesk scenario queue.
    Returns a dict with scenario_id, scenario_queue, label, confidence,
    urgency, key_signals, reasoning.
    """
    if client:
        client_ctx = (
            f"IDENTIFIED CLIENT: {client['company']}\n"
            f"FINANCE PIC (Agent to assign): {client['pic']}\n"
            f"Match type: {client['match_type']}"
        )
    else:
        client_ctx = (
            f"CLIENT: Unknown — email '{sender_email}' not in database.\n"
            "Route by ticket content only."
        )

    scenario_list = "\n".join(
        f"{s['id']} | {s['queue']} | {s['label']}" for s in SCENARIOS
    )

    prompt = f"""You are a Freshdesk routing AI for Sprout Solutions (Philippine HR/payroll software).
Route this ticket to the correct scenario queue.

{client_ctx}

TICKET:
Subject: {subject}
Description: {description[:1500]}
Sender email: {sender_email}

SCENARIOS (id | queue | label):
{scenario_list}

Philippine billing context:
- 2307 = BIR Certificate of Creditable Tax Withheld (proof-of-payment doc clients submit)
- 2303 = BIR Certificate of Registration (Sprout's own doc, clients request it)
- OR = Official Receipt, CR = Collection Receipt
- Recon = Reconciliation, SOA = Statement of Account
- P1 = higher priority than P2

Respond ONLY with valid JSON (no markdown, no extra text):
{{
  "scenario_id": "<one of the ids above>",
  "scenario_queue": "<matching queue name>",
  "label": "<matching label>",
  "confidence": <1-100>,
  "urgency": "<low|medium|high>",
  "key_signals": ["<signal1>", "<signal2>", "<signal3>"],
  "reasoning": "<2 concise sentences>"
}}"""

    response = anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    # Strip markdown fences if present
    text = re.sub(r"```json|```", "", text).strip()
    result = json.loads(text)

    # Validate scenario_id
    valid_ids = {s["id"] for s in SCENARIOS}
    if result.get("scenario_id") not in valid_ids:
        # fallback to first scenario
        result["scenario_id"]    = SCENARIOS[0]["id"]
        result["scenario_queue"] = SCENARIOS[0]["queue"]
        result["label"]          = SCENARIOS[0]["label"]

    return result


# ════════════════════════════════════════════════════════════════════════════
# 3. FRESHDESK ASSIGNMENT
# ════════════════════════════════════════════════════════════════════════════

def _fd_headers() -> dict:
    return {"Content-Type": "application/json"}

def _fd_auth() -> tuple:
    return (FRESHDESK_API_KEY, "X")

def _fd_url(path: str) -> str:
    domain = FRESHDESK_DOMAIN.rstrip("/")
    if not domain.startswith("http"):
        domain = f"https://{domain}"
    return f"{domain}/api/v2{path}"


@lru_cache(maxsize=1)
def _get_agents() -> dict:
    """Fetch all Freshdesk agents and return {name_lower: agent_id}."""
    r = requests.get(_fd_url("/agents"), auth=_fd_auth(), timeout=10)
    r.raise_for_status()
    return {a["contact"]["name"].lower(): a["id"] for a in r.json()}


def _find_agent_id(name: str) -> int | None:
    """Look up a Freshdesk agent ID by name (case-insensitive)."""
    if not name:
        return None
    agents = _get_agents()
    return agents.get(name.lower())


def assign_ticket(ticket_id: str, routing: dict, client: dict | None) -> dict:
    """
    Update the Freshdesk ticket:
      - Assign to the Finance PIC agent (if identified)
      - Set the group matching the scenario queue
      - Add routing tags
    """
    update_body: dict = {}

    # Assign agent
    if client and client.get("pic"):
        agent_id = _find_agent_id(client["pic"])
        if agent_id:
            update_body["responder_id"] = agent_id
        else:
            log.warning("Agent '%s' not found in Freshdesk", client["pic"])

    # Assign group
    queue = routing.get("scenario_queue", "")
    group_id = FRESHDESK_GROUPS.get(queue)
    if group_id:
        update_body["group_id"] = group_id
    else:
        log.warning("Group ID not configured for queue '%s'", queue)

    # Add tags
    tags = [
        routing.get("scenario_id", ""),
        f"urgency_{routing.get('urgency', 'medium')}",
        "auto-routed",
    ]
    update_body["tags"] = [t for t in tags if t]

    if not update_body:
        return {"status": "skipped", "reason": "nothing to update"}

    url = _fd_url(f"/tickets/{ticket_id}")
    r = requests.put(url, json=update_body, auth=_fd_auth(),
                     headers=_fd_headers(), timeout=10)

    if r.status_code in (200, 204):
        log.info("Ticket #%s updated successfully", ticket_id)
        return {"status": "ok", "updated": update_body}
    else:
        log.error("Freshdesk update failed %s: %s", r.status_code, r.text)
        return {"status": "error", "code": r.status_code, "body": r.text}
