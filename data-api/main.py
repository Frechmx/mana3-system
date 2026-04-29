"""
MANA³ PWA Data API
Google Cloud Run Function. Receives client_id, returns observation history,
prompt data, and weekly briefs for the PWA to display.
Also handles Strava webhook events and OAuth callback.
"""

import json
import os
import time
from datetime import datetime, timedelta
import requests

# Notion API configuration
NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "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")
DAILY_RECORDS_DB = "3691383d-69a0-450c-b8a1-804a80c367e2"
CLIENTS_DB = "e2bb9697-c7a5-4b9d-98be-8d93ac10cc64"
PRACTITIONER_BRIEFS_DB = "a6423d30-8432-461e-9054-5b9f91e79133"
STRAVA_TOKENS_DB = "350b52582f9c805689f1fcd5ce318116"

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}

# Strava configuration
STRAVA_CLIENT_ID = os.environ.get("STRAVA_CLIENT_ID", "232301")
STRAVA_CLIENT_SECRET = os.environ.get("STRAVA_CLIENT_SECRET", "")
STRAVA_VERIFY_TOKEN = os.environ.get("STRAVA_VERIFY_TOKEN", "MANA3_STRAVA_VERIFY")

# Make.com S12 webhook URL (replace with actual URL after creating S12 scenario)
MAKE_S12_WEBHOOK_URL = os.environ.get("MAKE_S12_WEBHOOK_URL", "https://hook.eu1.make.com/PLACEHOLDER_S12_WEBHOOK")

# Client ID to Notion page ID mapping (hardcoded for beta)
CLIENT_MAP = {
    "MANA-TEST": "324b5258-2f9c-8045-b971-e08677181692"
}


# ═══════════════════════════════════════════════
# NOTION HELPERS (existing)
# ═══════════════════════════════════════════════

def get_text(prop):
    """Extract plain text from a Notion rich_text property."""
    if not prop or not prop.get("rich_text"):
        return None
    texts = prop.get("rich_text", [])
    if not texts:
        return None
    return texts[0].get("plain_text", None)


def get_title_text(prop):
    """Extract plain text from a Notion title property."""
    if not prop or not prop.get("title"):
        return None
    titles = prop.get("title", [])
    if not titles:
        return None
    return titles[0].get("plain_text", None)


def get_select(prop):
    """Extract value from a Notion select property."""
    if not prop or not prop.get("select"):
        return None
    return prop["select"].get("name", None)


def get_number(prop):
    """Extract value from a Notion number property."""
    if not prop:
        return None
    return prop.get("number", None)


def get_date(prop):
    """Extract date string from a Notion date property."""
    if not prop or not prop.get("date"):
        return None
    return prop["date"].get("start", None)


def get_checkbox(prop):
    """Extract boolean from a Notion checkbox property."""
    if not prop:
        return False
    return prop.get("checkbox", False)


def get_full_text(prop):
    """Extract full plain text from a Notion rich_text property (all chunks)."""
    if not prop or not prop.get("rich_text"):
        return None
    texts = prop.get("rich_text", [])
    if not texts:
        return None
    return "".join([t.get("plain_text", "") for t in texts])


# ═══════════════════════════════════════════════
# STRAVA WEBHOOK + OAUTH
# ═══════════════════════════════════════════════

