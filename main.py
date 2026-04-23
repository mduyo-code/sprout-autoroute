"""
Sprout AutoRoute — Freshdesk Webhook Server
Receives new ticket webhooks from Freshdesk, identifies the client via
Google Sheets, and routes the ticket to the correct agent + group via AI.
"""

import os
import json
import logging
import requests
from flask import Flask, request, jsonify
from router import identify_client, route_ticket, assign_ticket

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

# ── Health check ─────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "Sprout AutoRoute"}), 200


# ── Freshdesk Webhook endpoint ────────────────────────────────────────────────
@app.route("/webhook/freshdesk", methods=["POST"])
def freshdesk_webhook():
    """
    Freshdesk calls this URL every time a new ticket is created.
    Payload shape (Freshdesk Automation → Webhook):
    {
      "ticket_id":   "12345",
      "subject":     "Invoice dispute for March",
      "description": "Hi, our invoice amount is wrong...",
      "email":       "client@company.com",
      "group_name":  "Billing",
      "status":      "2"          (2=Open)
    }
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        log.info("Webhook received: %s", json.dumps(data, indent=2))

        ticket_id   = str(data.get("ticket_id", "")).strip()
        subject     = data.get("subject", "").strip()
        description = data.get("description", "").strip()
        sender_email = data.get("email", "").strip().lower()

        if not ticket_id:
            return jsonify({"error": "Missing ticket_id"}), 400

        log.info("Processing ticket #%s from %s", ticket_id, sender_email)

        # 1. Identify client from sender email via Google Sheets
        client = identify_client(sender_email)
        log.info("Client match: %s", client)

        # 2. Ask Claude to pick the right Freshdesk scenario
        routing = route_ticket(
            subject=subject,
            description=description,
            sender_email=sender_email,
            client=client,
        )
        log.info("Routing decision: %s", routing)

        # 3. Apply the routing in Freshdesk (assign agent + group + tags)
        assign_result = assign_ticket(
            ticket_id=ticket_id,
            routing=routing,
            client=client,
        )
        log.info("Freshdesk update result: %s", assign_result)

        return jsonify({
            "ticket_id":   ticket_id,
            "client":      client.get("company") if client else "unknown",
            "pic":         client.get("pic")     if client else None,
            "scenario":    routing.get("scenario_queue"),
            "confidence":  routing.get("confidence"),
            "urgency":     routing.get("urgency"),
            "fd_status":   assign_result.get("status"),
        }), 200

    except Exception as exc:
        log.exception("Webhook handler failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
