"""
MANA³ PWA Data API
Google Cloud Run Function. Receives client_id, returns observation history,
prompt data, weekly briefs, progress trends, and personal baselines.

`data_state` is COMPUTED here, not read from Notion. See data_state.py for why.
"""

import json
import os
import re
from datetime import date as Date
from datetime import datetime, timedelta

import requests

import data_state

# ── Notion ──────────────────────────────────────────────────────────────────
# One place, env only. No fallback literal: a missing token must fail loudly
# rather than silently run on a hardcoded string nobody remembers to rotate.
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")

DAILY_RECORDS_DB = "3691383d-69a0-450c-b8a1-804a80c367e2"
CLIENTS_DB = "e2bb9697-c7a5-4b9d-98be-8d93ac10cc64"
PRACTITIONER_BRIEFS_DB = "a6423d30-8432-461e-9054-5b9f91e79133"

SESSIONS_DB = "37cb52582f9c80659efffd638696cb71"
SESSION_SETS_DB = "37cb52582f9c800cada5f625527b0c88"
ANTHROPOMETRICS_DB = "a98b52582f9c82e38a8c015a616f0174"

MIN_SESSIONS_FOR_TREND = 2

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}

CLIENT_MAP = {
    "MANA-TEST": "324b5258-2f9c-8045-b971-e08677181692",
}


def _today_str():
    """Client-local date. Never datetime.utcnow() — see data_state.today_local."""
    return data_state.today_str()


def _days_ago_str(n):
    return (data_state.today_local() - timedelta(days=n)).isoformat()


# ═══════════════════════════════════════════════
# NOTION HELPERS
# ═══════════════════════════════════════════════

def get_text(prop):
    if not prop or not prop.get("rich_text"):
        return None
    texts = prop.get("rich_text", [])
    return texts[0].get("plain_text", None) if texts else None


def get_title_text(prop):
    if not prop or not prop.get("title"):
        return None
    titles = prop.get("title", [])
    return titles[0].get("plain_text", None) if titles else None


def get_select(prop):
    if not prop or not prop.get("select"):
        return None
    return prop["select"].get("name", None)


def get_number(prop):
    if not prop:
        return None
    return prop.get("number", None)


def get_date(prop):
    if not prop or not prop.get("date"):
        return None
    return prop["date"].get("start", None)


def get_checkbox(prop):
    if not prop:
        return False
    return prop.get("checkbox", False)


def get_full_text(prop):
    if not prop or not prop.get("rich_text"):
        return None
    texts = prop.get("rich_text", [])
    if not texts:
        return None
    return "".join([t.get("plain_text", "") for t in texts])


# ═══════════════════════════════════════════════
# QUERIES
# ═══════════════════════════════════════════════

def query_daily_records(client_page_id, days=8):
    since = _days_ago_str(days)
    body = {
        "filter": {
            "and": [
                {"property": "client", "relation": {"contains": client_page_id}},
                {"property": "date", "date": {"on_or_after": since}},
            ]
        },
        "sorts": [{"property": "date", "direction": "descending"}],
        "page_size": days,
    }
    resp = requests.post(
        f"https://api.notion.com/v1/databases/{DAILY_RECORDS_DB}/query",
        headers=NOTION_HEADERS, json=body,
    )
    if resp.status_code != 200:
        return []
    return resp.json().get("results", [])


def query_daily_records_since(client_page_id, since_date, page_cap=3):
    """All daily records on/after since_date, newest first, paginated."""
    filt = {
        "and": [
            {"property": "client", "relation": {"contains": client_page_id}},
            {"property": "date", "date": {"on_or_after": since_date}},
        ]
    }
    out, cursor = [], None
    for _ in range(page_cap):
        body = {
            "filter": filt,
            "sorts": [{"property": "date", "direction": "descending"}],
            "page_size": 100,
        }
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(
            f"https://api.notion.com/v1/databases/{DAILY_RECORDS_DB}/query",
            headers=NOTION_HEADERS, json=body,
        )
        if resp.status_code != 200:
            break
        data = resp.json()
        out.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return out


