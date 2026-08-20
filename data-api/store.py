"""
store.py — the only module in MANA³ that talks to the database.

Contract
--------
Nothing outside this file imports `requests` for Notion, constructs a Notion
property object, or knows what `rich_text[0].plain_text` means. Scoring,
oscillation, relational and archetype modules receive plain Python values and
return plain Python values.

That contract is the entire point. When the store moves to Postgres, this file
is rewritten against psycopg and every consumer is untouched. If Notion's
property shapes leak past this boundary, that migration becomes a rewrite of
the whole codebase instead of one file.

Two rules encoded here, both learned the hard way:

1.  ZERO IS ABSENT. S1 and S1-H write 0 into Notion number fields when Polar
    returned nothing. A resting heart rate of 0 is not a measurement. Every
    physiological read passes through `_absent_if_zero`, so no consumer ever
    has to remember this. Fields where zero is legitimate (steps, calories,
    load) are listed explicitly in ZERO_IS_VALID.

2.  ONE AUTH HEADER. The token lives in exactly one place. On 18 Aug 2026
    Notion tightened Bearer-scheme enforcement and five scenarios died at once
    because the same header was pasted into 25 modules. That cannot recur here.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date as Date
from datetime import datetime, timedelta
from typing import Any, Iterable, Iterator, Sequence

import requests

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
NOTION_VERSION = "2022-06-28"
API_ROOT = "https://api.notion.com/v1"

DB_CLIENTS = "e2bb9697-c7a5-4b9d-98be-8d93ac10cc64"
DB_DAILY_RECORDS = "3691383d-69a0-450c-b8a1-804a80c367e2"
DB_ACTIVITIES = "2ab37cb0-bf3e-4b0b-84ad-86289cfd0837"
DB_BRIEFS = "a6423d30-8432-461e-9054-5b9f91e79133"
DB_STRUCTURE = "342b52582f9c801f81bbded1eb139344"

PAGE_SIZE = 100
MAX_RETRIES = 4
BACKOFF_BASE = 0.5
RATE_LIMIT_SLEEP = 0.34  # Notion allows ~3 req/s sustained.

# Number fields where 0 is a real observation rather than a missing one.
ZERO_IS_VALID = frozenset({
    "steps", "calories_active", "calories_total",
    "load_7d", "load_28d", "load_ratio",
    "activity_count_7d", "activity_count_28d",
    "voice_duration_seconds",
})

# Physiological plausibility bounds. Outside these, the value is an artefact,
# not a reading, and is treated as absent.
PLAUSIBLE = {
    "resting_heart_rate": (25.0, 110.0),
    "hrv_overnight_rmssd": (5.0, 300.0),
    "respiration_rate_avg": (4.0, 30.0),
    "sleep_duration_minutes": (60.0, 900.0),
}


class StoreError(RuntimeError):
    """Any failure reaching or interpreting the database."""


# ─────────────────────────────────────────────────────────────
# DOMAIN TYPES — what consumers actually receive
# ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DailyRecord:
    """One client-day. All physiological values are float or None, never 0-as-absent."""

    page_id: str
    client_id: str
    client_page_id: str | None
    date: Date

    # Overnight tuple — the recovery signal.
    sleep_duration_minutes: float | None = None
    sleep_score_normalized: float | None = None
    sleep_deep_pct: float | None = None
    sleep_rem_pct: float | None = None
    hrv_overnight_rmssd: float | None = None
    resting_heart_rate: float | None = None
    respiration_rate_avg: float | None = None
    readiness_score_normalized: float | None = None
    stress_proxy_normalized: float | None = None

    # Same-day activity. Note: this is a *partial* day when read before midnight.
    steps: float | None = None
    calories_active: float | None = None
    calories_total: float | None = None

    # Subjective.
    checkin: dict[str, float | None] = field(default_factory=dict)
    voice_extraction: str | None = None

    # Derived — treat as cache, never as source of truth.
    overall_score: float | None = None
    coherence_band: str | None = None
    field_scores: dict[str, float | None] = field(default_factory=dict)
    layer_scores: dict[str, float | None] = field(default_factory=dict)
    observation_text: str | None = None

    @property
    def has_overnight(self) -> bool:
        """The three metrics that define a usable night."""
        return all(
            v is not None
            for v in (
                self.sleep_duration_minutes,
                self.hrv_overnight_rmssd,
                self.resting_heart_rate,
            )
        )

    def overnight_tuple(self) -> tuple[float | None, ...]:
        """Used to detect carry-forward: identical tuples on consecutive days."""
        return (
            self.sleep_duration_minutes,
            self.hrv_overnight_rmssd,
            self.resting_heart_rate,
            self.respiration_rate_avg,
        )


# ─────────────────────────────────────────────────────────────
# TRANSPORT — the only code that speaks HTTP to Notion
# ─────────────────────────────────────────────────────────────

def _headers() -> dict[str, str]:
    if not NOTION_TOKEN:
        raise StoreError("NOTION_TOKEN is not set")
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    url = f"{API_ROOT}{path}"
    last: Exception | None = None

    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.request(
                method, url, headers=_headers(), json=payload, timeout=30
            )
        except requests.RequestException as exc:
            last = exc
            time.sleep(BACKOFF_BASE * (2 ** attempt))
            continue

        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", BACKOFF_BASE * (2 ** attempt)))
            log.warning("notion rate limit, sleeping %.1fs", wait)
            time.sleep(wait)
            continue

        if resp.status_code == 401:
            # Loud and specific: this is the failure mode that cost a full day.
            raise StoreError(
                "Notion rejected the token (401). Check NOTION_TOKEN is current "
                "and that the integration still has access to the databases."
            )

        if resp.status_code >= 500:
            last = StoreError(f"notion {resp.status_code}: {resp.text[:200]}")
            time.sleep(BACKOFF_BASE * (2 ** attempt))
            continue

        if not resp.ok:
            raise StoreError(f"notion {resp.status_code}: {resp.text[:400]}")

        time.sleep(RATE_LIMIT_SLEEP)
        return resp.json()

    raise StoreError(f"notion unreachable after {MAX_RETRIES} attempts: {last}")


def _query_all(database_id: str, body: dict) -> Iterator[dict]:
    """Paginate a database query. Yields raw pages; callers decode."""
    cursor = None
    while True:
        payload = dict(body, page_size=PAGE_SIZE)
        if cursor:
            payload["start_cursor"] = cursor
        data = _request("POST", f"/databases/{database_id}/query", payload)
        yield from data.get("results", [])
        if not data.get("has_more"):
            return
        cursor = data.get("next_cursor")


# ─────────────────────────────────────────────────────────────
# PROPERTY CODEC — Notion's shapes stop here
# ─────────────────────────────────────────────────────────────

def _text(props: dict, key: str) -> str | None:
    p = props.get(key) or {}
    for kind in ("rich_text", "title"):
        items = p.get(kind)
        if items:
            joined = "".join(i.get("plain_text", "") for i in items).strip()
            return joined or None
    if p.get("type") == "select":
        return (p.get("select") or {}).get("name")
    return None


def _raw_number(props: dict, key: str) -> float | None:
    p = props.get(key) or {}
    v = p.get("number")
    return float(v) if isinstance(v, (int, float)) else None


def _absent_if_zero(props: dict, key: str) -> float | None:
    """Rule 1. Zero means Polar returned nothing, unless zero is legitimate."""
    v = _raw_number(props, key)
    if v is None:
        return None
    if v == 0 and key not in ZERO_IS_VALID:
        return None
    lo, hi = PLAUSIBLE.get(key, (None, None))
    if lo is not None and not (lo <= v <= hi):
        log.info("implausible %s=%s treated as absent", key, v)
        return None
    return v


def _date(props: dict, key: str) -> Date | None:
    d = ((props.get(key) or {}).get("date") or {}).get("start")
    if not d:
        return None
    return datetime.fromisoformat(d.replace("Z", "+00:00")).date()


def _relation_id(props: dict, key: str) -> str | None:
    rel = (props.get(key) or {}).get("relation") or []
    return rel[0].get("id") if rel else None


def _decode_record(page: dict) -> DailyRecord:
    props = page.get("properties", {})
    record_id = _text(props, "record_id") or ""
    return DailyRecord(
        page_id=page["id"],
        client_id=record_id.split("_")[0] if record_id else "",
        client_page_id=_relation_id(props, "client"),
        date=_date(props, "date") or Date.min,
        sleep_duration_minutes=_absent_if_zero(props, "sleep_duration_minutes"),
        sleep_score_normalized=_absent_if_zero(props, "sleep_score_normalized"),
        sleep_deep_pct=_absent_if_zero(props, "sleep_deep_pct"),
        sleep_rem_pct=_absent_if_zero(props, "sleep_rem_pct"),
        hrv_overnight_rmssd=_absent_if_zero(props, "hrv_overnight_rmssd"),
        resting_heart_rate=_absent_if_zero(props, "resting_heart_rate"),
        respiration_rate_avg=_absent_if_zero(props, "respiration_rate_avg"),
        readiness_score_normalized=_absent_if_zero(props, "readiness_score_normalized"),
        stress_proxy_normalized=_absent_if_zero(props, "stress_proxy_normalized"),
        steps=_raw_number(props, "steps"),
        calories_active=_raw_number(props, "calories_active"),
        calories_total=_raw_number(props, "calories_total"),
        checkin={
            f"q{i}": _absent_if_zero(props, f"checkin_q{i}") for i in range(1, 8)
        },
        voice_extraction=_text(props, "voice_extraction"),
        overall_score=_raw_number(props, "overall_score"),
        coherence_band=_text(props, "coherence_band"),
        field_scores={
            f"f{i}": _raw_number(props, f"f{i}_score") for i in range(1, 13)
        },
        layer_scores={
            k: _raw_number(props, f"layer_{k}")
            for k in ("structure", "electricity", "energy", "regulation")
        },
        observation_text=_text(props, "observation_text"),
    )


def _num(v: float | None) -> dict:
    return {"number": None if v is None else round(float(v), 2)}


def _rt(v: str | None, limit: int = 1900) -> dict:
    """Notion caps rich_text at 2000 chars; truncate at source, not on write."""
    return {"rich_text": [{"text": {"content": (v or "")[:limit]}}]}


def _sel(v: str | None) -> dict:
    return {"select": None if not v else {"name": v}}


# ─────────────────────────────────────────────────────────────
# PUBLIC INTERFACE — the only surface consumers use
# ─────────────────────────────────────────────────────────────

def get_active_clients() -> list[dict[str, Any]]:
    pages = _query_all(
        DB_CLIENTS, {"filter": {"property": "status", "select": {"equals": "Active"}}}
    )
    out = []
    for p in pages:
        props = p.get("properties", {})
        out.append({
            "page_id": p["id"],
            "client_id": _text(props, "client_id"),
            "polar_user_id": _text(props, "polar_user_id"),
            "protocol_start": _date(props, "protocol_start"),
        })
    return out


def get_daily_record(client_id: str, on: Date) -> DailyRecord | None:
    body = {
        "filter": {
            "property": "record_id",
            "title": {"equals": f"{client_id}_{on.isoformat()}"},
        }
    }
    for page in _query_all(DB_DAILY_RECORDS, body):
        return _decode_record(page)
    return None


def get_records_range(client_id: str, start: Date, end: Date) -> list[DailyRecord]:
    """Inclusive both ends, ascending. Ascending order is load-bearing: trajectory
    and oscillation recomputation must walk oldest-to-newest or they cascade wrong."""
    body = {
        "filter": {
            "and": [
                {"property": "record_id", "title": {"starts_with": f"{client_id}_"}},
                {"property": "date", "date": {"on_or_after": start.isoformat()}},
                {"property": "date", "date": {"on_or_before": end.isoformat()}},
            ]
        },
        "sorts": [{"property": "date", "direction": "ascending"}],
    }
    return [_decode_record(p) for p in _query_all(DB_DAILY_RECORDS, body)]


def get_clean_days(
    client_id: str, start: Date, end: Date, drop_carry_forward: bool = True
) -> list[DailyRecord]:
    """The single definition of 'clean', used by baselines, oscillation,
    relational and archetype alike. If this rule ever needs to change, it
    changes here and everything downstream follows."""
    records = get_records_range(client_id, start, end)
    clean: list[DailyRecord] = []
    previous: tuple | None = None
    for r in records:
        if not r.has_overnight:
            previous = None
            continue
        tup = r.overnight_tuple()
        if drop_carry_forward and previous is not None and tup == previous:
            log.info("%s %s dropped: identical to previous day", client_id, r.date)
            continue
        clean.append(r)
        previous = tup
    return clean


def window_quality(
    client_id: str, end: Date, days: int
) -> dict[str, Any]:
    """Density, longest gap and n for a window — the numbers S9/S10/S11 gate on.
    Every longitudinal output should carry these so the weekly brief can decline."""
    start = end - timedelta(days=days - 1)
    clean = get_clean_days(client_id, start, end)
    present = {r.date for r in clean}

    longest_gap = gap = 0
    for i in range(days):
        day = start + timedelta(days=i)
        gap = 0 if day in present else gap + 1
        longest_gap = max(longest_gap, gap)

    return {
        "n": len(clean),
        "window_days": days,
        "density": round(len(clean) / days, 3) if days else 0.0,
        "longest_gap": longest_gap,
        "start": start,
        "end": end,
    }


def write_scores(page_id: str, scores: dict[str, Any]) -> None:
    """Write derived values. Accepts the scoring engine's output dict directly."""
    props: dict[str, Any] = {}

    if "data_state" in scores:
        props["data_state"] = _sel(scores["data_state"])
    if "overall_score" in scores:
        props["overall_score"] = _num(scores["overall_score"])
    if "coherence_band" in scores:
        props["coherence_band"] = _sel(scores["coherence_band"])

    for key, val in (scores.get("field_scores") or {}).items():
        props[f"{key}_score"] = _num(val.get("score") if isinstance(val, dict) else val)
        if isinstance(val, dict) and "confidence" in val:
            props[f"{key}_confidence"] = _sel(val["confidence"])

    for layer, val in (scores.get("layer_scores") or {}).items():
        props[f"layer_{layer}"] = _num(val)

    for metric in ("load_7d", "load_28d", "load_ratio",
                   "activity_count_7d", "activity_count_28d"):
        if metric in scores:
            props[metric] = _num(scores[metric])

    if not props:
        return
    _request("PATCH", f"/pages/{page_id}", {"properties": props})


