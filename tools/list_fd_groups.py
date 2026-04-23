"""
tools/list_fd_groups.py
Run this ONCE after deploying to get your Freshdesk group IDs and agent IDs.

Usage (locally):
  pip install requests
  FRESHDESK_DOMAIN=yourcompany.freshdesk.com \
  FRESHDESK_API_KEY=your_key_here \
  python tools/list_fd_groups.py
"""

import os
import requests

DOMAIN  = os.environ.get("FRESHDESK_DOMAIN", "").rstrip("/")
API_KEY = os.environ.get("FRESHDESK_API_KEY", "")

if not DOMAIN or not API_KEY:
    print("ERROR: Set FRESHDESK_DOMAIN and FRESHDESK_API_KEY environment variables.")
    exit(1)

BASE = f"https://{DOMAIN}/api/v2"
AUTH = (API_KEY, "X")

def get(path):
    r = requests.get(f"{BASE}{path}", auth=AUTH)
    r.raise_for_status()
    return r.json()

print("\n" + "="*60)
print("FRESHDESK GROUPS")
print("="*60)
groups = get("/groups")
for g in groups:
    print(f"  ID: {g['id']:>8}   Name: {g['name']}")

print("\n" + "="*60)
print("FRESHDESK AGENTS")
print("="*60)
agents = get("/agents")
for a in agents:
    print(f"  ID: {a['id']:>8}   Name: {a['contact']['name']}")

print("\n" + "="*60)
print("Copy the group IDs into FRESHDESK_GROUPS in router.py")
print("="*60 + "\n")
