"""
test_loader.py — tests for the eligibility contract.

These are the fundamentals tests. If any of them fail, every number the
longitudinal layer produces downstream is suspect, because all four
computations read the Window this module builds.

Two fixtures, deliberately different regimes:
  rc_payload.json    pre-floor (2026-03..04), Notion API shape, the
                     regression anchor against relational v3.
  live_aug_2026.json post-floor (2026-08), flat row shape, the only
                     sample containing real carry-forward inside rows
                     tagged data_state='full'.
"""
import json
import os
from pathlib import Path

import pytest

from longitudinal.loader import (
    FIELDS,
    Day,
    LoaderConfig,
    build_window,
    load_from_notion_payload,
    parse_daily_pages,
    parse_expected_pairs,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def rc_payload():
    return json.loads((FIXTURES / "rc_payload.json").read_text())


@pytest.fixture
def rc_expected():
    return json.loads((FIXTURES / "rc_expected.json").read_text())


@pytest.fixture
def live_rows():
    return json.loads((FIXTURES / "live_aug_2026.json").read_text())["rows"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No LOADER_* variable leaks between tests. Defaults must be defaults."""
    for k in list(os.environ):
        if k.startswith("LOADER_"):
            monkeypatch.delenv(k, raising=False)


@pytest.fixture
def legacy(monkeypatch):
    """Exact relational v3 semantics: sentinel zeros kept, partial
    carry-forward admitted. Used only to prove the port changed nothing.
    Never a production configuration."""
    monkeypatch.setenv("LOADER_ZERO_AS_NULL", "false")
    monkeypatch.setenv("LOADER_NEAR_DUP_MIN_FIELDS", "0")
    return LoaderConfig()


# ──────────────────────────────────────────────────────────────────────
# REGRESSION: the loader must select exactly what relational v3 selected
# ──────────────────────────────────────────────────────────────────────
def test_matches_v3_row_accounting(rc_payload, rc_expected, legacy):
    """Golden. v3's published config block is the contract being preserved."""
    win, _ = load_from_notion_payload(rc_payload, legacy)
    cfg = rc_expected["config"]

    assert win.rows_received == cfg["rows_received"] == 27
    assert win.n_eligible == cfg["rows_used"] == 23
    assert win.excluded_counts()["no_field_data"] == cfg["dropped_empty"] == 4
    assert win.excluded_counts()["no_sync"] == cfg["dropped_no_sync"] == 0
    assert win.excluded_counts()["exact_duplicate"] == cfg["dropped_duplicate"] == 0
    assert win.excluded_counts()["out_of_window"] == cfg["dropped_out_of_window"] == 0


def test_matches_v3_window_geometry(rc_payload, rc_expected, legacy):
    win, _ = load_from_notion_payload(rc_payload, legacy)
    cfg = rc_expected["config"]
    assert win.window_start == cfg["window_start"] == "2026-02-13"
    assert win.window_end == cfg["window_end"] == "2026-04-13"
    assert win.span_days == cfg["span_days"] == 28
    assert win.coverage_pct == cfg["coverage_pct"] == 82.1


def test_selected_dates_are_frozen(rc_payload, legacy):
    """The exact 23 surviving days. Any change here changes every RC value."""
    win, _ = load_from_notion_payload(rc_payload, legacy)
    assert win.dates == [
        "2026-03-17", "2026-03-21", "2026-03-22", "2026-03-23", "2026-03-24",
        "2026-03-25", "2026-03-26", "2026-03-27", "2026-03-28", "2026-03-29",
        "2026-03-30", "2026-03-31", "2026-04-01", "2026-04-02", "2026-04-03",
        "2026-04-04", "2026-04-05", "2026-04-06", "2026-04-09", "2026-04-10",
        "2026-04-11", "2026-04-12", "2026-04-13",
    ]


def test_expected_pairs_parse(rc_payload):
    _, pairs = load_from_notion_payload(rc_payload)
    assert len(pairs) == 66
    assert all(p.pair_id for p in pairs)
    assert all(p.diagnostic_weight is not None for p in pairs)


def test_missing_weight_defaults_to_one_not_a_tenth():
    """A missing diagnostic_weight must not mute a pair to a tenth of its
    neighbours. Regression on the v2 bug."""
    pairs = parse_expected_pairs({"results": [{"properties": {
        "pair_id": {"type": "title", "title": [{"plain_text": "F1_F2"}]},
        "field_a": {"type": "select", "select": {"name": "F1"}},
        "field_b": {"type": "select", "select": {"name": "F2"}},
    }}]})
    assert pairs[0].diagnostic_weight == 1.0
    assert pairs[0].weight_was_missing is True


# ──────────────────────────────────────────────────────────────────────
# ACCOUNTING INVARIANT
# ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("fixture_name", ["rc_payload", "live_rows"])
def test_every_row_is_accounted_for(fixture_name, request):
    """No row may vanish without a named reason. This is the invariant that
    makes the loader auditable; a silent drop is the exact failure mode that
    let S7 report success while writing nulls."""
    data = request.getfixturevalue(fixture_name)
    if fixture_name == "rc_payload":
        win, _ = load_from_notion_payload(data)
    else:
        win = build_window(data)
    assert win.rows_received == win.n_eligible + len(win.excluded)


def test_exclusion_reasons_are_from_the_known_set(live_rows):
    from longitudinal.loader import EXCLUSION_REASONS
    win = build_window(live_rows)
    assert all(e.reason in EXCLUSION_REASONS for e in win.excluded)


# ──────────────────────────────────────────────────────────────────────
# CARRY-FORWARD
# ──────────────────────────────────────────────────────────────────────
def test_data_state_full_does_not_guarantee_independence(live_rows):
    """2026-08-03 is byte-identical to 08-02 and both are tagged 'full'.
    Trusting data_state alone would admit it as an observation."""
    win = build_window(live_rows)
    dropped = {e.date: e.reason for e in win.excluded}
    assert dropped.get("2026-08-03") == "exact_duplicate"
    assert "2026-08-03" not in win.dates


def test_duplicate_compares_against_original_sequence(live_rows):
    """08-05 and 08-06 are identical to 08-04, which is dropped as no_sync.
    Comparing against surviving rows only would make 08-05 look novel."""
    win = build_window(live_rows)
    dropped = {e.date: e.reason for e in win.excluded}
    assert dropped.get("2026-08-04") == "no_sync"
    assert dropped.get("2026-08-05") == "exact_duplicate"
    assert dropped.get("2026-08-06") == "exact_duplicate"


def test_near_duplicate_can_be_disabled_for_v3_parity(live_rows, legacy):
    """08-07 shares 11/12 fields with 08-06 and v3 admitted it."""
    win = build_window(live_rows, legacy)
    assert "2026-08-07" in win.dates
    assert win.excluded_counts()["near_duplicate"] == 0


def test_near_duplicate_candidates_are_always_counted(live_rows, legacy):
    """Detected and reported even when the drop is disabled, so a legacy
    run still tells you what it is carrying."""
    win = build_window(live_rows, legacy)
    assert win.diagnostics["near_dup_candidates_10of12"] >= 1
    assert win.diagnostics["near_dup_dropped"] == 0


def test_near_duplicate_catches_partial_carry_forward(live_rows):
    win = build_window(live_rows)
    by_date = {e.date: e for e in win.excluded}
    assert by_date["2026-08-07"].reason == "near_duplicate"
    # The detail must record how many fields matched, so an exclusion can
    # be argued with after the fact rather than taken on trust.
    assert by_date["2026-08-07"].detail.startswith("11/12")


def test_near_duplicate_threshold_does_not_touch_real_days(live_rows):
    """Measured separation on live data is bimodal: real days share 0-4
    fields with their predecessor, carry-forwards share 11-12. The default
    threshold of 10 must not remove a single genuine observation."""
    win = build_window(live_rows)
    genuine = {"2026-08-17", "2026-08-18", "2026-08-19",
               "2026-08-20", "2026-08-21", "2026-08-22"}
    assert genuine.issubset(set(win.dates))


# ──────────────────────────────────────────────────────────────────────
# ZERO-CODED NULLS
# ──────────────────────────────────────────────────────────────────────
def test_sentinel_zeros_are_nulled_by_default(rc_payload):
    """scoring-engine/main.py lists f1_score..f12_score in
    SENTINEL_ZERO_FIELDS: S3 maps empty Notion numbers to 0 and 0 is never
    a real field score. The loader must agree with the scoring engine."""
    win, _ = load_from_notion_payload(rc_payload)
    assert win.diagnostics["zero_cells_nulled"] == 35
    assert win.diagnostics["zero_cells_present"] == 0


def test_nulling_zeros_removes_cells_not_days(rc_payload):
    """A day carrying one sentinel zero is still a day."""
    win, _ = load_from_notion_payload(rc_payload)
    assert win.n_eligible == 23


def test_zeros_can_be_kept_for_v3_parity(rc_payload, legacy):
    """35 of the pre-floor fixture's non-null cells are exactly 0. Legacy
    mode keeps them but must never hide them."""
    win, _ = load_from_notion_payload(rc_payload, legacy)
    assert win.diagnostics["zero_cells_present"] == 35
    assert win.diagnostics["zero_cells_nulled"] == 0


def test_post_floor_data_contains_no_zeros(live_rows):
    """Evidence that zero-coding is a pre-floor artifact of the old
    scoring engine, not a legitimate score."""
    win = build_window(live_rows)
    assert win.diagnostics["zero_cells_present"] == 0


# ──────────────────────────────────────────────────────────────────────
# WINDOW GEOMETRY
# ──────────────────────────────────────────────────────────────────────
def test_window_is_calendar_bounded_not_row_bounded(monkeypatch):
    """A row-count window stretches backwards in proportion to the drop
    rate and blends unrelated physiological regimes."""
    monkeypatch.setenv("LOADER_WINDOW_DAYS", "10")
    rows = [
        {"date": f"2026-08-{d:02d}", "data_state": "full",
         "wearable_absent": False, **{f: float(d + i) for i, f in enumerate(FIELDS)}}
        for d in range(1, 23)
    ]
    win = build_window(rows, LoaderConfig())
    assert win.window_start == "2026-08-13"
    assert win.window_end == "2026-08-22"
    assert win.n_eligible == 10
    assert win.excluded_counts()["out_of_window"] == 12


def test_window_anchors_on_last_surviving_row_not_last_received(monkeypatch):
    """A trailing no_sync row must not drag the window forward a day."""
    monkeypatch.setenv("LOADER_WINDOW_DAYS", "3")
    base = {f: 1.0 for f in FIELDS}
    rows = [
        {"date": "2026-08-18", "data_state": "full", "wearable_absent": False, **base},
        {"date": "2026-08-19", "data_state": "full", "wearable_absent": False,
         **{f: 2.0 for f in FIELDS}},
        {"date": "2026-08-20", "data_state": "no_sync", "wearable_absent": False,
         **{f: 3.0 for f in FIELDS}},
    ]
    win = build_window(rows, LoaderConfig())
    assert win.window_end == "2026-08-19"


def test_span_days_reflects_actual_elapsed_time(live_rows):
    win = build_window(live_rows)
    assert win.span_days == 22          # 08-01 .. 08-22
    assert win.n_eligible < win.span_days


# ──────────────────────────────────────────────────────────────────────
# REGIME
# ──────────────────────────────────────────────────────────────────────
def test_regime_pre_floor(rc_payload):
    win, _ = load_from_notion_payload(rc_payload)
    assert win.regime == "pre_floor"


def test_regime_mixed_when_window_straddles_the_floor(live_rows):
    """2026-08-01..22 with a 60-day window starts before the 08-10 floor."""
    win = build_window(live_rows)
    assert win.regime == "mixed"


def test_regime_post_floor(live_rows, monkeypatch):
    monkeypatch.setenv("LOADER_WINDOW_DAYS", "13")
    win = build_window(live_rows, LoaderConfig())
    assert win.window_start == "2026-08-10"
    assert win.regime == "post_floor"


def test_floor_is_configurable(live_rows, monkeypatch):
    monkeypatch.setenv("LOADER_CLEAN_DATA_FLOOR", "2026-01-01")
    win = build_window(live_rows, LoaderConfig())
    assert win.regime == "post_floor"


# ──────────────────────────────────────────────────────────────────────
# SUFFICIENCY
# ──────────────────────────────────────────────────────────────────────
def test_archetype_refuses_on_current_data_volume(live_rows):
    """The S10 gate. With 14 eligible days it must not emit a label."""
    win = build_window(live_rows)
    assert win.n_eligible == 13
    assert win.is_sufficient("archetype") is False
    payload = win.insufficiency("archetype")
    assert payload["status"] == "insufficient_data"
    assert payload["required"] == 30
    assert payload["n_eligible"] == 13
    assert "requires 30" in payload["reason"]


def test_relational_validation_status_tracks_v3(rc_payload, legacy):
    win, _ = load_from_notion_payload(rc_payload, legacy)
    assert win.is_sufficient("relational") is True
    assert win.sufficiency["relational"]["validation_status"] == "valid"


def test_relational_test_mode_below_valid_min(live_rows):
    """13 days computes but is labelled test_mode so narration can refuse."""
    win = build_window(live_rows)
    assert win.sufficiency["relational"]["ok"] is False
    assert win.sufficiency["relational"]["validation_status"] == "test_mode"


def test_gates_are_configurable(live_rows, monkeypatch):
    monkeypatch.setenv("LOADER_ARCHETYPE_MIN_DAYS", "5")
    win = build_window(live_rows, LoaderConfig())
    assert win.is_sufficient("archetype") is True


# ──────────────────────────────────────────────────────────────────────
# WINDOW API — the only surface analyzers may use
# ──────────────────────────────────────────────────────────────────────
def test_series_aligns_with_days(live_rows):
    win = build_window(live_rows)
    s = win.series("F1")
    assert len(s) == win.n_eligible
    assert s[0] == win.days[0].get("F1")


def test_all_series_covers_twelve_fields(live_rows):
    win = build_window(live_rows)
    assert set(win.all_series()) == set(FIELDS)


def test_days_are_immutable(live_rows):
    win = build_window(live_rows)
    with pytest.raises(Exception):
        win.days[0].date = "1999-01-01"


def test_config_is_echoed(live_rows):
    win = build_window(live_rows)
    for key in ("window_days", "clean_data_floor", "near_dup_min_fields",
                "zero_as_null", "archetype_min_days"):
        assert key in win.config


def test_to_dict_is_json_serialisable(live_rows):
    win = build_window(live_rows)
    json.dumps(win.to_dict())


# ──────────────────────────────────────────────────────────────────────
# PARSING EDGE CASES
# ──────────────────────────────────────────────────────────────────────
def test_empty_payload_yields_empty_window():
    win = build_window([])
    assert win.n_eligible == 0
    assert win.window_start is None
    assert win.regime == "unknown"
    assert win.is_sufficient("relational") is False


def test_unsorted_notion_results_are_sorted_before_filtering():
    """Notion result order is not guaranteed. Carry-forward detection
    compares chronological neighbours, so sorting must precede filtering."""
    def page(d, v):
        props = {
            "date": {"type": "date", "date": {"start": d}},
            "data_state": {"type": "select", "select": {"name": "full"}},
            "wearable_data_absent": {"type": "checkbox", "checkbox": False},
        }
        for f in FIELDS:
            props[f"f{f[1:]}_score"] = {"type": "number", "number": v}
        return {"properties": props}

    rows = parse_daily_pages({"results": [
        page("2026-08-03", 3.0), page("2026-08-01", 1.0), page("2026-08-02", 1.0),
    ]})
    assert [r["date"] for r in rows] == ["2026-08-01", "2026-08-02", "2026-08-03"]
    win = build_window(rows)
    assert win.dates == ["2026-08-01", "2026-08-03"]


def test_defaults_are_the_intended_production_values():
    """Guards against a default being flipped by accident. Both of these
    were deliberate decisions backed by measurement; changing one should
    require changing this test and saying why."""
    cfg = LoaderConfig()
    assert cfg.zero_as_null is True
    assert cfg.near_dup_min_fields == 10
    assert cfg.window_days == 60
    assert cfg.clean_data_floor == "2026-08-10"
    assert cfg.archetype_min_days == 30


def test_row_with_all_null_fields_is_dropped_before_no_sync():
    """Order matters for attribution: an empty no_sync row is reported as
    no_field_data, and the counts must not double-attribute it."""
    rows = [{"date": "2026-08-01", "data_state": "no_sync",
             "wearable_absent": True, **{f: None for f in FIELDS}}]
    win = build_window(rows)
    assert win.excluded_counts()["no_field_data"] == 1
    assert win.excluded_counts()["no_sync"] == 0
