"""
loader.py — the single definition of "an eligible day".

This module is the foundation of the longitudinal layer. Every analyzer
(relational, oscillation, archetype) and the narrator consume the Window
this module produces. No analyzer may read Notion, re-filter rows, or
apply its own idea of validity. If four computations disagree about what
counts as an observation, the product has four answers to every question.

Hard rules for this file:
  * PURE. No HTTP, no Notion client, no clock reads, no environment
    mutation. Input is parsed JSON, output is a Window. This is what makes
    the whole layer testable from fixtures.
  * Every dropped row is accounted for by name in Window.excluded. A row
    that vanishes without a reason is a bug, and the invariant test
    enforces the accounting.
  * Every threshold is env-overridable and echoed in Window.config, so a
    test-phase value can never quietly become permanent.

Defaults correct two contaminants that relational v3 did not:
  * zero-coded nulls (ZERO_AS_NULL=true), matching the SENTINEL_ZERO_FIELDS
    convention already enforced in scoring-engine/main.py
  * near-duplicate carry-forward (NEAR_DUP_MIN_FIELDS=10)
Both are counted in Window.diagnostics whether or not they act, so any run
can be explained after the fact. Setting LOADER_ZERO_AS_NULL=false and
LOADER_NEAR_DUP_MIN_FIELDS=0 restores exact v3 behaviour; tests/test_loader.py
pins that legacy configuration against rc_expected.json so the port stays
provable independently of the semantics.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field as dc_field
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

# ──────────────────────────────────────────────────────────────────────
# FIELD CONTRACT
# The 12 fields and their layer assignment live here, not in the
# analyzers. One place to change if the model ever changes.
# ──────────────────────────────────────────────────────────────────────
FIELDS: Tuple[str, ...] = (
    "F1", "F2", "F3", "F4", "F5", "F6",
    "F7", "F8", "F9", "F10", "F11", "F12",
)

FIELD_PROPERTY = {f: f"f{f[1:]}_score" for f in FIELDS}

FIELD_LAYER = {
    "F1": "Structure", "F2": "Structure", "F3": "Structure",
    "F4": "Electricity", "F5": "Electricity", "F6": "Electricity",
    "F7": "Energy", "F8": "Energy", "F9": "Energy",
    "F10": "Regulation", "F11": "Regulation", "F12": "Regulation",
}

LAYERS: Tuple[str, ...] = ("Structure", "Electricity", "Energy", "Regulation")


# ──────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────
def _env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, str(default)))


def _env_bool(key: str, default: bool) -> bool:
    return os.environ.get(key, "true" if default else "false").lower() == "true"


def _env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


class LoaderConfig:
    """Snapshot of loader configuration, read once per build_window call.

    Read at construction rather than at import so tests can monkeypatch the
    environment without reloading the module.
    """

    def __init__(self) -> None:
        # Window is CALENDAR days, never row count. A row-count window
        # stretches backwards in proportion to the drop rate: at ~45% drops
        # a "60-row" window reaches back ~108 calendar days and blends
        # unrelated physiological regimes.
        self.window_days = _env_int("LOADER_WINDOW_DAYS", 60)

        # Clean-data floor. Records before this date came from a pipeline
        # with known ingestion defects (zero-coded nulls, 57% ingestion
        # rate). Windows are classified pre_floor / mixed / post_floor and
        # analysis from different regimes is not comparable.
        self.clean_data_floor = _env_str("LOADER_CLEAN_DATA_FLOOR", "2026-08-10")

        # Exact carry-forward: all 12 fields identical to the previous row
        # in the ORIGINAL sequence.
        self.drop_duplicates = _env_bool("LOADER_DROP_DUPLICATES", True)

        # Rows the wearable never synced for. The ingest pipeline carries
        # the previous day's scores forward; these are not observations.
        self.drop_no_sync = _env_bool("LOADER_DROP_NO_SYNC", True)

        # ── NEAR-DUPLICATE ───────────────────────────────────────────
        # Partial carry-forward: N of 12 fields pasted forward, one or two
        # genuinely updated. Measured on live data 2026-08-01..22 the
        # distribution is cleanly bimodal — real days share 0-4 fields with
        # their predecessor, carry-forwards share 11-12, nothing between.
        # 2026-08-07 is an 11/12 carry-forward tagged data_state='full' and
        # is invisible to exact-tuple matching.
        # Measured cost of the default on live data 2026-08-01..22: one row.
        # 0 disables (v3 legacy behaviour).
        self.near_dup_min_fields = _env_int("LOADER_NEAR_DUP_MIN_FIELDS", 10)

        # ── ZERO-CODED NULLS ─────────────────────────────────────────
        # Not a judgement call: scoring-engine/main.py already declares
        # f1_score..f12_score members of SENTINEL_ZERO_FIELDS, on the
        # grounds that S3 maps empty Notion numbers to 0 via ifempty(x; 0)
        # and 0 is never a real field score. The loader must agree with the
        # scoring engine or the system has two definitions of "absent".
        #
        # Evidence: 12.7% of non-null field cells in the pre-floor fixture
        # are exactly 0, concentrated in F1/F2/F3/F6; live post-floor data
        # contains none (minimum observed value 8.8). Left in place, they
        # correlate absence with absence: on the pre-floor fixture they
        # inflate computed pairs from 24 to 59.
        #
        # Nulls the CELL, never the row. A day with one sentinel zero is
        # still a day.
        self.zero_as_null = _env_bool("LOADER_ZERO_AS_NULL", True)

        # ── SUFFICIENCY GATES ────────────────────────────────────────
        # Central, not per-analyzer. An analyzer below its gate returns
        # insufficient_data and never a number.
        self.relational_min_days = _env_int("LOADER_RELATIONAL_MIN_DAYS", 20)
        self.relational_valid_min = _env_int("LOADER_RELATIONAL_VALID_MIN", 21)
        self.oscillation_min_days = _env_int("LOADER_OSCILLATION_MIN_DAYS", 21)
        # Archetype is the strictest gate on purpose. Keying on a metric
        # that is nearly flat over 10 observations produces a confident
        # label from noise.
        self.archetype_min_days = _env_int("LOADER_ARCHETYPE_MIN_DAYS", 30)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "window_days": self.window_days,
            "clean_data_floor": self.clean_data_floor,
            "drop_duplicates": self.drop_duplicates,
            "drop_no_sync": self.drop_no_sync,
            "near_dup_min_fields": self.near_dup_min_fields,
            "zero_as_null": self.zero_as_null,
            "relational_min_days": self.relational_min_days,
            "relational_valid_min": self.relational_valid_min,
            "oscillation_min_days": self.oscillation_min_days,
            "archetype_min_days": self.archetype_min_days,
        }


# ──────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ──────────────────────────────────────────────────────────────────────
EXCLUSION_REASONS = (
    "no_field_data",
    "no_sync",
    "exact_duplicate",
    "near_duplicate",
    "out_of_window",
    "unparseable_date",
)


@dataclass(frozen=True)
class Day:
    """One eligible observation. Immutable by design."""
    date: str                      # ISO YYYY-MM-DD
    values: Dict[str, Optional[float]]
    data_state: str
    wearable_absent: bool
    page_id: str = ""

    def get(self, field_name: str) -> Optional[float]:
        return self.values.get(field_name)


@dataclass(frozen=True)
class Excluded:
    date: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class ExpectedPair:
    pair_id: str
    field_a: str
    field_b: str
    relationship_type: str
    expected_r: float
    lag_days: int
    gate_field: str
    gate_threshold: float
    diagnostic_weight: float
    weight_was_missing: bool
    layer_scope: str


@dataclass
class Window:
    """The shared contract. Analyzers receive this and nothing else."""
    days: List[Day] = dc_field(default_factory=list)
    excluded: List[Excluded] = dc_field(default_factory=list)
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    span_days: Optional[int] = None
    regime: str = "unknown"
    rows_received: int = 0
    config: Dict[str, Any] = dc_field(default_factory=dict)
    diagnostics: Dict[str, Any] = dc_field(default_factory=dict)
    sufficiency: Dict[str, Any] = dc_field(default_factory=dict)

    # ── accessors: the only way analyzers should read data ──
    @property
    def n_eligible(self) -> int:
        return len(self.days)

    @property
    def dates(self) -> List[str]:
        return [d.date for d in self.days]

    def series(self, field_name: str) -> List[Optional[float]]:
        """Ordered value series for one field, aligned with self.days."""
        return [d.get(field_name) for d in self.days]

    def all_series(self) -> Dict[str, List[Optional[float]]]:
        return {f: self.series(f) for f in FIELDS}

    @property
    def coverage_pct(self) -> Optional[float]:
        if not self.span_days:
            return None
        return round(100 * len(self.days) / self.span_days, 1)

    def excluded_counts(self) -> Dict[str, int]:
        counts = {r: 0 for r in EXCLUSION_REASONS}
        for e in self.excluded:
            counts[e.reason] = counts.get(e.reason, 0) + 1
        return counts

    def is_sufficient(self, analyzer: str) -> bool:
        return bool(self.sufficiency.get(analyzer, {}).get("ok"))

    def insufficiency(self, analyzer: str) -> Dict[str, Any]:
        """The payload an analyzer returns when it refuses to compute."""
        s = self.sufficiency.get(analyzer, {})
        return {
            "status": "insufficient_data",
            "analyzer": analyzer,
            "n_eligible": self.n_eligible,
            "required": s.get("required"),
            "reason": s.get("reason"),
            "regime": self.regime,
            "window_start": self.window_start,
            "window_end": self.window_end,
        }

    def to_dict(self) -> Dict[str, Any]:
        """Serialisable summary. Used in every service response and in the
        persisted snapshot, so a run can always be explained after the fact."""
        return {
            "window_start": self.window_start,
            "window_end": self.window_end,
            "span_days": self.span_days,
            "regime": self.regime,
            "n_eligible": self.n_eligible,
            "rows_received": self.rows_received,
            "coverage_pct": self.coverage_pct,
            "dates": self.dates,
            "excluded_counts": self.excluded_counts(),
            "excluded": [
                {"date": e.date, "reason": e.reason, "detail": e.detail}
                for e in self.excluded
            ],
            "config": self.config,
            "diagnostics": self.diagnostics,
            "sufficiency": self.sufficiency,
        }


# ──────────────────────────────────────────────────────────────────────
# NOTION PARSING
# Kept separate from filtering so the filter can be tested on plain dicts
# and so a future direct-Notion-query path reuses the same filter.
# ──────────────────────────────────────────────────────────────────────
def _get_number(props: Dict[str, Any], key: str) -> Optional[float]:
    prop = props.get(key, {})
    if prop.get("type") == "number":
        return prop.get("number")
    return None


def _get_date(props: Dict[str, Any], key: str) -> str:
    prop = props.get(key, {})
    if prop.get("type") == "date" and prop.get("date"):
        return prop["date"].get("start", "") or ""
    return ""


def _get_text(props: Dict[str, Any], key: str) -> str:
    prop = props.get(key, {})
    if prop.get("type") == "title":
        items = prop.get("title", [])
    elif prop.get("type") == "rich_text":
        items = prop.get("rich_text", [])
    else:
        return ""
    return items[0].get("plain_text", "") if items else ""


def _get_select(props: Dict[str, Any], key: str) -> str:
    prop = props.get(key, {})
    if prop.get("type") == "select" and prop.get("select"):
        return prop["select"].get("name", "") or ""
    return ""


def _get_checkbox(props: Dict[str, Any], key: str) -> bool:
    prop = props.get(key, {})
    if prop.get("type") == "checkbox":
        return bool(prop.get("checkbox"))
    return False


def parse_daily_pages(notion_daily: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Notion API response -> flat rows, sorted by date ascending.

    No filtering here. Sorting happens before filtering because
    carry-forward detection compares chronological neighbours, and Notion
    result order is not guaranteed.
    """
    rows: List[Dict[str, Any]] = []
    for page in notion_daily.get("results", []) or []:
        props = page.get("properties", {}) or {}
        row: Dict[str, Any] = {
            "page_id": page.get("id", "") or "",
            "date": _get_date(props, "date"),
            "data_state": _get_select(props, "data_state"),
            "wearable_absent": _get_checkbox(props, "wearable_data_absent"),
        }
        for f in FIELDS:
            row[f] = _get_number(props, FIELD_PROPERTY[f])
        rows.append(row)
    rows.sort(key=lambda r: r.get("date") or "")
    return rows


