"""
MANA³ Push Sender
Google Cloud Run Function. Sends a single Web Push notification to one client.

Called by S4 immediately after the observation is written to the Daily Record,
so the notification carries the one line worth reading rather than a generic
"your data is ready" nudge.

POST body:
  {
    "client_id": "MANA-TEST",
    "title":     "MANA³",              (optional)
    "body":      "<the observation>",
    "type":      "observation",        ("observation" | "prompt")
    "date":      "2026-08-18"          (optional, echoed to the click handler)
  }

Returns 200 with {"status": "sent"} on success. Never raises into Make: a push
failure must never break the pipeline that produced the observation.
"""

import json
import os
import requests

from pywebpush import webpush, WebPushException

NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
CLIENTS_DB = "e2bb9697-c7a5-4b9d-98be-8d93ac10cc64"

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

# VAPID keypair. The public half must match VAPID_PUBLIC_KEY in index.html
# exactly — a mismatch makes every existing subscription unusable.
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:mana3system@gmail.com")

# Notification body is truncated rather than sent whole: observations run to
# ~300 characters and both iOS and Android silently clip long bodies anyway.
# Clipping deliberately, at a word boundary, beats being clipped mid-word.
MAX_BODY_CHARS = 160


def get_full_text(prop):
    """Concatenate every rich_text chunk. Notion splits at 2000 characters."""
    if not prop or not prop.get("rich_text"):
        return None
    return "".join(t.get("plain_text", "") for t in prop["rich_text"]) or None


def lookup_client(client_id):
    """Find a client by client_id (the title property). Returns page id and
    the stored push subscription, or (None, None)."""
    body = {
        "filter": {"property": "client_id", "title": {"equals": client_id}},
        "page_size": 1,
    }
    resp = requests.post(
        f"https://api.notion.com/v1/databases/{CLIENTS_DB}/query",
        headers=NOTION_HEADERS, json=body, timeout=15,
    )
    if resp.status_code != 200:
        return None, None
    results = resp.json().get("results", [])
    if not results:
        return None, None
    page = results[0]
    return page["id"], get_full_text(page.get("properties", {}).get("push_subscription"))


def clear_subscription(page_id):
    """Blank a dead subscription so we stop trying it every morning. The PWA
    re-subscribes and re-registers on its next load."""
    requests.patch(
        f"https://api.notion.com/v1/pages/{page_id}",
        headers=NOTION_HEADERS,
        json={"properties": {"push_subscription": {"rich_text": []}}},
        timeout=15,
    )


def truncate(text, limit=MAX_BODY_CHARS):
    if not text:
        return ""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(".,;:—-") + "…"


def main(request):
    if request.method == "OPTIONS":
        return ("", 204, {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST",
            "Access-Control-Allow-Headers": "Content-Type",
        })

    headers = {"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"}

    def out(payload, code=200):
        return (json.dumps(payload), code, headers)

    if not VAPID_PRIVATE_KEY:
        return out({"status": "skipped", "reason": "no_vapid_private_key"})

    data = request.get_json(silent=True) or {}
    client_id = data.get("client_id")
    if not client_id:
        return out({"status": "error", "reason": "missing_client_id"}, 400)

    body_text = truncate(data.get("body"))
    if not body_text:
        # Nothing worth reading means no notification. An empty push is worse
        # than none: it teaches the client that opening the app is pointless.
        return out({"status": "skipped", "reason": "empty_body"})

    page_id, sub_raw = lookup_client(client_id)
    if not page_id:
        return out({"status": "error", "reason": f"unknown_client:{client_id}"}, 404)
    if not sub_raw:
        return out({"status": "skipped", "reason": "no_subscription"})

    try:
        subscription = json.loads(sub_raw)
    except (ValueError, TypeError):
        return out({"status": "error", "reason": "subscription_not_json"}, 500)

    payload = {
        "title": data.get("title") or "MANA³",
        "body": body_text,
        "data": {
            "type": data.get("type") or "observation",
            "client_id": client_id,
            "date": data.get("date"),
        },
    }

    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_SUBJECT},
            timeout=15,
        )
        return out({"status": "sent", "client_id": client_id,
                    "chars": len(body_text)})

    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        # 404/410 mean the push service has permanently dropped this endpoint —
        # uninstalled PWA, cleared browser data, or a rotated VAPID key.
        if status in (404, 410):
            clear_subscription(page_id)
            return out({"status": "expired", "cleared": True, "http": status})
        return out({"status": "failed", "http": status, "detail": str(e)[:300]})

    except Exception as e:
        # Never surface an exception to Make: the observation is already
        # written and safe, and a failed push must not fail the scenario.
        return out({"status": "failed", "detail": str(e)[:300]})