def query_latest_brief(client_page_id):
    body = {
        "filter": {"property": "client", "relation": {"contains": client_page_id}},
        "sorts": [{"property": "week_end", "direction": "descending"}],
        "page_size": 1,
    }
    resp = requests.post(
        f"https://api.notion.com/v1/databases/{PRACTITIONER_BRIEFS_DB}/query",
        headers=NOTION_HEADERS, json=body,
    )
    if resp.status_code != 200:
        return None
    results = resp.json().get("results", [])
    return results[0] if results else None


# ═══════════════════════════════════════════════
# EXTRACTION
# ═══════════════════════════════════════════════

def parse_brief_sections(raw_text):
    if not raw_text:
        return {}
    sections = {}
    pattern = r'\[(\w+\d*)\]\s*(.*?)\s*\[/\1\]'
    for tag, content in re.findall(pattern, raw_text, re.DOTALL):
        sections[tag.lower()] = content.strip()
    return sections


def extract_brief(record):
    props = record.get("properties", {})
    raw_log = get_full_text(props.get("observation_log"))
    raw_snapshot = get_full_text(props.get("data_snapshot"))
    sections = parse_brief_sections(raw_log)

    flags = []
    for i in range(1, 6):
        key = f"flag{i}"
        if key not in sections:
            continue
        flag_text = sections[key]
        flag = {"raw": flag_text}
        for field in ("Title", "Field", "Layer", "Urgency", "Evidence"):
            m = re.search(
                rf'{field}:\s*(.+?)(?=\s+(?:Title|Field|Layer|Urgency|Evidence):|$)',
                flag_text,
            )
            if m:
                flag[field.lower()] = m.group(1).strip()
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
    """Extract a Notion Daily Record.

    Note: `data_state` is deliberately NOT read from Notion. That column is a
    stale snapshot; the live value is computed in handle_daily.
    """
    props = record.get("properties", {})

    options_raw = get_text(props.get("prompt_options"))
    options = [o.strip() for o in options_raw.split(",,") if o.strip()] if options_raw else []

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
        "checkin": {f"q{i}": get_number(props.get(f"checkin_q{i}")) for i in range(1, 8)},
    }


def empty_record(day_str):
    """A record shape for a day with no Notion row yet.

    Previously handle_daily fell back to `all_days[0]` when today's record was
    missing — presenting YESTERDAY's row as today. That is the same carry-forward
    class of bug as the stale score, just in the transport layer.
    """
    return {
        "date": day_str,
        "observation": None,
        "observation_type": None,
        "observation_field": None,
        "observation_layer": None,
        "band": None,
        "overall_score": None,
        "layer_scores": {"structure": None, "electricity": None,
                         "energy": None, "regulation": None},
        "voice_received": False,
        "prompt": None,
        "wearable": {k: None for k in (
            "sleep_score", "hrv", "rhr", "stress", "steps", "calories_active",
            "calories_total", "readiness", "sleep_duration", "respiration")},
        "checkin": {f"q{i}": None for i in range(1, 8)},
    }


# ═══════════════════════════════════════════════
# CYCLE INDICATORS
# ═══════════════════════════════════════════════

def compute_recovery_proportionality(today_data):
    w = today_data.get("wearable", {})
    cal, steps = w.get("calories_active"), w.get("steps")
    if cal is None and steps is None:
        return None

    cal_norm = min(100, (cal or 0) / 8.0) if cal else None
    steps_norm = min(100, (steps or 0) / 120.0) if steps else None
    load_vals = [v for v in (cal_norm, steps_norm) if v is not None]
    if not load_vals:
        return None
    load = sum(load_vals) / len(load_vals)

    sleep, readiness, stress = w.get("sleep_score"), w.get("readiness"), w.get("stress")
    rec_vals = []
    if sleep:
        rec_vals.append(sleep)
    if readiness:
        rec_vals.append(readiness)
    if stress:
        rec_vals.append(100 - stress)
    if not rec_vals:
        return None
    recovery = sum(rec_vals) / len(rec_vals)

    return round(max(-3, min(3, (recovery - load) / 33.3)), 1)


