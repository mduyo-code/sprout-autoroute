import os
import json
import logging
import re
import gspread
import requests
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

FRESHDESK_DOMAIN  = os.environ["FRESHDESK_DOMAIN"]
FRESHDESK_API_KEY = os.environ["FRESHDESK_API_KEY"]
GOOGLE_SHEET_ID   = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]

SCENARIO_MAP = {
    "p1_bil_dispute":  {"AC":70000512289,"AG":70000516799,"BP":70000518088,"JL":70000516820,"KC":70000518089,"MD":70000482143,"YN":70000519482},
    "p1_bil_revision": {"AC":70000516821,"AG":70000516798,"BP":70000516822,"JL":70000518090,"KC":70000518091,"MD":70000516823,"YN":70000519483},
    "p1_col_posting":  {"AC":70000516825,"AG":70000513826,"BP":70000516826,"JL":70000518092,"KC":70000518093,"MD":70000516827,"YN":70000519484},
    "p2_bil_renewal":  {"AC":70000516829,"AG":70000516802,"BP":70000516830,"JL":70000518095,"KC":70000518096,"MD":70000516833,"YN":70000519485},
    "p2_bil_request":  {"AC":70000516834,"AG":70000516801,"BP":70000516835,"JL":70000518097,"KC":70000518098,"MD":70000516836,"YN":70000519486},
    "p2_col_cr_or":    {"AC":70000516838,"AG":70000516800,"BP":70000516839,"JL":70000518099,"KC":70000518100,"MD":70000516840,"YN":70000519487},
    "p2_other_2303":   {"AC":70000516842,"AG":70000516803,"BP":70000516843,"JL":70000518101,"KC":70000518102,"MD":70000516844,"YN":70000519488},
}

DEFAULT_INITIALS = "MD"
DEFAULT_SCENARIO = "p1_bil_dispute"

KEYWORD_RULES = [
    ("p1_col_posting", ["2307","bir 2307","proof of payment","proof of pmt","payment confirmation","ewt","cwt","creditable withholding","remittance","payment advice","transfer confirmation","bills payment","gcash payment","bank transfer","payment receipt","for posting","payment attached"]),
    ("p1_bil_revision", ["revise invoice","revised invoice","invoice revision","update invoice","amend invoice","amended invoice","correction on invoice","correct invoice","wrong vat","incorrect vat","vat correction","reissue invoice","re-issue invoice"]),
    ("p1_bil_dispute",  ["invoice dispute","dispute invoice","wrong amount","incorrect amount","incorrect invoice","discrepancy","reconciliation","recon","statement of account","overcharge","double charge","billing issue","billing error","billing discrepancy","clarification on invoice","billing already covered","already paid","duplicate invoice"]),
    ("p2_col_cr_or",    ["official receipt","collection receipt","request or","request cr","send or","send cr","copy of or","copy of cr","need or","need cr","payment extension","extend payment","extension of payment","payment deadline","request extension","extend due date","grace period","staggered payment","payment arrangement","installment","partial payment"]),
    ("p2_other_2303",   ["2303","bir 2303","certificate of registration","bir certificate","sprout docs","sprout document","bir registration","tax registration","accreditation document"]),
    ("p2_bil_renewal",  ["renewal","renew subscription","contract renewal","subscription renewal","new client","new account","onboarding","new subscription","new billing","new contract","additional module","additional license","new user license"]),
    ("p2_bil_request",  ["request invoice","need invoice","send invoice","resend invoice","copy of invoice","invoice copy","soft copy","hard copy","request for invoice","invoice recipient","add recipient","billing recipient","invoice distribution","update recipient","update contact","billing contact","update billing","invoice email"]),
]

AGENT_INITIALS = {
    "abegail cruz":"AC","andrea gaor":"AG","bea punzalan":"BP",
    "john paulo ligad":"JL","john ligad":"JL",
    "katrina blanca catalan":"KC","katrina catalan":"KC",
    "mabel duyo":"MD","ynna navarra":"YN",
    "jessica orinday":"JO","angeline flores":"AF",
}

SCENARIO_LABELS = {
    "p1_bil_dispute":  ("P1_BILLINGS",      "Invoice Disputes, Client Request, Recon"),
    "p1_bil_revision": ("P1_BILLINGS",      "Invoice Revision"),
    "p1_col_posting":  ("P1_Collection",    "Posting Proof of Payment & BIR 2307"),
    "p2_bil_renewal":  ("P2_Billings",      "Billing of Renewal and New Client"),
    "p2_bil_request":  ("P2_Billings",      "Request Invoice (soft & hard copy)"),
    "p2_col_cr_or":    ("P2_Collection",    "Request for CR, OR, Payment Extension, Recon"),
    "p2_other_2303":   ("P2_Other request", "Request for 2303 & Other Sprout Docs"),
}

_sheet_cache = {}
_sheet_cache_expiry = datetime.min

def _load_sheet():
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
    EMAIL_COLS = [14,21,22,23,24,25,26]
    NAME_COLS  = [0,2,30]
    PIC_COL    = 33
    clients = {}
    for row in all_rows[1:]:
        def get(idx):
            return row[idx].strip() if idx < len(row) else ""
        primary_name = get(0)
        pic = get(PIC_COL)
        if not pic or not primary_name:
            continue
        alt_names = [get(i) for i in NAME_COLS if get(i)]
        emails = [get(i) for i in EMAIL_COLS if get(i)]
        if primary_name not in clients:
            clients[primary_name] = {"company":primary_name,"alt_names":[],"pic":pic,"emails":[]}
        for n in alt_names:
            if n and n not in clients[primary_name]["alt_names"]:
                clients[primary_name]["alt_names"].append(n)
        clients[primary_name]["emails"].extend(emails)
        clients[primary_name]["emails"] = list(dict.fromkeys(clients[primary_name]["emails"]))
    _sheet_cache = clients
    _sheet_cache_expiry = datetime.now() + timedelta(minutes=10)
    log.info("Loaded %d client records from Google Sheets", len(clients))
    return list(clients.values())

