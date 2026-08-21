"""
data_state.py — is there usable overnight data for this day, and if not, why not.

Why this exists
---------------
`data_state` used to be a Notion column written by S3. That made it a snapshot
of what was true at the moment S3 last ran, which is wrong in two directions:

  - Polar delivers a night hours after it ended. Between the night ending and
    the data arriving, the record is empty and the column said `no_sync` —
    telling the client her watch failed when it hadn't. That happened on the
    18th, 20th and 21st of August 2026.
  - When a late night finally landed, or S1-H healed an old day, nothing
    recomputed the column. Good data sat underneath a `no_sync` label.

Computing it at read time removes both. There is no cache to go stale and
nothing to re-run: a healed day is correct the instant the data lands.

The states
----------
    present   all three overnight metrics available. Trust the day.
    partial   some but not all. Usable for narrative, not for baselines.
    pending   nothing yet, but the collection window is still open. This is
              the state that did not exist before, and its absence is what
              made the system lie.
    missing   window closed, nothing arrived. The honest negative.

`pending` and `missing` are the same data with different meaning, separated
only by the clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

State = Literal["present", "partial", "pending", "missing"]

CLIENT_TZ = ZoneInfo("Europe/Paris")

# Last S1 pass of the day, plus grace. Before this, absence means "not yet".
# After it, absence means "not coming". KEEP IN SYNC WITH S1'S SCHEDULE — if
# the evening run moves, move this.
FINAL_SYNC_HOUR = 20
FINAL_SYNC_GRACE_MINUTES = 45

OVERNIGHT_FIELDS = ("sleep_duration_minutes", "hrv_overnight_rmssd", "resting_heart_rate")

# States whose numbers must never enter a baseline pool, a correlation, or an
# archetype window. Imported by main.py so the rule lives in one place.
NON_ANALYSABLE_STATES = frozenset({"pending", "missing", "partial"})


def today_local() -> Date:
    """Current date in the client's timezone. Every date comparison in the API
    must use this — datetime.utcnow().date() is a different day for two hours
    every night, which is exactly the window where pending flips to missing."""
    return datetime.now(timezone.utc).astimezone(CLIENT_TZ).date()


def today_str() -> str:
    return today_local().isoformat()


@dataclass(frozen=True)
class DataState:
    state: State
    reason: str
    present_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]

    @property
    def is_analysable(self) -> bool:
        """Safe for baselines, oscillation, relational, archetype."""
        return self.state == "present"

    @property
    def show_score(self) -> bool:
        """Whether the client should see a number at all.

        Deliberately strict. A score computed from a partial or empty record is
        not a weaker version of the real score — it is a different number that
        happens to look like one. Showing yesterday's value, or a value derived
        from zeros, is worse than showing nothing.
        """
        return self.state == "present"

    def to_dict(self) -> dict:
        return {
            "data_state": self.state,
            "data_state_reason": self.reason,
            "present_fields": list(self.present_fields),
            "missing_fields": list(self.missing_fields),
            "is_analysable": self.is_analysable,
            "show_score": self.show_score,
        }


def _now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone(CLIENT_TZ)


def _window_closed(day: Date, now: datetime | None = None) -> bool:
    """Has the last collection opportunity for `day` passed?"""
    now = now or _now_local()
    if day < now.date():
        return True
    if day > now.date():
        return False
    cutoff = datetime.combine(day, time(FINAL_SYNC_HOUR), tzinfo=CLIENT_TZ) + timedelta(
        minutes=FINAL_SYNC_GRACE_MINUTES
    )
    return now >= cutoff


def compute(record, day: Date, now: datetime | None = None) -> DataState:
    """The single source of truth for whether a day has usable overnight data.

    `record` is anything exposing the three OVERNIGHT_FIELDS as attributes
    (a store.DailyRecord, or the _Extracted adapter below), or None — the daily
    record itself may not exist yet, which is normal before the 06:30 pass.
    """
    if record is None:
        closed = _window_closed(day, now)
        return DataState(
            state="missing" if closed else "pending",
            reason="no record for this date" if closed else "record not yet created",
            present_fields=(),
            missing_fields=OVERNIGHT_FIELDS,
        )

    present = tuple(f for f in OVERNIGHT_FIELDS if getattr(record, f, None) is not None)
    missing = tuple(f for f in OVERNIGHT_FIELDS if getattr(record, f, None) is None)

    if not missing:
        return DataState("present", "complete overnight tuple", present, ())

    if present:
        return DataState(
            state="partial",
            reason="missing " + ", ".join(missing),
            present_fields=present,
            missing_fields=missing,
        )

    if _window_closed(day, now):
        return DataState(
            state="missing",
            reason="no overnight data after final sync window",
            present_fields=(),
            missing_fields=missing,
        )

    return DataState(
        state="pending",
        reason="waiting for sync; window open until "
               f"{FINAL_SYNC_HOUR:02d}:{FINAL_SYNC_GRACE_MINUTES:02d}",
        present_fields=(),
        missing_fields=missing,
    )


# ── Adapter for main.py's dict shape ────────────────────────────────────────
# main.py's extract_record() returns plain dicts, not store.DailyRecord. This
# bridges the two without main.py migrating onto store.py yet. It also applies
# the zero-is-absent rule, which extract_record() does not.

def _clean(v):
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if v == 0 else v


class _Extracted:
    """Attribute view over an extract_record() dict."""

    def __init__(self, day: dict):
        w = day.get("wearable") or {}
        self.sleep_duration_minutes = _clean(w.get("sleep_duration"))
        self.hrv_overnight_rmssd = _clean(w.get("hrv"))
        self.resting_heart_rate = _clean(w.get("rhr"))


def compute_from_extracted(day: dict | None, day_date: Date, now=None) -> DataState:
    return compute(_Extracted(day) if day else None, day_date, now)


# Copy shown to the client. The pending wording is the whole point of this
# module: it does not accuse her of anything.
CLIENT_COPY = {
    "present": "",
    "partial": "Part of last night came through. The picture is incomplete.",
    "pending": "Waiting for your watch to sync. Last night usually arrives by evening.",
    "missing": "No overnight data came through for last night.",
}


def client_message(state: DataState) -> str:
    return CLIENT_COPY[state.state]