def compute_subjective_objective_alignment(today_data):
    checkin = today_data.get("checkin", {})
    w = today_data.get("wearable", {})

    q_vals = [checkin.get(f"q{i}") for i in range(1, 8)]
    q_vals = [v for v in q_vals if v is not None]
    if not q_vals:
        return None
    subjective = (sum(q_vals) / len(q_vals) - 1) / 6 * 100

    sleep, readiness, stress = w.get("sleep_score"), w.get("readiness"), w.get("stress")
    obj_vals = []
    if sleep:
        obj_vals.append(sleep)
    if readiness:
        obj_vals.append(readiness)
    if stress:
        obj_vals.append(100 - stress)
    if not obj_vals:
        return None
    objective = sum(obj_vals) / len(obj_vals)

    return round(max(-3, min(3, (subjective - objective) / 33.3)), 1)


def compute_load_accumulation_72h(today_data, history):
    today = _today_str()
    candidates = ([today_data] if today_data else []) + (history or [])
    days = [d for d in candidates if d.get("date") != today][:3]

    cals, hrvs, stresses, sleeps = [], [], [], []
    for day in days:
        w = day.get("wearable", {})
        if w.get("calories_total"):
            cals.append(w["calories_total"])
        if w.get("hrv"):
            hrvs.append(w["hrv"])
        if w.get("stress"):
            stresses.append(w["stress"])
        if w.get("sleep_score"):
            sleeps.append(w["sleep_score"])

    if not any((cals, hrvs, stresses, sleeps)):
        return None

    return {
        "avg_cal": round(sum(cals) / len(cals)) if cals else None,
        "avg_hrv": round(sum(hrvs) / len(hrvs)) if hrvs else None,
        "avg_stress": round(sum(stresses) / len(stresses)) if stresses else None,
        "avg_sleep": round(sum(sleeps) / len(sleeps)) if sleeps else None,
        "days_with_data": max(len(cals), len(hrvs), len(stresses), len(sleeps)),
    }


# ═══════════════════════════════════════════════
# DAILY
# ═══════════════════════════════════════════════

def handle_daily(request, client_id, client_page_id, headers):
    records = query_daily_records(client_page_id, days=8)
    all_days = [extract_record(r) for r in records]

    today = _today_str()
    today_data = next((d for d in all_days if d.get("date") == today), None)
    history = [d for d in all_days if d.get("date") != today]

    # No row yet is a legitimate state, not a reason to show yesterday as today.
    if today_data is None:
        today_data = empty_record(today)

    st = data_state.compute_from_extracted(today_data, Date.fromisoformat(today))
    today_data.update(st.to_dict())
    today_data["message"] = data_state.client_message(st)

    # A score derived from an incomplete record is a different number wearing
    # the same clothes. Refuse to serve it.
    if not st.show_score:
        today_data["overall_score"] = None
        today_data["layer_scores"] = None
        today_data["band"] = None

    # History days: state computed too, so the PWA never renders a stale label.
    for d in history:
        if not d.get("date"):
            continue
        hst = data_state.compute_from_extracted(d, Date.fromisoformat(d["date"]))
        d.update(hst.to_dict())
        if not hst.show_score:
            d["overall_score"] = None
            d["layer_scores"] = None
            d["band"] = None

    cycle_indicators = {
        "recovery_proportionality": compute_recovery_proportionality(today_data),
        "subjective_objective_alignment": compute_subjective_objective_alignment(today_data),
        "load_72h": compute_load_accumulation_72h(today_data, history),
    }

    return (json.dumps({
        "client_id": client_id,
        "today": today_data,
        "history": history[:7],
        "cycle_indicators": cycle_indicators,
    }), 200, headers)


def handle_weekly_brief(request, client_id, client_page_id, headers):
    record = query_latest_brief(client_page_id)
    if not record:
        return (json.dumps({"client_id": client_id, "brief": None}), 200, headers)
    return (json.dumps({
        "client_id": client_id,
        "brief": extract_brief(record),
    }), 200, headers)