def parse_expected_pairs(notion_pairs: Dict[str, Any]) -> List[ExpectedPair]:
    """Notion Expected Covariance Table -> ExpectedPair list."""
    pairs: List[ExpectedPair] = []
    for page in notion_pairs.get("results", []) or []:
        props = page.get("properties", {}) or {}
        pair_id = _get_text(props, "pair_id")
        if not pair_id:
            continue
        dw_raw = _get_number(props, "diagnostic_weight")
        pairs.append(ExpectedPair(
            pair_id=pair_id,
            field_a=_get_select(props, "field_a"),
            field_b=_get_select(props, "field_b"),
            relationship_type=_get_select(props, "relationship_type"),
            expected_r=_get_number(props, "expected_r") or 0.0,
            lag_days=int(_get_number(props, "lag_days") or 0),
            gate_field=_get_select(props, "gate_field") or "None",
            gate_threshold=float(_get_number(props, "gate_threshold") or 0),
            # Default 1.0, not 0.10. A missing weight must not silently mute
            # a pair to a tenth of its neighbours.
            diagnostic_weight=dw_raw if dw_raw is not None else 1.0,
            weight_was_missing=dw_raw is None,
            layer_scope=_get_select(props, "layer_scope") or "Cross-layer",
        ))
    return pairs


