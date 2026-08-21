"""
data_state.py — is there usable overnight data for this day, and if not, why not.

Why this exists
---------------
`data_state` used to be a Notion column written by S3. That made it a snapshot
of what was true at the moment S3 last ran, which is wrong in two directions:

  - Polar delivers a night hours after it ended. Between the night ending and
    the data arriving, the record is empty and the column said `no_sync` —
    telling the client her watch failed when it hadn't. That happened on the
    18th, the 20th and the 21st of August 2026.
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
only by the clock. Everything downstream keys off `is_analysable`, so the
distinction is presentational — but presentation is exactly where the harm was.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from store import DailyRecord

State = Literal["present", "partial", "pending", "missing"]

CLIENT_TZ = ZoneInfo("Europe/Paris")

# Last S1 pass of the day, plus grace. Before this, absence means "not yet".
# After it, absence means "not coming". Keep in sync with S1's schedule —
# if the evening run moves, move this.
FINAL_SYNC_HOUR = 20
FINAL_SYNC_GRACE_MINUTES = 45

OVERNIGHT_FIELDS = ("sleep_duration_minutes", "hrv_overnight_rmssd", "resting_heart_rate")


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

        Deliberately strict. A score computed from a partial or empty record
        is not a weaker version of the real score — it is a different number
        that happens to look like one. Showing yesterday's value, or a value
        derived from zeros, is worse than showing nothing.
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


def compute(record: DailyRecord | None, day: Date, now: datetime | None = None) -> DataState:
    """The single source of truth for whether a day has usable overnight data.

    `record` may be None — the daily record itself may not exist yet, which is
    normal between midnight and the 06:30 pass.
    """
    if record is None:
        closed = _window_closed(day, now)
        return DataState(
            state="missing" if closed else "pending",
            reason="no record for this date" if closed else "record not yet created",
            present_fields=(),
            missing_fields=OVERNIGHT_FIELDS,
        )

    present = tuple(f for f in OVERNIGHT_FIELDS if getattr(record, f) is not None)
    missing = tuple(f for f in OVERNIGHT_FIELDS if getattr(record, f) is None)

    if not missing:
        return DataState("present", "complete overnight tuple", present, ())

    if present:
        return DataState(
            state="partial",
            reason=f"missing {', '.join(missing)}",
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
        reason=f"waiting for sync; window open until {FINAL_SYNC_HOUR:02d}:"
               f"{FINAL_SYNC_GRACE_MINUTES:02d}",
        present_fields=(),
        missing_fields=missing,
    )


# Copy shown to the client. The pending wording is the whole point of this
# module: it does not accuse her of anything.
CLIENT_COPY: dict[State, str] = {
    "present": "",
    "partial": "Part of last night came through. The picture is incomplete.",
    "pending": "Waiting for your watch to sync. Last night usually arrives by evening.",
    "missing": "No overnight data came through for last night.",
}


def client_message(state: DataState) -> str:
    return CLIENT_COPY[state.state]
