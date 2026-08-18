"""
data_state.py — MANA³ scoring engine, data-state gate (Option B).

Classifies each Daily Record's Tier-2 (wearable) data into one of four states
BEFORE any field scoring happens. The engine and S4 consume this instead of
guessing from nulls.

States
------
full     : all three DAL anchors present and the recovery block is fresh.
partial  : some anchors present, some missing; not stale. ALSO: no anchors at
           all but a primary overnight measure is present — see below.
stale    : recovery block is a byte-identical carry-forward of the previous
           day's record. Values are PRESENT but FALSE — the most dangerous
           state, because null-checks pass. (Observed live: MANA-TEST
           2026-08-05..07 duplicated the 2026-08-04 sleep/HRV/RHR/stress
           block while activity fields kept updating.)
no_sync  : no anchors AND no primary measures, or wearable_data_absent is
           flagged upstream.

Unstaged nights (v2)
--------------------
All three anchors are vendor COMPOSITES. Polar only computes them when it has
successfully staged the night. When staging fails it still records the sleep —
duration, continuity, interruptions, heart-rate samples are all there — but
returns sleep_score 0, omits sleep_charge, and dumps the whole duration into
deep_sleep with light and REM at zero. Nightly Recharge never runs, so HRV,
resting HR and breathing rate are genuinely absent.

Classifying that as no_sync was wrong twice over: it discarded a real measured
night, and it told the client their watch hadn't synced when it had.
(Observed live: MANA-TEST 2026-08-14, 9h36 recorded, stored as zero.)

So absence of the composites no longer implies absence of data. If any
PRIMARY_OVERNIGHT_FIELD survived, the state is `partial` — some of the night
is known, the vendor's interpretation of it is not.

Engine contract (enforced by the caller, main.py):
  full    -> score all fields normally.
  partial -> score only fields backed by present anchors; carry forward the
             rest and the overall score (never recompute overall from a
             partial mix — formula inconsistency).
  stale   -> treat as no Tier-2 data: carry forward scores, stamp state.
  no_sync -> same as stale.
S4 reads `data_state` off the Daily Record and switches observation genre.

Pure module: no I/O, no Notion, no Flask. Import and call.
"""

from dataclasses import dataclass, field
from typing import Optional

# The three device-agnostic DAL anchors. All are vendor composites — present
# only when the vendor's own scoring succeeded.
ANCHOR_FIELDS = (
    "sleep_score_normalized",
    "readiness_score_normalized",
    "stress_proxy_normalized",
)

# Directly measured overnight quantities. These survive when the composites
# don't, and they are the same four the /baselines endpoint uses — for the
# same reason: they are physiologically primary rather than vendor-derived.
PRIMARY_OVERNIGHT_FIELDS = (
    "sleep_duration_minutes",
    "hrv_overnight_rmssd",
    "resting_heart_rate",
    "respiration_rate_avg",
)

# Overnight/recovery block used for the stale fingerprint. These come from the
# nightly sync as one unit; real physiology never repeats them all exactly.
RECOVERY_BLOCK_FIELDS = (
    "sleep_score_normalized",
    "readiness_score_normalized",
    "stress_proxy_normalized",
    "hrv_overnight_rmssd",
    "resting_heart_rate",
    "sleep_duration_minutes",
    "respiration_rate_avg",
)

# Minimum number of non-null, pairwise-identical recovery fields required to
# call a record stale. Below this, coincidence is conceivable; at 4+, it isn't.
STALE_MIN_MATCHING_FIELDS = 4


@dataclass(frozen=True)
class DataStateResult:
    state: str                      # 'full' | 'partial' | 'stale' | 'no_sync'
    reason: str                     # machine-greppable explanation
    anchors_present: tuple = field(default_factory=tuple)
    anchors_missing: tuple = field(default_factory=tuple)
    primaries_present: tuple = field(default_factory=tuple)


def _present(value) -> bool:
    """None and empty string are absent. 0 is PRESENT (a real reading)."""
    return value is not None and value != ""


def _is_stale(today: dict, yesterday: Optional[dict]) -> bool:
    """True when today's recovery block is an exact copy of yesterday's."""
    if not yesterday:
        return False
    matching = 0
    for f in RECOVERY_BLOCK_FIELDS:
        t, y = today.get(f), yesterday.get(f)
        if not _present(t) or not _present(y):
            continue
        if t != y:
            return False          # any fresh divergence => not a carry-forward
        matching += 1
    return matching >= STALE_MIN_MATCHING_FIELDS


def compute_data_state(today: dict, yesterday: Optional[dict] = None) -> DataStateResult:
    """
    Classify today's Tier-2 data.

    Parameters
    ----------
    today : dict
        Daily Record property values for today (Notion property names as keys).
        Must include the anchor fields; recovery-block, primary-overnight and
        `wearable_data_absent` keys are used when present.
    yesterday : dict | None
        Same shape for the previous calendar day. None on a client's first day
        or when the previous record is missing — the stale check is skipped.
    """
    present = tuple(f for f in ANCHOR_FIELDS if _present(today.get(f)))
    missing = tuple(f for f in ANCHOR_FIELDS if f not in present)
    primaries = tuple(f for f in PRIMARY_OVERNIGHT_FIELDS if _present(today.get(f)))

    # Upstream absence flag wins outright.
    if today.get("wearable_data_absent") in (True, "__YES__"):
        return DataStateResult("no_sync", "wearable_data_absent_flag",
                               present, missing, primaries)

    # Stale beats full: values exist but are yesterday's bytes.
    if _is_stale(today, yesterday):
        return DataStateResult("stale", "carried_forward_recovery_block",
                               present, missing, primaries)

    if len(present) == len(ANCHOR_FIELDS):
        return DataStateResult("full", "all_anchors_present",
                               present, missing, primaries)

    if len(present) == 0:
        # No composites. Only no_sync if nothing was measured either.
        if primaries:
            return DataStateResult(
                "partial",
                "unstaged_night_primaries:" + ",".join(primaries),
                present, missing, primaries,
            )
        return DataStateResult("no_sync", "all_anchors_null",
                               present, missing, primaries)

    return DataStateResult("partial", "missing:" + ",".join(missing),
                           present, missing, primaries)
