"""
test_local.py — Run this to test the webhook locally before deploying.

Usage:
  1. Make sure you have a .env file with all required variables (see .env.example)
  2. pip install python-dotenv
  3. python test_local.py
"""

import json
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("Tip: pip install python-dotenv to auto-load .env file")

import os

# Quick env check
required = ["ANTHROPIC_API_KEY", "FRESHDESK_DOMAIN", "FRESHDESK_API_KEY",
            "GOOGLE_SHEET_ID", "GOOGLE_CREDS_JSON"]
missing = [k for k in required if not os.environ.get(k)]
if missing:
    print(f"ERROR: Missing environment variables: {', '.join(missing)}")
    print("Create a .env file based on .env.example")
    sys.exit(1)

from router import identify_client, route_ticket

# ── Test cases from the actual Freshdesk export ───────────────────────────────
TEST_TICKETS = [
    {
        "name": "Known client — proof of payment",
        "email": "pasay.hr@stemzglobal.com",
        "subject": "Re: Follow-up on BIR 2307 for Your Payment",
        "description": "Please find attached our BIR 2307 for your payment processing.",
    },
    {
        "name": "Known client — invoice dispute",
        "email": "ronjaylordalbaira@lynvilleland.com.ph",
        "subject": "Clarification on Invoice SI_SSPI00008896 – Billing Already Covered by Contract",
        "description": "Good day, we would like to clarify that the invoice amount seems to be covered by our existing contract.",
    },
    {
        "name": "Unknown sender",
        "email": "unknown@mystery.com",
        "subject": "Request for BIR 2303",
        "description": "Please send us your Certificate of Registration for our accreditation.",
    },
]

print("\n" + "="*70)
print("SPROUT AUTOROUTE — LOCAL TEST")
print("="*70)

for t in TEST_TICKETS:
    print(f"\n▶ {t['name']}")
    print(f"  Email:   {t['email']}")
    print(f"  Subject: {t['subject']}")

    client = identify_client(t["email"])
    if client:
        print(f"  Client:  {client['company']} ({client['match_type']} match)")
        print(f"  PIC:     {client['pic']}")
    else:
        print(f"  Client:  ⚠ Unknown sender")

    routing = route_ticket(
        subject=t["subject"],
        description=t["description"],
        sender_email=t["email"],
        client=client,
    )
    print(f"  Queue:   {routing['scenario_queue']}")
    print(f"  Label:   {routing['label']}")
    print(f"  Conf:    {routing['confidence']}%  |  Urgency: {routing['urgency']}")
    print(f"  Reason:  {routing['reasoning']}")

print("\n" + "="*70)
print("All tests complete. If output looks correct, deploy to Render!")
print("="*70 + "\n")