def handle_strava_webhook(request, headers):
    """Handle Strava webhook: GET = subscription verification, POST = event."""

    if request.method == "GET":
        # Strava subscription verification challenge
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if mode == "subscribe" and token == STRAVA_VERIFY_TOKEN:
            # Must return hub.challenge as JSON with 200
            return (json.dumps({"hub.challenge": challenge}), 200, {
                "Content-Type": "application/json"
            })
        return ("Forbidden", 403, headers)

    if request.method == "POST":
        event = request.get_json(silent=True)
        if not event:
            return (json.dumps({"error": "No payload"}), 400, headers)

        object_type = event.get("object_type")
        aspect_type = event.get("aspect_type")
        owner_id = event.get("owner_id")
        object_id = event.get("object_id")

        # Only process activity creates
        if object_type != "activity" or aspect_type != "create":
            return (json.dumps({"status": "ignored", "reason": f"{object_type}.{aspect_type}"}), 200, headers)

        # Look up client by strava_athlete_id
        client_record = lookup_strava_client(owner_id)
        if not client_record:
            return (json.dumps({"error": f"Unknown Strava athlete: {owner_id}"}), 404, headers)

        client_id = client_record.get("client_id")
        token_page_id = client_record.get("page_id")

        # Check / refresh token
        access_token = client_record.get("access_token")
        expires_at = client_record.get("expires_at", 0)

        if time.time() > (expires_at - 300):
            # Token expired or expiring within 5 minutes — refresh
            refresh_result = refresh_strava_token(
                client_record.get("refresh_token"),
                token_page_id
            )
            if refresh_result:
                access_token = refresh_result["access_token"]
            else:
                return (json.dumps({"error": "Token refresh failed"}), 500, headers)

        # Forward enriched event to Make.com S12 webhook
        forward_payload = {
            "object_id": object_id,
            "owner_id": owner_id,
            "aspect_type": aspect_type,
            "client_id": client_id,
            "access_token": access_token
        }

        try:
            fwd_resp = requests.post(
                MAKE_S12_WEBHOOK_URL,
                json=forward_payload,
                timeout=10
            )
            return (json.dumps({
                "status": "forwarded",
                "client_id": client_id,
                "activity_id": object_id,
                "make_status": fwd_resp.status_code
            }), 200, headers)
        except Exception as e:
            return (json.dumps({"error": f"Forward failed: {str(e)}"}), 500, headers)

    return ("Method not allowed", 405, headers)


def lookup_strava_client(strava_athlete_id):
    """Look up a client in the Strava Tokens Notion DB by athlete ID."""
    body = {
        "filter": {
            "property": "strava_athlete_id",
            "number": {"equals": int(strava_athlete_id)}
        },
        "page_size": 1
    }

    resp = requests.post(
        f"https://api.notion.com/v1/databases/{STRAVA_TOKENS_DB}/query",
        headers=NOTION_HEADERS,
        json=body
    )

    if resp.status_code != 200:
        return None

    results = resp.json().get("results", [])
    if not results:
        return None

    record = results[0]
    props = record.get("properties", {})

    return {
        "page_id": record["id"],
        "client_id": get_text(props.get("client_id")),
        "strava_athlete_id": get_number(props.get("strava_athlete_id")),
        "access_token": get_text(props.get("access_token")),
        "refresh_token": get_text(props.get("refresh_token")),
        "expires_at": get_number(props.get("expires_at")) or 0,
        "scopes": get_text(props.get("scopes")),
    }


def refresh_strava_token(refresh_token, token_page_id):
    """Refresh a Strava OAuth token and update Notion."""
    resp = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    })

    if resp.status_code != 200:
        return None

    token_data = resp.json()
    new_access = token_data.get("access_token")
    new_refresh = token_data.get("refresh_token")
    new_expires = token_data.get("expires_at")

    # Update Notion record
    update_body = {
        "properties": {
            "access_token": {
                "rich_text": [{"text": {"content": new_access}}]
            },
            "refresh_token": {
                "rich_text": [{"text": {"content": new_refresh}}]
            },
            "expires_at": {
                "number": new_expires
            },
            "last_refreshed": {
                "date": {"start": datetime.utcnow().isoformat() + "Z"}
            }
        }
    }

    requests.patch(
        f"https://api.notion.com/v1/pages/{token_page_id}",
        headers=NOTION_HEADERS,
        json=update_body
    )

    return {
        "access_token": new_access,
        "refresh_token": new_refresh,
        "expires_at": new_expires
    }