def write_observation(page_id: str, text: str, flags: str | None = None) -> None:
    props = {"observation_text": _rt(text)}
    if flags is not None:
        props["priority_flags"] = _rt(flags)
    _request("PATCH", f"/pages/{page_id}", {"properties": props})


def write_wearable(page_id: str, values: dict[str, float | None]) -> None:
    """Absent stays absent. Passing None writes null, never 0 — that is the
    whole reason the 14th and 16th of August looked like measured zeros."""
    props = {k: _num(v) for k, v in values.items()}
    props["wearable_data_absent"] = {
        "checkbox": not any(
            values.get(k) for k in
            ("sleep_duration_minutes", "hrv_overnight_rmssd", "resting_heart_rate")
        )
    }
    _request("PATCH", f"/pages/{page_id}", {"properties": props})


def get_activities(client_page_id: str, start: Date, end: Date) -> list[dict]:
    body = {
        "filter": {
            "and": [
                {"property": "client", "relation": {"contains": client_page_id}},
                {"property": "date", "date": {"on_or_after": start.isoformat()}},
                {"property": "date", "date": {"on_or_before": end.isoformat()}},
            ]
        },
        "sorts": [{"property": "date", "direction": "ascending"}],
    }
    out = []
    for p in _query_all(DB_ACTIVITIES, body):
        props = p.get("properties", {})
        out.append({
            "page_id": p["id"],
            "date": _date(props, "date"),
            "sport": _text(props, "sport"),
            "cardio_load": _raw_number(props, "cardio_load"),
            "duration_minutes": _raw_number(props, "duration_minutes"),
        })
    return out
