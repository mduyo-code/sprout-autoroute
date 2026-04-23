"""
router.py — Core logic for Sprout AutoRoute
  1. identify_client  — matches sender email OR company name from Google Sheets
  2. route_ticket     — calls Claude AI to pick the best Freshdesk scenario
  3. assign_ticket    — triggers the exact Freshdesk Scenario Automation
"""

import os
import json
import logging
import re
import gspread
import requests
from anthropic import Anthropic
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# ── Environment variables ─────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
FRESHDESK_DOMAIN  = os.environ["FRESHDESK_DOMAIN"]
FRESHDESK_API_KEY = os.environ["FRESHDESK_API_KEY"]
GOOGLE_SHEET_ID   = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]

anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Freshdesk Scenario Automation IDs ────────────────────────────────────────
# Format: SCENARIO_MAP[scenario_type][agent_initials] = scenario_id
# AC=Abegail Cruz, AG=Andrea Gaor, BP=Bea Punzalan,
# JL=John Paulo Ligad, KC=Katrina Blanca Catalan,
# MD=Mabel Duyo, YN=Ynna Navarra

SCENARIO_MAP = {
    "p1_bil_dispute": {
        "AC": 70000512289,
        "AG": 70000516799,
        "BP": 70000518088,
        "JL": 70000516820,
        "KC": 70000518089,
        "MD": 70000482143,
        "YN": 70000519482,
    },
    "p1_bil_revision": {
        "AC": 70000516821,
        "AG": 70000516798,
        "BP": 70000516822,
        "JL": 70000518090,
        "KC": 70000518091,
        "MD": 70000516823,
        "YN": 70000519483,
    },
    "p1_col_posting": {
        "AC": 70000516825,
        "AG": 70000513826,
        "BP": 70000516826,
        "JL": 70000518092,
        "KC": 70000518093,
        "MD": 70000516827,
        "YN": 70000519484,
    },
    "p2_bil_renewal": {
        "AC": 70000516829,
        "AG": 70000516802,
        "BP": 70000516830,
        "JL": 70000518095,
        "KC": 70000518096,
        "MD": 70000516833,
        "YN": 70000519485,
    },
    "p2_bil_request": {
        "AC": 70000516834,
        "AG": 70000516801,
        "BP": 70000516835,
        "JL": 70000518097,
        "KC": 70000518098,
        "MD": 70000516836,
        "YN": 70000519486,
    },
    "p2_col_cr_or": {
        "AC": 70000516838,
        "AG": 70000516800,
        "BP": 70000516839,
        "JL": 70000518099,
        "KC": 70000518100,
        "MD": 70000516840,
        "YN": 70000519487,
    },
    "p2_other_2303": {
        "AC": 70000516842,
        "AG": 70000516803,
        "BP": 70000516843,
        "JL": 70000518101,
        "KC": 70000518102,
        "MD": 70000516844,
        "YN": 70000519488,
    },
}

DEFAULT_INITIALS = "MD"

# ── Scenario definitions for Claude prompt ────────────────────────────────────
SCENARIOS = [
    {"id": "p1_bil_dispute",  "queue": "P1_BILLINGS",      "label": "Invoice Disputes, Client Request, Recon"},
    {"id": "p1_bil_revision", "queue": "P1_BILLINGS",      "label": "Invoice Revision"},
    {"id": "p1_col_posting",  "queue": "P1_Collection",    "label": "Posting Proof of Payment & BIR 2307"},
    {"id": "p2_bil_renewal",  "queue": "P2_Billings",      "label": "Billing of Renewal and New Client"},
    {"id": "p2_bil_request",  "queue": "P2_Billings",      "label": "Request Invoice (soft & hard copy)"},
    {"id": "p2_col_cr_or",    "queue": "P2_Collection",    "label": "Request for CR, OR, Payment Extension, Recon"},
    {"id": "p2_other_2303",   "queue": "P2_Other request", "label": "Request for 2303 & Other Sprout Docs"},
]

# ── Agent name → initials mapping ────────────────────────────────────────────
AGENT_INITIALS = {
    "abegail cruz":           "AC",
    "andrea gaor":            "AG",
    "bea punzalan":           "BP",
    "john paulo ligad":       "JL",
    "john ligad":             "JL",
    "katrina blanca catalan": "KC",
    "katrina catalan":        "KC",
    "mabel duyo":             "MD",
    "ynna navarra":           "YN",
    "jessica orinday":        "JO",
    "angeline flores":        "AF",
}