def handle_strava_oauth_callback(request, headers):
    """Handle Strava OAuth callback: exchange code for tokens, store in Notion."""
    code = request.args.get("code")
    scope = request.args.get("scope", "")

    if not code:
        return (json.dumps({"error": "Missing authorization code"}), 400, headers)

    # Exchange code for tokens
    resp = requests.post("https://www.strava.com/oauth/token", data={
        "client_id": STRAVA_CLIENT_ID,
        "client_secret": STRAVA_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code"
    })

    if resp.status_code != 200:
        return (json.dumps({"error": "Token exchange failed", "detail": resp.text}), 500, headers)

    token_data = resp.json()
    athlete = token_data.get("athlete", {})
    athlete_id = athlete.get("id")
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_at = token_data.get("expires_at")

    # Check if this athlete already exists in Strava Tokens DB
    existing = lookup_strava_client(athlete_id)

    if existing:
        # Update existing record
        update_body = {
            "properties": {
                "access_token": {
                    "rich_text": [{"text": {"content": access_token}}]
                },
                "refresh_token": {
                    "rich_text": [{"text": {"content": refresh_token}}]
                },
                "expires_at": {
                    "number": expires_at
                },
                "scopes": {
                    "rich_text": [{"text": {"content": scope}}]
                },
                "last_refreshed": {
                    "date": {"start": datetime.utcnow().isoformat() + "Z"}
                }
            }
        }
        requests.patch(
            f"https://api.notion.com/v1/pages/{existing['page_id']}",
            headers=NOTION_HEADERS,
            json=update_body
        )
        client_id = existing.get("client_id", "UNKNOWN")
    else:
        # Create new record — client_id must be set manually after
        client_id = "PENDING"
        create_body = {
            "parent": {"database_id": STRAVA_TOKENS_DB},
            "properties": {
                "token_id": {
                    "title": [{"text": {"content": f"STRAVA_{athlete_id}"}}]
                },
                "client_id": {
                    "rich_text": [{"text": {"content": client_id}}]
                },
                "strava_athlete_id": {
                    "number": athlete_id
                },
                "access_token": {
                    "rich_text": [{"text": {"content": access_token}}]
                },
                "refresh_token": {
                    "rich_text": [{"text": {"content": refresh_token}}]
                },
                "expires_at": {
                    "number": expires_at
                },
                "scopes": {
                    "rich_text": [{"text": {"content": scope}}]
                },
                "last_refreshed": {
                    "date": {"start": datetime.utcnow().isoformat() + "Z"}
                }
            }
        }
        requests.post(
            "https://api.notion.com/v1/pages",
            headers=NOTION_HEADERS,
            json=create_body
        )

    # Return a user-friendly success page
    athlete_name = f"{athlete.get('firstname', '')} {athlete.get('lastname', '')}".strip()
    html = f"""<!DOCTYPE html>
<html><head><title>MANA³ — Strava Connected</title>
<style>
body {{ font-family: Inter, sans-serif; background: #0A0A0A; color: #F5F5F0;
       display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
.card {{ text-align: center; max-width: 400px; padding: 40px; }}
h1 {{ font-size: 24px; margin: 0 0 8px; }}
p {{ color: #888; font-size: 14px; line-height: 1.6; }}
.check {{ font-size: 48px; margin-bottom: 16px; }}
.id {{ font-family: monospace; color: #3A86FF; }}
</style></head>
<body><div class="card">
<div class="check">✓</div>
<h1>Strava Connected</h1>
<p>Welcome, <strong>{athlete_name}</strong>.<br>
Athlete ID: <span class="id">{athlete_id}</span><br>
Client: <span class="id">{client_id}</span></p>
<p>{'Tokens updated.' if existing else 'New record created — set client_id in Notion.'}</p>
</div></body></html>"""

    return (html, 200, {"Content-Type": "text/html"})


# ═══════════════════════════════════════════════
# EXISTING HANDLERS (unchanged)
# ═══════════════════════════════════════════════

def query_daily_records(client_page_id, days=8):
    """Fetch recent daily records for a client from Notion."""
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")

    body = {
        "filter": {
            "and": [
                {
                    "property": "client",
                    "relation": {"contains": client_page_id}
                },
                {
                    "property": "date",
                    "date": {"on_or_after": since}
                }
            ]
        },
        "sorts": [
            {"property": "date", "direction": "descending"}
        ],
        "page_size": days
    }

    resp = requests.post(
        f"https://api.notion.com/v1/databases/{DAILY_RECORDS_DB}/query",
        headers=NOTION_HEADERS,
        json=body
    )

    if resp.status_code != 200:
        return []

    return resp.json().get("results", [])