# ═══════════════════════════════════════════════
# BASELINES
# ═══════════════════════════════════════════════
#
# Four physiologically-primary metrics, baselined WITHIN the client. No vendor
# composites (sleep score, readiness, recovery %, Body Battery, strain): those
# are already normalised against the vendor's own reference, so a percentile of
# a percentile adds noise, and they are the one class of number that does not
# survive a device switch.

# No record before this date is trustworthy: sleep dating, activity dating, the
# exercise capture window and the zero-as-absent resolver were all wrong before
# it. A median computed over earlier days would be a median of known bugs.
CLEAN_DATA_FLOOR = os.environ.get("CLEAN_DATA_FLOOR", "2026-08-10")

# ── Recovery pool ───────────────────────────────────────────────────────────
# Records before CLEAN_DATA_FLOOR carry the penultimate-index bug: one night's
# reading was stamped onto every following day until the next sync. The
# repetition is the bug — but each DISTINCT tuple is a real Polar night.
#
# A baseline is distributional, not temporal: median and IQR describe the set of
# values this client produces, and mis-dating changes which day a value is filed
# under, not what the value was. Collapsing runs of identical tuples therefore
# recovers usable nights without importing the bug.
#
# Baselines ONLY. Anything time-ordered — trends, week-on-week, trajectories —
# must still use CLEAN_DATA_FLOOR: recovered rows have no trustworthy date.
BASELINE_RECOVERY_MODE = os.environ.get("BASELINE_RECOVERY_MODE", "1") == "1"
BASELINE_RECOVERY_FLOOR = os.environ.get("BASELINE_RECOVERY_FLOOR", "2026-07-10")
BASELINE_RECOVERY_WINDOW_DAYS = 45

# Per-client override for a mid-protocol device change. A Garmin baseline can't
# be carried onto Polar values. Key = client_id, value = ISO date of the switch.
DEVICE_SWITCH_FLOOR = {}

BASELINE_WINDOW_DAYS = 28
BASELINE_MIN_DAYS = 14
BASELINE_READY_MIN_METRICS = 3

# Below this many observations, median/IQR are not published at all. A "median"
# of two points is their mean — a number that looks like a statistic and isn't.
BASELINE_MIN_STATS = 5

BASELINE_METRICS = [
    {"key": "sleep_duration", "label": "Sleep", "unit": "min",
     "direction": "higher_is_better", "source": "overnight"},
    {"key": "hrv", "label": "HRV", "unit": "ms",
     "direction": "higher_is_better", "source": "overnight"},
    {"key": "rhr", "label": "Resting heart rate", "unit": "bpm",
     "direction": "lower_is_better", "source": "overnight"},
    {"key": "respiration", "label": "Breathing rate", "unit": "br/min",
     "direction": "lower_is_better", "source": "overnight"},
]
# steps was removed after evidence it is dominated by wear time, not behaviour:
# 17,269 on 9 Aug against 1,100 on 12 Aug, both post-backfill. The chip would
# report how long the watch was worn while reading as a judgement about effort.

# Days in these states never enter the pool. Sourced from data_state so the
# rule cannot drift out of sync with the state machine — the old hardcoded
# {"no_sync","stale"} silently matched nothing once states were renamed.
EXCLUDED_DATA_STATES = set(data_state.NON_ANALYSABLE_STATES) | {"no_sync", "stale"}


def _clean_value(v):
    """Zero counts as absent. S1/S3 write 0 for empty Notion numbers, and none
    of these metrics has a physiologically real zero on a day the device was worn."""
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if v == 0 else v