# ──────────────────────────────────────────────────────────────────────
# FILTERING
# ──────────────────────────────────────────────────────────────────────
def _field_vector(row: Dict[str, Any]) -> Tuple[Optional[float], ...]:
    return tuple(row.get(f) for f in FIELDS)


def _matching_fields(a: Tuple, b: Tuple) -> int:
    """Count fields present in BOTH rows and exactly equal.

    Nulls do not count as agreement — two rows that are both mostly empty
    are not evidence of carry-forward.
    """
    return sum(
        1 for x, y in zip(a, b)
        if x is not None and y is not None and x == y
    )


def _classify_regime(
    window_start: Optional[str],
    window_end: Optional[str],
    floor: str,
) -> str:
    if not window_start or not window_end:
        return "unknown"
    try:
        s = date.fromisoformat(window_start)
        e = date.fromisoformat(window_end)
        f = date.fromisoformat(floor)
    except (ValueError, TypeError):
        return "unknown"
    if e < f:
        return "pre_floor"
    if s >= f:
        return "post_floor"
    return "mixed"


def _sufficiency(n: int, cfg: LoaderConfig, regime: str) -> Dict[str, Any]:
    """One verdict per analyzer, decided centrally.

    Regime is reported but does not itself block: a pre_floor window may be
    computed for backfill or comparison, it just must never be compared
    against a post_floor one. That refusal belongs to the snapshot
    resolver, not here.
    """
    def verdict(required: int, label: str) -> Dict[str, Any]:
        ok = n >= required
        return {
            "ok": ok,
            "required": required,
            "n_eligible": n,
            "reason": None if ok else (
                f"{label} requires {required} eligible days, window has {n}"
            ),
        }

    out = {
        "relational": verdict(cfg.relational_min_days, "relational matrix"),
        "oscillation": verdict(cfg.oscillation_min_days, "oscillation analysis"),
        "archetype": verdict(cfg.archetype_min_days, "archetype classification"),
    }
    # Relational computes above min_days but is only narratable above
    # valid_min. Preserved from v3's validation_status semantics.
    out["relational"]["validation_status"] = (
        "valid" if n >= cfg.relational_valid_min else "test_mode"
    )
    out["regime"] = regime
    return out