def query_latest_brief(client_page_id):
    """Fetch the most recent weekly brief for a client from Notion."""
    body = {
        "filter": {
            "property": "client",
            "relation": {"contains": client_page_id}
        },
        "sorts": [
            {"property": "week_end", "direction": "descending"}
        ],
        "page_size": 1
    }

    resp = requests.post(
        f"https://api.notion.com/v1/databases/{PRACTITIONER_BRIEFS_DB}/query",
        headers=NOTION_HEADERS,
        json=body
    )

    if resp.status_code != 200:
        return None

    results = resp.json().get("results", [])
    if not results:
        return None

    return results[0]


def parse_brief_sections(raw_text):
    """Parse the bracketed brief format into structured sections."""
    if not raw_text:
        return {}

    sections = {}
    import re
    pattern = r'\[(\w+\d*)\]\s*(.*?)\s*\[/\1\]'
    matches = re.findall(pattern, raw_text, re.DOTALL)

    for tag, content in matches:
        tag_lower = tag.lower()
        sections[tag_lower] = content.strip()

    return sections


def extract_brief(record):
    """Extract relevant fields from a Notion Practitioner Brief record."""
    props = record.get("properties", {})

    raw_log = get_full_text(props.get("observation_log"))
    raw_snapshot = get_full_text(props.get("data_snapshot"))

    sections = parse_brief_sections(raw_log)

    flags = []
    for i in range(1, 6):
        key = f"flag{i}"
        if key in sections:
            flag_text = sections[key]
            flag = {"raw": flag_text}
            for field in ["Title", "Field", "Layer", "Urgency", "Evidence"]:
                match = __import__("re").search(
                    rf'{field}:\s*(.+?)(?=\s+(?:Title|Field|Layer|Urgency|Evidence):|$)',
                    flag_text
                )
                if match:
                    flag[field.lower()] = match.group(1).strip()
            flags.append(flag)

    raw_client_brief = get_full_text(props.get("client_brief"))
    if raw_client_brief:
        raw_client_brief = raw_client_brief.replace("\\n", "\n")
    client_sections = parse_brief_sections(raw_client_brief)

    return {
        "week_id": get_title_text(props.get("brief_id")),
        "week_start": get_date(props.get("week_start")),
        "week_end": get_date(props.get("week_end")),
        "status": sections.get("status"),
        "flags": flags,
        "voice_summary": sections.get("voice"),
        "wearables_summary": sections.get("wearables"),
        "recommendations": sections.get("recommendations"),
        "data_quality": sections.get("data_quality") or raw_snapshot,
        "client_brief": {
            "overview": client_sections.get("overview"),
            "structure": client_sections.get("structure"),
            "electricity": client_sections.get("electricity"),
            "energy": client_sections.get("energy"),
            "regulation": client_sections.get("regulation"),
            "highlight": client_sections.get("highlight"),
        } if client_sections else None,
    }