def _quantile(sorted_vals, q):
    """Linear-interpolation quantile (type 7). Pure Python — no numpy here."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    pos = (n - 1) * q
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _round_or_none(v, digits=1):
    return round(v, digits) if v is not None else None


def _effective_floor(client_id):
    switch = DEVICE_SWITCH_FLOOR.get(client_id)
    return switch if (switch and switch > CLEAN_DATA_FLOOR) else CLEAN_DATA_FLOOR


def _overnight_signature(day):
    w = day.get("wearable", {}) or {}
    return tuple(_clean_value(w.get(k))
                 for k in ("sleep_duration", "hrv", "rhr", "respiration"))


def collapse_carry_forward(days_ascending):
    """Drop each day whose four overnight values are identical to the previous
    day's. Four independent metrics matching to the decimal on consecutive days
    is the carry-forward signature, not physiology."""
    kept, collapsed, prev = [], 0, None
    for d in days_ascending:
        sig = _overnight_signature(d)
        if all(v is None for v in sig):
            continue
        if prev is not None and sig == prev:
            collapsed += 1
        else:
            kept.append(d)
        prev = sig
    return kept, collapsed


def compute_metric_baseline(spec, pool_values, current_value):
    n = len(pool_values)
    status = "ready" if n >= BASELINE_MIN_DAYS else "forming"

    out = {
        "key": spec["key"], "label": spec["label"], "unit": spec["unit"],
        "direction": spec["direction"], "source": spec["source"],
        "current": _round_or_none(current_value, 1),
        "n": n,
        "days_to_ready": max(0, BASELINE_MIN_DAYS - n),
        "status": status,
        "stats_published": n >= BASELINE_MIN_STATS,
        "median": None, "q1": None, "q3": None, "iqr": None,
        "deviation_iqr": None, "direction_vs_baseline": None, "favourable": None,
    }

    if n < BASELINE_MIN_STATS:
        return out

    s = sorted(pool_values)
    median, q1, q3 = _quantile(s, 0.5), _quantile(s, 0.25), _quantile(s, 0.75)
    iqr = (q3 - q1) if (q1 is not None and q3 is not None) else None

    out["median"] = _round_or_none(median, 1)
    out["q1"] = _round_or_none(q1, 1)
    out["q3"] = _round_or_none(q3, 1)
    out["iqr"] = _round_or_none(iqr, 1)

    # Deviation needs a ready window AND actual variation. A zero IQR would
    # divide by zero and report every night as a dramatic outlier.
    if status == "ready" and current_value is not None and iqr and iqr > 0:
        dev = (current_value - median) / iqr
        out["deviation_iqr"] = round(dev, 2)
        if dev > 0.25:
            out["direction_vs_baseline"] = "above"
        elif dev < -0.25:
            out["direction_vs_baseline"] = "below"
        else:
            out["direction_vs_baseline"] = "typical"
        # Physiology decided here, not in the render layer: for RHR and
        # breathing rate, below baseline is the good direction.
        if out["direction_vs_baseline"] == "typical":
            out["favourable"] = None
        elif spec["direction"] == "higher_is_better":
            out["favourable"] = dev > 0
        else:
            out["favourable"] = dev < 0

    return out


def handle_baselines(request, client_id, client_page_id, headers):
    """Per-metric 28-day median, IQR, current value, deviation in IQR units, and
    status (forming | ready). `ranked` orders metrics by absolute deviation —
    which three chips Today shows is a computation, not a render decision."""
    floor = _effective_floor(client_id)
    today = _today_str()

    recovery = BASELINE_RECOVERY_MODE and BASELINE_RECOVERY_FLOOR < floor
    window_days = BASELINE_RECOVERY_WINDOW_DAYS if recovery else BASELINE_WINDOW_DAYS
    pool_floor = BASELINE_RECOVERY_FLOOR if recovery else floor
    since = max(pool_floor, _days_ago_str(window_days))

    records = query_daily_records_since(client_page_id, since)
    days = [extract_record(r) for r in records]
    days = [d for d in days if d.get("date")]
    days.sort(key=lambda d: d["date"], reverse=True)

    # State is computed per day, then used to gate the pool. This is the step
    # that keeps healed days IN and empty days OUT without a re-score.
    for d in days:
        d["data_state"] = data_state.compute_from_extracted(
            d, Date.fromisoformat(d["date"])
        ).state

    today_record = next((d for d in days if d["date"] == today), None)

    pool_days = [d for d in days
                 if d["date"] < today and d["data_state"] not in EXCLUDED_DATA_STATES]
    pool_days.sort(key=lambda d: d["date"])
    collapsed = 0
    if recovery:
        pool_days, collapsed = collapse_carry_forward(pool_days)

    metrics = {}
    for spec in BASELINE_METRICS:
        key = spec["key"]
        pool_values = [v for v in
                       (_clean_value(d.get("wearable", {}).get(key)) for d in pool_days)
                       if v is not None]
        current = _clean_value(today_record.get("wearable", {}).get(key)) if today_record else None

        m = compute_metric_baseline(spec, pool_values, current)
        m["current_date"] = today if current is not None else None
        metrics[key] = m

    ready_keys = [k for k, m in metrics.items() if m["status"] == "ready"]
    scored = sorted((m for m in metrics.values() if m["deviation_iqr"] is not None),
                    key=lambda m: abs(m["deviation_iqr"]), reverse=True)
    ranked = [m["key"] for m in scored]

    overall_status = "ready" if len(ready_keys) >= BASELINE_READY_MIN_METRICS else "forming"
    days_to_ready = None
    if overall_status == "forming":
        gaps = sorted(m["days_to_ready"] for m in metrics.values())
        idx = BASELINE_READY_MIN_METRICS - 1
        days_to_ready = gaps[idx] if idx < len(gaps) else None

    return (json.dumps({
        "client_id": client_id,
        "as_of": today,
        "window": {
            "floor": floor, "pool_floor": pool_floor, "since": since,
            "window_days": window_days, "min_days": BASELINE_MIN_DAYS,
            "records_in_window": len(days), "days_in_pool": len(pool_days),
            "pool_mode": "recovered" if recovery else "clean",
            "collapsed_runs": collapsed,
        },
        "status": overall_status,
        "days_to_ready": days_to_ready,
        "ready_metrics": ready_keys,
        "metrics": metrics,
        "ranked": ranked,
        "top3": ranked[:3],
        "note": None if overall_status == "ready" else
                "Baseline still forming — not enough clean days to say what is "
                "normal for this client yet.",
    }), 200, headers)


# ═══════════════════════════════════════════════
# PROGRESS
# ═══════════════════════════════════════════════

def _range_to_days(range_param):
    if range_param == "30":
        return 30
    if range_param == "90":
        return 90
    return None


def query_anthropometrics(client_id):
    body = {
        "filter": {"property": "client_id", "rich_text": {"equals": client_id}},
        "sorts": [{"property": "date", "direction": "ascending"}],
        "page_size": 100,
    }
    resp = requests.post(
        f"https://api.notion.com/v1/databases/{ANTHROPOMETRICS_DB}/query",
        headers=NOTION_HEADERS, json=body,
    )
    if resp.status_code != 200:
        return []
    out = []
    for r in resp.json().get("results", []):
        p = r.get("properties", {})
        d = get_date(p.get("date"))
        if d:
            out.append({
                "date": d,
                "weight_kg": get_number(p.get("weight_kg")),
                "height_cm": get_number(p.get("height_cm")),
            })
    return out


def weight_on_or_before(measurements, target_date):
    chosen = None
    for m in measurements:
        if m["date"] <= target_date and m.get("weight_kg"):
            chosen = m["weight_kg"]
        elif m["date"] > target_date:
            break
    return chosen


def query_session_sets(client_id, since_date):
    filt = {"property": "client_id", "rich_text": {"equals": client_id}}
    if since_date:
        filt = {"and": [filt, {"property": "date", "date": {"on_or_after": since_date}}]}

    rows, cursor = [], None
    for _ in range(10):
        body = {"filter": filt,
                "sorts": [{"property": "date", "direction": "ascending"}],
                "page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        resp = requests.post(
            f"https://api.notion.com/v1/databases/{SESSION_SETS_DB}/query",
            headers=NOTION_HEADERS, json=body,
        )
        if resp.status_code != 200:
            break
        data = resp.json()
        for r in data.get("results", []):
            p = r.get("properties", {})
            rows.append({
                "session_id": get_text(p.get("session_id")),
                "date": get_date(p.get("date")),
                "exercise_name": get_select(p.get("exercise_name")),
                "mode": get_select(p.get("mode")),
                "set_count": get_number(p.get("set_count")),
                "tonnage_kg": get_number(p.get("tonnage_kg")),
                "top_load_kg": get_number(p.get("top_load_kg")),
                "best_mv": get_number(p.get("best_mv")),
                "best_pp": get_number(p.get("best_pp")),
                "sets_json": get_full_text(p.get("sets_json")),
            })
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return rows


def _session_ts(session_id):
    """Decode the epoch-ms timestamp in a session_id ('S-' + base36(Date.now())).
    Chronological tie-break for same-date sessions. 0 if undecodable."""
    if not session_id:
        return 0
    code = session_id[2:] if session_id.startswith("S-") else session_id
    try:
        return int(code, 36)
    except Exception:
        return 0


def _mean_power_stats(sets_json):
    """Per-set MEAN power. W/kg is conventionally mean-power-based — NOT best_pp
    (peak power), which is a separate metric."""
    if not sets_json:
        return None, None
    try:
        sets = json.loads(sets_json)
    except Exception:
        return None, None
    mps = [s.get("mp") for s in sets if isinstance(s, dict) and s.get("mp") is not None]
    if not mps:
        return None, None
    return max(mps), sum(mps) / len(mps)


def _pct_delta(series):
    vals = [pt["value"] for pt in series if pt.get("value") is not None]
    if len(vals) < 2 or not vals[0]:
        return None
    return round((vals[-1] - vals[0]) / vals[0] * 100, 1)


def _row_metrics(row, measurements, latest_weight):
    bw = weight_on_or_before(measurements, row.get("date")) or latest_weight
    best_mp, avg_mp = _mean_power_stats(row.get("sets_json"))
    top_load = row.get("top_load_kg")
    return {
        "session_id": row.get("session_id"),
        "date": row.get("date"),
        "best_mv": row.get("best_mv"),
        "best_mean_power_w": round(best_mp, 1) if best_mp else None,
        "avg_mean_power_w": round(avg_mp, 1) if avg_mp else None,
        "peak_power_w": row.get("best_pp"),
        "top_load_kg": top_load,
        "wkg_best": round(best_mp / bw, 2) if (best_mp and bw) else None,
        "wkg_avg": round(avg_mp / bw, 2) if (avg_mp and bw) else None,
        "rel_strength_x_bw": round(top_load / bw, 2) if (top_load and bw) else None,
        "tonnage_kg": row.get("tonnage_kg"),
    }


def _max_or_none(vals):
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


def handle_progress(request, client_id, client_page_id, headers):
    """Activity summary, per-lift trends, and recent-vs-all-time ceiling.
    range = 30 | 90 | all (default 30). top_performance is always all-time —
    a ceiling can't be derived from a windowed slice."""
    range_param = (request.args.get("range") if request.method == "GET"
                   else (request.get_json(silent=True) or {}).get("range")) or "30"
    days = _range_to_days(range_param)
    since = _days_ago_str(days) if days else None

    measurements = query_anthropometrics(client_id)
    latest_weight = measurements[-1]["weight_kg"] if measurements else None
    latest_height = next((m["height_cm"] for m in reversed(measurements)
                          if m.get("height_cm")), None)

    all_sets = query_session_sets(client_id, None)
    range_sets = [r for r in all_sets if (since is None or (r.get("date") or "") >= since)]
    range_label = {"30": "Last 30 days", "90": "Last 90 days"}.get(range_param, "All time")

    activity_summary = {
        "period": range_label,
        "days_active": len({r.get("date") for r in range_sets if r.get("date")}),
        "sessions": len({r.get("session_id") for r in range_sets if r.get("session_id")}),
        "total_tonnage_kg": round(sum((r.get("tonnage_kg") or 0) for r in range_sets)),
        "exercise_count": len({r.get("exercise_name") for r in range_sets if r.get("exercise_name")}),
    }

    sessions = {}
    for row in range_sets:
        sid = row.get("session_id")
        if not sid:
            continue
        s = sessions.setdefault(sid, {"date": row.get("date"), "session_id": sid, "tonnage": 0})
        s["tonnage"] += row.get("tonnage_kg") or 0
    tonnage_series = [
        {"date": s["date"], "value": round(s["tonnage"])}
        for s in sorted(sessions.values(),
                        key=lambda v: (v["date"] or "", _session_ts(v["session_id"])))
    ]

    lifts = {}
    for row in range_sets:
        name = row.get("exercise_name")
        if name:
            lifts.setdefault(name, []).append(_row_metrics(row, measurements, latest_weight))

    trends = []
    for name, entries in lifts.items():
        entries.sort(key=lambda x: (x["date"] or "", _session_ts(x["session_id"])))
        distinct = len({e["session_id"] for e in entries})
        if distinct < MIN_SESSIONS_FOR_TREND:
            continue
        wkg_series = [{"date": e["date"], "value": e["wkg_best"]} for e in entries]
        rel_series = [{"date": e["date"], "value": e["rel_strength_x_bw"]} for e in entries]
        trends.append({
            "exercise": name,
            "sessions": distinct,
            "series": entries,
            "summary": {
                "wkg_best_latest": entries[-1]["wkg_best"],
                "wkg_best_delta_pct": _pct_delta(wkg_series),
                "rel_strength_latest": entries[-1]["rel_strength_x_bw"],
                "rel_strength_delta_pct": _pct_delta(rel_series),
            },
        })
    trends.sort(key=lambda t: t["sessions"], reverse=True)

    recent_since = _days_ago_str(30)
    alltime_lifts = {}
    for row in all_sets:
        name = row.get("exercise_name")
        if name:
            alltime_lifts.setdefault(name, []).append(
                _row_metrics(row, measurements, latest_weight))

    top_performance = []
    for name, entries in alltime_lifts.items():
        if len({e["session_id"] for e in entries}) < MIN_SESSIONS_FOR_TREND:
            continue
        recent = [e for e in entries if (e["date"] or "") >= recent_since]
        at_wkg = _max_or_none([e["wkg_best"] for e in entries])
        at_rel = _max_or_none([e["rel_strength_x_bw"] for e in entries])
        rc_wkg = _max_or_none([e["wkg_best"] for e in recent])
        rc_rel = _max_or_none([e["rel_strength_x_bw"] for e in recent])
        top_performance.append({
            "exercise": name,
            "wkg": {
                "recent_best": rc_wkg, "all_time_best": at_wkg,
                "pct_of_ceiling": round(rc_wkg / at_wkg * 100) if (rc_wkg and at_wkg) else None,
                "is_pr": bool(rc_wkg and at_wkg and rc_wkg >= at_wkg),
            },
            "rel_strength": {
                "recent_best": rc_rel, "all_time_best": at_rel,
                "pct_of_ceiling": round(rc_rel / at_rel * 100) if (rc_rel and at_rel) else None,
                "is_pr": bool(rc_rel and at_rel and rc_rel >= at_rel),
            },
        })
    top_performance.sort(key=lambda t: t["exercise"])

    return (json.dumps({
        "client_id": client_id,
        "range": range_param,
        "range_label": range_label,
        "bodyweight_kg": latest_weight,
        "height_cm": latest_height,
        "has_weight_data": latest_weight is not None,
        "session_count": len(sessions),
        "activity_summary": activity_summary,
        "tonnage_series": tonnage_series,
        "tonnage_delta_pct": _pct_delta(tonnage_series),
        "lifts": trends,
        "top_performance": top_performance,
        "note": None if trends else
                "Not enough session data yet — lifts appear once logged in 2+ sessions.",
    }), 200, headers)


# ═══════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════

def main(request):
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
        if not NOTION_TOKEN:
            return (json.dumps({"error": "NOTION_TOKEN is not set"}), 500, headers)

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
        if endpoint == "progress":
            return handle_progress(request, client_id, client_page_id, headers)
        if endpoint == "baselines":
            return handle_baselines(request, client_id, client_page_id, headers)
        return handle_daily(request, client_id, client_page_id, headers)

    except Exception as e:
        return (json.dumps({"error": str(e)}), 500, headers)