def _normalize(text):
    text = text.lower().strip()
    for s in [" inc."," inc"," corp."," corp"," co."," co"," ltd."," ltd"," llc"," phil."," phil"," philippines"," phils."," phils"," opc"," bpo"," international"," intl"]:
        text = text.replace(s,"")
    return text.strip()

def identify_client(sender_email="", subject="", description=""):
    email_lower = (sender_email or "").lower().strip()
    clients = _load_sheet()
    for c in clients:
        if email_lower in [e.lower() for e in c["emails"]]:
            log.info("Matched exact email: %s → %s", sender_email, c["company"])
            return {**c,"match_type":"exact_email"}
    domain = email_lower.split("@")[-1] if "@" in email_lower else ""
    generic = {"gmail.com","yahoo.com","hotmail.com","outlook.com","icloud.com","live.com","ymail.com","sprout.ph","netsuite.com","email.netsuite.com","5843001.email.netsuite.com"}
    if domain and domain not in generic:
        for c in clients:
            if any(domain in e.lower() for e in c["emails"]):
                log.info("Matched domain: %s → %s", domain, c["company"])
                return {**c,"match_type":"domain"}
    search_texts = []
    if subject:
        search_texts.append(("subject", subject))
    if description:
        clean = re.sub(r"<[^>]+>"," ", description)
        clean = re.sub(r"\s+"," ", clean).strip()
        search_texts.append(("description", clean[:3000]))
    for c in clients:
        all_names = [c["company"]] + c.get("alt_names",[])
        norm_names = [_normalize(n) for n in all_names if n]
        for source, text in search_texts:
            text_norm = _normalize(text)
            for norm_name in norm_names:
                if len(norm_name) >= 4 and norm_name in text_norm:
                    log.info("Matched company in %s: '%s' → %s", source, norm_name, c["company"])
                    return {**c,"match_type":f"company_name_in_{source}"}
    log.info("No client match for email='%s'", sender_email)
    return None

def get_agent_initials(pic_name):
    if not pic_name:
        return DEFAULT_INITIALS
    initials = AGENT_INITIALS.get(pic_name.lower().strip())
    if not initials:
        log.warning("No initials for PIC '%s', using default", pic_name)
        return DEFAULT_INITIALS
    return initials

def _strip_html(text):
    text = re.sub(r"<[^>]+>"," ", text)
    return re.sub(r"\s+"," ", text).strip().lower()

def route_ticket(subject="", description="", sender_email="", client=None):
    subject_lower = subject.lower()
    body_lower    = _strip_html(description)
    full_text     = subject_lower + " " + body_lower
    matched_scenario = None
    matched_keywords = []
    for scenario_id, keywords in KEYWORD_RULES:
        hits = [kw for kw in keywords if kw.lower() in full_text]
        if hits:
            matched_scenario = scenario_id
            matched_keywords = hits
            log.info("Keyword match → %s | keywords: %s", scenario_id, hits)
            break
    if not matched_scenario:
        matched_scenario = DEFAULT_SCENARIO
        log.info("No keyword match — using default: %s", DEFAULT_SCENARIO)
    queue, label = SCENARIO_LABELS.get(matched_scenario, ("P1_BILLINGS","Invoice Disputes, Client Request, Recon"))
    high = ["urgent","asap","immediately","suspended","deactivated","overdue","past due","critical","deadline today","due today","final notice"]
    medium = ["follow up","follow-up","reminder","please expedite","still waiting","not yet received"]
    urgency = "high" if any(s in full_text for s in high) else "medium" if any(s in full_text for s in medium) else "low"
    return {"scenario_id":matched_scenario,"scenario_queue":queue,"label":label,"matched_keywords":matched_keywords,"confidence":100 if matched_keywords else 0,"urgency":urgency,"reasoning":f"Matched keywords: {matched_keywords}" if matched_keywords else "No keywords matched — default scenario used."}

def _fd_auth():
    return (FRESHDESK_API_KEY,"X")

def _fd_url(path):
    domain = FRESHDESK_DOMAIN.rstrip("/")
    if not domain.startswith("http"):
        domain = f"https://{domain}"
    return f"{domain}/api/v2{path}"

def assign_ticket(ticket_id="", routing=None, client=None):
    routing    = routing or {}
    pic_name   = client.get("pic","") if client else ""
    initials   = get_agent_initials(pic_name)
    scene_type = routing.get("scenario_id", DEFAULT_SCENARIO)
    scene_id   = SCENARIO_MAP.get(scene_type,{}).get(initials) or SCENARIO_MAP.get(scene_type,{}).get(DEFAULT_INITIALS)
    if not scene_id:
        log.error("No scenario ID for type='%s' initials='%s'", scene_type, initials)
        return {"status":"error","reason":"scenario_id not found"}
    log.info("Triggering scenario %s (%s+%s) on ticket #%s", scene_id, scene_type, initials, ticket_id)
    url = _fd_url(f"/tickets/{ticket_id}/trigger_scenario")
    r   = requests.post(url, json={"scenario_id":scene_id}, auth=_fd_auth(), headers={"Content-Type":"application/json"}, timeout=10)
    if r.status_code in (200,204):
        log.info("Scenario triggered successfully on ticket #%s", ticket_id)
        return {"status":"ok","scenario_id":scene_id,"initials":initials,"pic":pic_name,"type":scene_type}
    else:
        log.error("Scenario trigger failed %s: %s", r.status_code, r.text)
        return {"status":"error","code":r.status_code,"body":r.text}