def extract_record(record):
    """Extract relevant fields from a Notion Daily Record."""
    props = record.get("properties", {})

    options_raw = get_text(props.get("prompt_options"))
    options = []
    if options_raw:
        options = [o.strip() for o in options_raw.split(",,") if o.strip()]

    return {
        "date": get_date(props.get("date")),
        "observation": get_text(props.get("observation_text")),
        "observation_type": get_select(props.get("observation_type")),
        "observation_field": get_select(props.get("observation_primary_field")),
        "observation_layer": get_select(props.get("observation_primary_layer")),
        "band": get_select(props.get("coherence_band")),
        "overall_score": get_number(props.get("overall_score")),
        "layer_scores": {
            "structure": get_number(props.get("layer_structure")),
            "electricity": get_number(props.get("layer_electricity")),
            "energy": get_number(props.get("layer_energy")),
            "regulation": get_number(props.get("layer_regulation")),
        },
        "voice_received": get_checkbox(props.get("voice_received")),
        "prompt": {
            "question": get_text(props.get("prompt_question")),
            "options": options,
            "answer": get_text(props.get("prompt_answer")),
            "fired": get_checkbox(props.get("prompt_fired")),
        } if get_checkbox(props.get("prompt_fired")) or get_text(props.get("prompt_question")) else None,
        "wearable": {
            "sleep_score": get_number(props.get("sleep_score_normalized")),
            "hrv": get_number(props.get("hrv_overnight_rmssd")),
            "rhr": get_number(props.get("resting_heart_rate")),
            "stress": get_number(props.get("stress_proxy_normalized")),
            "steps": get_number(props.get("steps")),
            "calories_active": get_number(props.get("calories_active")),
            "calories_total": get_number(props.get("calories_total")),
            "readiness": get_number(props.get("readiness_score_normalized")),
            "sleep_duration": get_number(props.get("sleep_duration_minutes")),
            "respiration": get_number(props.get("respiration_rate_avg")),
        },
        "checkin": {
            "q1": get_number(props.get("checkin_q1")),
            "q2": get_number(props.get("checkin_q2")),
            "q3": get_number(props.get("checkin_q3")),
            "q4": get_number(props.get("checkin_q4")),
            "q5": get_number(props.get("checkin_q5")),
            "q6": get_number(props.get("checkin_q6")),
            "q7": get_number(props.get("checkin_q7")),
        },
    }


# ═══════════════════════════════════════════════
# CYCLE INDICATORS (unchanged)
# ═══════════════════════════════════════════════

def compute_recovery_proportionality(today_data):
    """Compare yesterday's load against last night's recovery."""
    w = today_data.get("wearable", {})

    cal = w.get("calories_active")
    steps = w.get("steps")
    if cal is None and steps is None:
        return None

    cal_norm = min(100, (cal or 0) / 8.0) if cal else None
    steps_norm = min(100, (steps or 0) / 120.0) if steps else None

    load_vals = [v for v in [cal_norm, steps_norm] if v is not None]
    if not load_vals:
        return None
    load = sum(load_vals) / len(load_vals)

    sleep = w.get("sleep_score")
    readiness = w.get("readiness")
    stress = w.get("stress")

    rec_vals = []
    if sleep is not None and sleep > 0:
        rec_vals.append(sleep)
    if readiness is not None and readiness > 0:
        rec_vals.append(readiness)
    if stress is not None and stress > 0:
        rec_vals.append(100 - stress)

    if not rec_vals:
        return None
    recovery = sum(rec_vals) / len(rec_vals)

    diff = recovery - load
    scaled = max(-3, min(3, diff / 33.3))
    return round(scaled, 1)


def compute_subjective_objective_alignment(today_data):
    """Compare check-in average against wearable recovery composite."""
    checkin = today_data.get("checkin", {})
    w = today_data.get("wearable", {})

    q_vals = [checkin.get(f"q{i}") for i in range(1, 8)]
    q_vals = [v for v in q_vals if v is not None]
    if not q_vals:
        return None
    subjective = (sum(q_vals) / len(q_vals) - 1) / 6 * 100

    sleep = w.get("sleep_score")
    readiness = w.get("readiness")
    stress = w.get("stress")

    obj_vals = []
    if sleep is not None and sleep > 0:
        obj_vals.append(sleep)
    if readiness is not None and readiness > 0:
        obj_vals.append(readiness)
    if stress is not None and stress > 0:
        obj_vals.append(100 - stress)

    if not obj_vals:
        return None
    objective = sum(obj_vals) / len(obj_vals)

    diff = subjective - objective
    scaled = max(-3, min(3, diff / 33.3))
    return round(scaled, 1)