# ════════════════════════════════════════════════════════════════════════════
# 1. CLIENT IDENTIFICATION  (Google Sheets)
#    Priority: exact email → domain → company name in subject/body
# ════════════════════════════════════════════════════════════════════════════

_sheet_cache: dict = {}
_sheet_cache_expiry: datetime = datetime.min


def _load_sheet() -> list:
    global _sheet_cache, _sheet_cache_expiry

    if datetime.now() < _sheet_cache_expiry and _sheet_cache:
        return list(_sheet_cache.values())

    log.info("Refreshing Google Sheets client cache...")
    creds_info = json.loads(GOOGLE_CREDS_JSON)
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    gc = gspread.authorize(creds)
    ws = gc.open_by_key(GOOGLE_SHEET_ID).sheet1
    all_rows = ws.get_all_values()

    # Column indices (0-based):
    # A=0 (primary name), C=2 (alt name), O=14, V=21, W=22,
    # X=23, Y=24, Z=25, AA=26 (emails), AE=30 (alt name 2), AH=33 (PIC)
    EMAIL_COLS   = [14, 21, 22, 23, 24, 25, 26]  # O, V, W, X, Y, Z, AA
    NAME_COLS    = [0, 2, 30]                      # A, C, AE
    PIC_COL      = 33                              # AH

    clients: dict = {}

    for row in all_rows[1:]:
        def get(idx):
            return row[idx].strip() if idx < len(row) else ""

        # Collect all name variants (A, C, AE) — use column A as primary key
        primary_name = get(0)
        alt_names    = [get(i) for i in NAME_COLS if get(i)]
        pic          = get(PIC_COL)

        if not pic or not primary_name:
            continue

        emails = [get(i) for i in EMAIL_COLS if get(i)]

        if primary_name not in clients:
            clients[primary_name] = {
                "company":   primary_name,
                "alt_names": [],
                "pic":       pic,
                "emails":    [],
            }

        # Merge alt names and emails (handles multiple rows per company)
        for n in alt_names:
            if n and n not in clients[primary_name]["alt_names"]:
                clients[primary_name]["alt_names"].append(n)
        clients[primary_name]["emails"].extend(emails)
        clients[primary_name]["emails"] = list(
            dict.fromkeys(clients[primary_name]["emails"])
        )

    _sheet_cache = clients
    _sheet_cache_expiry = datetime.now() + timedelta(minutes=10)
    log.info("Loaded %d client records from Google Sheets", len(clients))
    return list(clients.values())


def _normalize(text: str) -> str:
    """Lowercase and remove common suffixes for fuzzy company matching."""
    text = text.lower().strip()
    for suffix in [" inc.", " inc", " corp.", " corp", " co.", " co",
                   " ltd.", " ltd", " llc", " phil.", " phil",
                   " philippines", " phils.", " phils"]:
        text = text.replace(suffix, "")
    return text.strip()


def identify_client(sender_email: str, subject: str = "",
                    description: str = "") -> dict | None:
    """
    Match a ticket to a client record using 4 strategies (in priority order):
      1. Exact email match
      2. Email domain match
      3. Company name found in subject line
      4. Company name found in email body/description
    """
    email_lower = (sender_email or "").lower().strip()
    clients     = _load_sheet()

    # ── Strategy 1: Exact email match ────────────────────────────────────────
    for c in clients:
        if email_lower in [e.lower() for e in c["emails"]]:
            log.info("Matched by exact email: %s → %s", sender_email, c["company"])
            return {**c, "match_type": "exact_email"}

    # ── Strategy 2: Email domain match ───────────────────────────────────────
    domain  = email_lower.split("@")[-1] if "@" in email_lower else ""
    generic = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
               "icloud.com", "live.com", "ymail.com", "sprout.ph",
               "netsuite.com", "email.netsuite.com"}
    if domain and domain not in generic:
        for c in clients:
            if any(domain in e.lower() for e in c["emails"]):
                log.info("Matched by domain: %s → %s", domain, c["company"])
                return {**c, "match_type": "domain"}

    # ── Strategy 3 & 4: Company name in subject or description ───────────────
    search_texts = []
    if subject:
        search_texts.append(("subject", subject))
    if description:
        # Only search first 2000 chars of description for performance
        search_texts.append(("description", description[:2000]))

    for c in clients:
        # Build list of all name variants for this client
        all_names = [c["company"]] + c.get("alt_names", [])
        norm_names = [_normalize(n) for n in all_names if n]

        for source, text in search_texts:
            text_norm = _normalize(text)
            for norm_name in norm_names:
                # Must be at least 4 chars to avoid false positives
                if len(norm_name) >= 4 and norm_name in text_norm:
                    log.info("Matched by company name in %s: '%s' → %s",
                             source, norm_name, c["company"])
                    return {**c, "match_type": f"company_name_in_{source}"}

    log.info("No client match found for email='%s'", sender_email)
    return None


