"""
MANA³ PWA Data API
Google Cloud Run Function. Receives client_id, returns observation history,
prompt data, and weekly briefs for the PWA to display.
"""

import json
import os
from datetime import datetime, timedelta
import requests

# Notion API configuration
NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "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")
DAILY_RECORDS_DB = "3691383d-69a0-450c-b8a1-804a80c367e2"
CLIENTS_DB = "e2bb9697-c7a5-4b9d-98be-8d93ac10cc64"
PRACTITIONER_BRIEFS_DB = "a6423d30-8432-461e-9054-5b9f91e79133"

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json"
}

# Client ID to Notion page ID mapping (hardcoded for beta)
CLIENT_MAP = {
    "MANA-TEST": "324b5258-2f9c-8045-b971-e08677181692"
}


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
    # Match [TAG] content [/TAG] patterns
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

    # Parse the brief sections
    sections = parse_brief_sections(raw_log)

    # Extract flags
    flags = []
    for i in range(1, 6):
        key = f"flag{i}"
        if key in sections:
            flag_text = sections[key]
            # Parse flag subfields
            flag = {"raw": flag_text}
            for field in ["Title", "Field", "Layer", "Urgency", "Evidence"]:
                match = __import__("re").search(
                    rf'{field}:\s*(.+?)(?=\s+(?:Title|Field|Layer|Urgency|Evidence):|$)',
                    flag_text
                )
                if match:
                    flag[field.lower()] = match.group(1).strip()
            flags.append(flag)

    # Parse client brief sections (clean escaped newlines from Make.com text parser)
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

    # Parse prompt options from double-comma format
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
# CYCLE INDICATORS
# ═══════════════════════════════════════════════

def compute_recovery_proportionality(today_data):
    """Compare yesterday's load against last night's recovery.
    Returns a value from -3 to +3.
    Negative = under-recovered relative to load.
    Positive = recovery exceeded what load demanded.
    0 = proportional."""
    w = today_data.get("wearable", {})

    # Load signal: active calories + steps (normalized to 0-100 each)
    cal = w.get("calories_active")
    steps = w.get("steps")
    if cal is None and steps is None:
        return None

    cal_norm = min(100, (cal or 0) / 8.0) if cal else None  # 800 cal = 100
    steps_norm = min(100, (steps or 0) / 120.0) if steps else None  # 12000 steps = 100

    load_vals = [v for v in [cal_norm, steps_norm] if v is not None]
    if not load_vals:
        return None
    load = sum(load_vals) / len(load_vals)

    # Recovery signal: sleep score + readiness + inverse stress
    sleep = w.get("sleep_score")
    readiness = w.get("readiness")
    stress = w.get("stress")

    rec_vals = []
    if sleep is not None and sleep > 0:
        rec_vals.append(sleep)
    if readiness is not None and readiness > 0:
        rec_vals.append(readiness)
    if stress is not None and stress > 0:
        rec_vals.append(100 - stress)  # Invert: low stress = high recovery

    if not rec_vals:
        return None
    recovery = sum(rec_vals) / len(rec_vals)

    # Difference: recovery - load, scaled to -3..+3
    diff = recovery - load  # Range roughly -100 to +100
    scaled = max(-3, min(3, diff / 33.3))
    return round(scaled, 1)


def compute_subjective_objective_alignment(today_data):
    """Compare check-in average against wearable recovery composite.
    Returns -3 to +3.
    Negative = client feels worse than wearable predicts.
    Positive = client feels better than wearable predicts.
    0 = aligned."""
    checkin = today_data.get("checkin", {})
    w = today_data.get("wearable", {})

    # Subjective: average of check-in q1-q7, normalized to 0-100
    q_vals = [checkin.get(f"q{i}") for i in range(1, 8)]
    q_vals = [v for v in q_vals if v is not None]
    if not q_vals:
        return None
    subjective = (sum(q_vals) / len(q_vals) - 1) / 6 * 100  # 1-7 scale to 0-100

    # Objective: sleep + readiness + inverse stress
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

    # Difference: subjective - objective, scaled to -3..+3
    diff = subjective - objective
    scaled = max(-3, min(3, diff / 33.3))
    return round(scaled, 1)


def compute_load_accumulation_72h(today_data, history):
    """Compute 72h rolling averages for key metrics across the 3 most recent
    COMPLETE days (yesterday, day before, day before that).

    Today's record is excluded because it's a partial-day read and would
    distort the average. The client sees these averages on the PWA and
    compares them to the values shown on their watch/Polar Flow — so the
    numbers must be a strict arithmetic mean of the raw values written to
    Notion for the 3 most recent complete days.

    Fields averaged:
    - calories_total (kcal, includes BMR) — matches Polar Flow "Total calories"
    - hrv_overnight_rmssd (ms) — matches Polar Flow overnight HRV
    - stress_proxy_normalized (0-100) — derived from Polar ANS charge
    - sleep_score_normalized (0-100) — matches Polar Flow sleep score
    """
    today_str = datetime.utcnow().strftime("%Y-%m-%d")

    # Build candidate pool: all available days, then filter out today's date.
    # This guarantees we never include a partial-day record, even if the
    # caller mis-classified today_data (e.g. S1 hasn't run yet so the most
    # recent record is yesterday).
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

    # Compute cycle indicators
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


def main(request):
    """Cloud Function entry point."""
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

    try:
        # Accept both GET params and POST body
        if request.method == "POST":
            data = request.get_json(silent=True) or {}
            client_id = data.get("client_id", "MANA-TEST")
            endpoint = data.get("endpoint", "daily")
        else:
            client_id = request.args.get("client_id", "MANA-TEST")
            endpoint = request.args.get("endpoint", "daily")

        # Look up client page ID
        client_page_id = CLIENT_MAP.get(client_id)
        if not client_page_id:
            return (json.dumps({"error": f"Unknown client: {client_id}"}), 404, headers)

        # Route to handler
        if endpoint == "weekly-brief":
            return handle_weekly_brief(request, client_id, client_page_id, headers)
        else:
            return handle_daily(request, client_id, client_page_id, headers)

    except Exception as e:
        return (json.dumps({"error": str(e)}), 500, headers)
