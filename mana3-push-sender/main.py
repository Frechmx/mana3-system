"""
MANA³ Push Sender
Google Cloud Function `mana3-push-sender`. Sends one Web Push notification.

Called by S4 immediately after the observation is written to the Daily Record,
so the notification carries the one line worth reading rather than a generic
"your data is ready" nudge.

v2 changes:
  - The subscription is looked up in Notion here instead of being passed in.
    The caller only needs client_id, so S4 doesn't need an extra Notion module
    and a fragile mapping of a JSON blob through Make.
  - Dead endpoints (HTTP 404/410) blank push_subscription in Notion, so a
    stale endpoint isn't retried every morning forever.
  - Body is truncated at a word boundary; iOS and Android clip long bodies
    silently and mid-word.
  - An empty body sends nothing. A blank notification teaches the client that
    opening the app is pointless — the one habit that can't be rebuilt.
  - A `subscription` in the request body still wins, so the old contract keeps
    working and this can be tested without touching Notion.

POST body (new contract):
  {"client_id": "MANA-TEST", "body": "<observation>",
   "type": "observation", "date": "2026-08-18", "title": "MANA³"}
"""

import functions_framework
import json
import os
import requests
from pywebpush import webpush, WebPushException

VAPID_PRIVATE_KEY = os.environ.get(
    "VAPID_PRIVATE_KEY", "W44xkMHpJV0mqoeKKEqACsSNllQA6G4HE9IlziyHY5E"
)
VAPID_CLAIMS = {"sub": os.environ.get("VAPID_SUBJECT", "mailto:mana3system@gmail.com")}

NOTION_API_KEY = os.environ.get(
    "NOTION_API_KEY", "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"
)
CLIENTS_DB = "e2bb9697-c7a5-4b9d-98be-8d93ac10cc64"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

MAX_BODY_CHARS = 160


def parse_subscription(raw):
    """Parse a stored subscription that may not be valid JSON.

    The PWA sends JSON.stringify(sub) — double quotes, correct. But S8b
    re-serialises it on the way into Notion and stores a single-quoted object
    repr instead ({'endpoint':'https://…'}), which json.loads rejects. Rather
    than depend on that scenario being fixed, accept both forms here: this
    function is the last thing standing between a working notification and a
    silent daily failure.

    Returns a dict, or None if it genuinely can't be read.
    """
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw

    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        pass

    # Python-style repr: 'key': 'value', possibly with None/True/False.
    try:
        import ast
        parsed = ast.literal_eval(raw)
        if isinstance(parsed, dict):
            return parsed
    except (ValueError, SyntaxError, TypeError):
        pass

    # Last resort: JS-style repr with null/true/false, which literal_eval
    # can't read. Swap the quote style and retry as JSON.
    try:
        return json.loads(raw.replace("'", '"'))
    except (ValueError, TypeError):
        return None


def _full_text(prop):
    """Concatenate every rich_text chunk — Notion splits at 2000 characters."""
    if not prop or not prop.get("rich_text"):
        return None
    return "".join(t.get("plain_text", "") for t in prop["rich_text"]) or None


def lookup_client(client_id):
    """Find a client by client_id (the title property).
    Returns (page_id, subscription_json_string) or (None, None)."""
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
    return page["id"], _full_text(page.get("properties", {}).get("push_subscription"))


def clear_subscription(page_id):
    """Blank a dead subscription. The PWA re-subscribes on its next load."""
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
    return text[:limit].rsplit(" ", 1)[0].rstrip(".,;:—-") + "…"


@functions_framework.http
def send_push(request):
    if request.method == "OPTIONS":
        return ("", 204, {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "3600",
        })

    headers = {"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"}

    def out(payload, code=200):
        return (json.dumps(payload), code, headers)

    try:
        payload = request.get_json(silent=True) or {}
        client_id = payload.get("client_id")

        body_text = truncate(payload.get("body"))
        if not body_text:
            return out({"status": "skipped", "reason": "empty_body"})

        # Old contract: an explicit subscription still wins.
        subscription = payload.get("subscription")
        page_id = None

        if not subscription:
            if not client_id:
                return out({"status": "error",
                            "reason": "no_subscription_and_no_client_id"}, 400)
            page_id, sub_raw = lookup_client(client_id)
            if not page_id:
                return out({"status": "error",
                            "reason": f"unknown_client:{client_id}"}, 404)
            if not sub_raw:
                return out({"status": "skipped", "reason": "no_subscription"})
            subscription = parse_subscription(sub_raw)
            if not subscription or not subscription.get("endpoint"):
                return out({"status": "error",
                            "reason": "subscription_unreadable",
                            "head": str(sub_raw)[:60]}, 500)

        notification = json.dumps({
            "title": payload.get("title") or "MANA³",
            "body": body_text,
            "data": payload.get("data") or {
                "type": payload.get("type") or "observation",
                "client_id": client_id,
                "date": payload.get("date"),
            },
        })

        webpush(
            subscription_info=subscription,
            data=notification,
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims=dict(VAPID_CLAIMS),
            timeout=15,
        )
        return out({"status": "sent", "client_id": client_id, "chars": len(body_text)})

    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        # 404/410: the push service has permanently dropped this endpoint —
        # PWA uninstalled, browser data cleared, or the key rotated.
        if status in (404, 410) and page_id:
            clear_subscription(page_id)
            return out({"status": "expired", "cleared": True, "http": status})
        return out({"status": "failed", "http": status, "detail": str(e)[:300]})

    except Exception as e:
        # Never fail the scenario. The observation is already written and safe.
        return out({"status": "failed", "detail": str(e)[:300]})


# Some deployments of this function are configured with `main` as the entry
# point rather than `send_push`. Alias so either setting works.
main = send_push