def compute_load_accumulation_72h(today_data, history):
    """Compute 72h rolling averages for key metrics."""
    today_str = datetime.utcnow().strftime("%Y-%m-%d")

    candidates = []
    if today_data:
        candidates.append(today_data)
    if history:
        candidates.extend(history)

    complete_days = [d for d in candidates if d.get("date") != today_str]
    days = complete_days[:3]

    cals = []
    hrvs = []
    stresses = []
    sleeps = []

    for day in days:
        w = day.get("wearable", {})
        cal = w.get("calories_total")
        hrv = w.get("hrv")
        stress = w.get("stress")
        sleep = w.get("sleep_score")

        if cal is not None and cal > 0:
            cals.append(cal)
        if hrv is not None and hrv > 0:
            hrvs.append(hrv)
        if stress is not None and stress > 0:
            stresses.append(stress)
        if sleep is not None and sleep > 0:
            sleeps.append(sleep)

    if not cals and not hrvs and not stresses and not sleeps:
        return None

    return {
        "avg_cal": round(sum(cals) / len(cals)) if cals else None,
        "avg_hrv": round(sum(hrvs) / len(hrvs)) if hrvs else None,
        "avg_stress": round(sum(stresses) / len(stresses)) if stresses else None,
        "avg_sleep": round(sum(sleeps) / len(sleeps)) if sleeps else None,
        "days_with_data": max(len(cals), len(hrvs), len(stresses), len(sleeps)),
    }


def handle_daily(request, client_id, client_page_id, headers):
    """Handle daily records request."""
    records = query_daily_records(client_page_id, days=8)

    if not records:
        return (json.dumps({
            "client_id": client_id,
            "today": None,
            "history": [],
            "cycle_indicators": None,
        }), 200, headers)

    all_days = [extract_record(r) for r in records]

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    today_data = None
    history = []

    for day in all_days:
        if day["date"] == today_str:
            today_data = day
        else:
            history.append(day)

    if today_data is None and all_days:
        today_data = all_days[0]
        history = all_days[1:]

    cycle_indicators = None
    if today_data:
        rp = compute_recovery_proportionality(today_data)
        soa = compute_subjective_objective_alignment(today_data)
        load_72h = compute_load_accumulation_72h(today_data, history)

        cycle_indicators = {
            "recovery_proportionality": rp,
            "subjective_objective_alignment": soa,
            "load_72h": load_72h,
        }

    return (json.dumps({
        "client_id": client_id,
        "today": today_data,
        "history": history[:7],
        "cycle_indicators": cycle_indicators,
    }), 200, headers)


def handle_weekly_brief(request, client_id, client_page_id, headers):
    """Handle weekly brief request."""
    record = query_latest_brief(client_page_id)

    if not record:
        return (json.dumps({
            "client_id": client_id,
            "brief": None
        }), 200, headers)

    brief = extract_brief(record)

    return (json.dumps({
        "client_id": client_id,
        "brief": brief
    }), 200, headers)


# ═══════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════

def main(request):
    """Cloud Function entry point with path-based routing."""

    # Handle CORS preflight
    if request.method == "OPTIONS":
        return ("", 204, {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "3600",
        })

    headers = {
        "Access-Control-Allow-Origin": "*",
        "Content-Type": "application/json",
    }

    path = request.path or "/"

    try:
        # ── Strava routes (path-based) ──
        if path == "/strava/webhook":
            return handle_strava_webhook(request, headers)

        if path == "/strava/callback":
            return handle_strava_oauth_callback(request, headers)

        # ── Existing API routes (param-based) ──
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            client_id = data.get("client_id", "MANA-TEST")
            endpoint = data.get("endpoint", "daily")
        else:
            client_id = request.args.get("client_id", "MANA-TEST")
            endpoint = request.args.get("endpoint", "daily")

        client_page_id = CLIENT_MAP.get(client_id)
        if not client_page_id:
            return (json.dumps({"error": f"Unknown client: {client_id}"}), 404, headers)

        if endpoint == "weekly-brief":
            return handle_weekly_brief(request, client_id, client_page_id, headers)
        else:
            return handle_daily(request, client_id, client_page_id, headers)

    except Exception as e:
        return (json.dumps({"error": str(e)}), 500, headers)