def build_window(
    rows: List[Dict[str, Any]],
    config: Optional[LoaderConfig] = None,
) -> Window:
    """Flat rows -> Window. The single eligibility decision in the system.

    Order of operations is load-bearing and matches relational v3:
      1. rows with no field data at all
      2. no_sync / wearable_absent
      3. exact carry-forward vs the previous row in the ORIGINAL sequence
      4. near carry-forward (off by default)
      5. calendar window cut, anchored on the last SURVIVING row

    Step 3 compares against the original sequence, including rows already
    dropped, so that removing row N does not make N-1 and N+1 look like
    neighbours and mask a real repeat.
    """
    cfg = config or LoaderConfig()
    win = Window(rows_received=len(rows), config=cfg.as_dict())

    zero_cells_nulled = 0
    near_dup_candidates = 0   # counted even when the drop is disabled

    working: List[Day] = []

    for i, row in enumerate(rows):
        row_date = (row.get("date") or "")[:10]
        vec = _field_vector(row)

        if all(v is None for v in vec):
            win.excluded.append(Excluded(row_date, "no_field_data"))
            continue

        if cfg.drop_no_sync and (
            row.get("data_state") == "no_sync" or row.get("wearable_absent")
        ):
            win.excluded.append(Excluded(
                row_date, "no_sync",
                f"data_state={row.get('data_state') or 'none'}",
            ))
            continue

        if i > 0:
            prev_vec = _field_vector(rows[i - 1])
            matching = _matching_fields(vec, prev_vec)

            if cfg.drop_duplicates and vec == prev_vec:
                win.excluded.append(Excluded(
                    row_date, "exact_duplicate",
                    f"identical to {(rows[i-1].get('date') or '')[:10]}",
                ))
                continue

            if matching >= 10:
                near_dup_candidates += 1

            if (cfg.near_dup_min_fields > 0
                    and matching >= cfg.near_dup_min_fields):
                win.excluded.append(Excluded(
                    row_date, "near_duplicate",
                    f"{matching}/12 fields identical to "
                    f"{(rows[i-1].get('date') or '')[:10]}",
                ))
                continue

        values: Dict[str, Optional[float]] = {}
        for f in FIELDS:
            v = row.get(f)
            if cfg.zero_as_null and v == 0:
                v = None
                zero_cells_nulled += 1
            values[f] = v

        working.append(Day(
            date=row_date,
            values=values,
            data_state=row.get("data_state") or "",
            wearable_absent=bool(row.get("wearable_absent")),
            page_id=row.get("page_id") or "",
        ))

    # ── CALENDAR WINDOW ──
    # Anchored on the last SURVIVING row, so "60-day window" always means
    # 60 days of elapsed time, never "however far back we reached to find
    # 60 rows".
    if working and cfg.window_days > 0:
        try:
            end_d = date.fromisoformat(working[-1].date)
            start_d = end_d - timedelta(days=cfg.window_days - 1)
            kept: List[Day] = []
            for d in working:
                if not d.date:
                    win.excluded.append(Excluded(d.date, "unparseable_date"))
                    continue
                if date.fromisoformat(d.date) >= start_d:
                    kept.append(d)
                else:
                    win.excluded.append(Excluded(d.date, "out_of_window"))
            working = kept
            win.window_start = start_d.isoformat()
            win.window_end = end_d.isoformat()
        except (ValueError, TypeError):
            # Unparseable anchor: fall through uncut rather than silently
            # correlating an arbitrary slice.
            win.window_start = win.window_end = None

    win.days = working

    if working:
        try:
            first = date.fromisoformat(working[0].date)
            last = date.fromisoformat(working[-1].date)
            win.span_days = (last - first).days + 1
        except (ValueError, TypeError):
            win.span_days = None

    win.regime = _classify_regime(
        win.window_start, win.window_end, cfg.clean_data_floor
    )

    zero_cells_present = sum(
        1 for d in working for f in FIELDS if d.get(f) == 0
    )
    win.diagnostics = {
        "zero_cells_present": zero_cells_present,
        "zero_cells_nulled": zero_cells_nulled,
        "near_dup_candidates_10of12": near_dup_candidates,
        "near_dup_dropped": win.excluded_counts()["near_duplicate"],
        "eligible_by_data_state": _count_by_state(working),
    }

    win.sufficiency = _sufficiency(len(working), cfg, win.regime)
    return win


def _count_by_state(days: List[Day]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for d in days:
        key = d.data_state or "unset"
        out[key] = out.get(key, 0) + 1
    return out


def load_from_notion_payload(
    payload: Dict[str, Any],
    config: Optional[LoaderConfig] = None,
) -> Tuple[Window, List[ExpectedPair]]:
    """Entry point for the Make-shaped payload the service receives today.

    Returns the Window and the expected-pair table together because both
    come from the same request and both are Notion parsing concerns.
    """
    rows = parse_daily_pages(payload.get("notion_daily", {}) or {})
    pairs = parse_expected_pairs(payload.get("notion_pairs", {}) or {})
    return build_window(rows, config), pairs