def get_agent_initials(pic_name: str) -> str:
    if not pic_name:
        return DEFAULT_INITIALS
    initials = AGENT_INITIALS.get(pic_name.lower().strip())
    if not initials:
        log.warning("No initials for PIC '%s', using default '%s'",
                    pic_name, DEFAULT_INITIALS)
        return DEFAULT_INITIALS
    return initials


# ════════════════════════════════════════════════════════════════════════════
# 2. AI ROUTING  (Claude)
# ════════════════════════════════════════════════════════════════════════════

def route_ticket(subject: str, description: str, sender_email: str,
                 client: dict | None) -> dict:
    if client:
        client_ctx = (
            f"IDENTIFIED CLIENT: {client['company']}\n"
            f"FINANCE PIC: {client['pic']}\n"
            f"Match type: {client['match_type']}"
        )
    else:
        client_ctx = (
            f"CLIENT: Unknown - '{sender_email}' not in database.\n"
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
Description (first 1500 chars): {description[:1500]}
Sender: {sender_email}

SCENARIOS (id | queue | label):
{scenario_list}

Philippine billing context:
- 2307 = BIR Certificate of Creditable Tax Withheld (proof-of-payment doc clients submit)
- 2303 = BIR Certificate of Registration (Sprout doc clients request)
- OR = Official Receipt, CR = Collection Receipt
- Recon = Reconciliation, SOA = Statement of Account
- P1 = higher priority than P2
- Requests to update invoice recipients / billing contacts = p2_bil_request

Respond ONLY with valid JSON (no markdown):
{{"scenario_id":"<id>","scenario_queue":"<queue>","label":"<label>","confidence":<1-100>,"urgency":"<low|medium|high>","key_signals":["<s1>","<s2>","<s3>"],"reasoning":"<2 sentences>"}}"""

    response = anthropic.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=600,
        messages=[{"role": "user", "content": prompt}],
    )

    text   = re.sub(r"```json|```", "", response.content[0].text.strip()).strip()
    result = json.loads(text)

    valid_ids = {s["id"] for s in SCENARIOS}
    if result.get("scenario_id") not in valid_ids:
        result.update({
            "scenario_id":    SCENARIOS[0]["id"],
            "scenario_queue": SCENARIOS[0]["queue"],
            "label":          SCENARIOS[0]["label"],
        })
    return result


# ════════════════════════════════════════════════════════════════════════════
# 3. FRESHDESK SCENARIO TRIGGER
# ════════════════════════════════════════════════════════════════════════════

def _fd_auth():
    return (FRESHDESK_API_KEY, "X")

def _fd_url(path: str) -> str:
    domain = FRESHDESK_DOMAIN.rstrip("/")
    if not domain.startswith("http"):
        domain = f"https://{domain}"
    return f"{domain}/api/v2{path}"


def assign_ticket(ticket_id: str, routing: dict, client: dict | None) -> dict:
    pic_name   = client.get("pic", "") if client else ""
    initials   = get_agent_initials(pic_name)
    scene_type = routing.get("scenario_id", "p1_bil_dispute")
    scene_id   = (SCENARIO_MAP.get(scene_type, {}).get(initials)
                  or SCENARIO_MAP.get(scene_type, {}).get(DEFAULT_INITIALS))

    if not scene_id:
        log.error("No scenario ID for type='%s' initials='%s'",
                  scene_type, initials)
        return {"status": "error", "reason": "scenario_id not found"}

    log.info("Triggering scenario %s (%s + %s) on ticket #%s",
             scene_id, scene_type, initials, ticket_id)

    url = _fd_url(f"/tickets/{ticket_id}/trigger_scenario")
    r   = requests.post(url,
                        json={"scenario_id": scene_id},
                        auth=_fd_auth(),
                        headers={"Content-Type": "application/json"},
                        timeout=10)

    if r.status_code in (200, 204):
        log.info("Scenario triggered successfully on ticket #%s", ticket_id)
        return {"status": "ok", "scenario_id": scene_id,
                "initials": initials, "pic": pic_name, "type": scene_type}
    else:
        log.error("Scenario trigger failed %s: %s", r.status_code, r.text)
        return {"status": "error", "code": r.status_code, "body": r.text}
