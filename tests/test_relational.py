"""
test_relational.py — tests for the relational analyzer.

The headline test is test_full_response_matches_v3_golden: in legacy loader
configuration the analyzer reproduces rc_expected.json exactly, every key,
including the compatibility config block and matrix_json_string. That is
what makes the port provable independently of the semantic changes the
loader introduced.

The rest guard the properties that were bugs in v2 and must not come back.
"""
import json
import os
from pathlib import Path

import pytest

from longitudinal.analyzers import relational
from longitudinal.loader import (
    FIELDS,
    LoaderConfig,
    build_window,
    load_from_notion_payload,
    parse_expected_pairs,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("LOADER_", "RC_")):
            monkeypatch.delenv(k, raising=False)


@pytest.fixture
def legacy(monkeypatch):
    """Exact v3 loader semantics. Only for proving the port."""
    monkeypatch.setenv("LOADER_ZERO_AS_NULL", "false")
    monkeypatch.setenv("LOADER_NEAR_DUP_MIN_FIELDS", "0")
    return LoaderConfig()


@pytest.fixture
def rc_payload():
    return json.loads((FIXTURES / "rc_payload.json").read_text())


@pytest.fixture
def rc_expected():
    return json.loads((FIXTURES / "rc_expected.json").read_text())


@pytest.fixture
def live_rows():
    return json.loads((FIXTURES / "live_aug_2026.json").read_text())["rows"]


@pytest.fixture
def pairs(rc_payload):
    return parse_expected_pairs(rc_payload["notion_pairs"])


# ──────────────────────────────────────────────────────────────────────
# THE PORT
# ──────────────────────────────────────────────────────────────────────
def test_full_response_matches_v3_golden(rc_payload, rc_expected, legacy):
    """Every key, byte for byte. The one test that says the port is real."""
    win, prs = load_from_notion_payload(rc_payload, legacy)
    got = relational.compute(win, prs, rc_payload.get("previous_matrix"))
    got.pop("window")          # new key, absent from the v3 golden
    assert got == rc_expected


def test_legacy_config_block_is_rebuilt_from_the_window(rc_payload, legacy):
    """S7's parsers still read this shape. It must survive until they don't."""
    win, prs = load_from_notion_payload(rc_payload, legacy)
    cfg = relational.compute(win, prs)["config"]
    assert cfg["rows_received"] == 27
    assert cfg["rows_used"] == 23
    assert cfg["rows_dropped"] == 4
    assert cfg["dropped_empty"] == 4
    assert cfg["window_start"] == "2026-02-13"


def test_window_block_is_present_and_complete(rc_payload):
    """The honest account, alongside the compatibility one."""
    win, prs = load_from_notion_payload(rc_payload)
    out = relational.compute(win, prs)
    assert "window" in out
    assert out["window"]["regime"] == "pre_floor"
    assert "excluded" in out["window"]
    assert out["window"]["n_eligible"] == out["days_analyzed"]


# ──────────────────────────────────────────────────────────────────────
# THE SEMANTIC CHANGE THE LOADER INTRODUCED
# ──────────────────────────────────────────────────────────────────────
def test_sentinel_zeros_collapse_the_computable_matrix(rc_payload, legacy):
    """Under production defaults two thirds of v3's matrix disappears,
    because those pairs were only computable while absence was coded as 0.
    This is the port working, not breaking."""
    win_legacy, prs = load_from_notion_payload(rc_payload, legacy)
    legacy_out = relational.compute(win_legacy, prs)

    for k in ("LOADER_ZERO_AS_NULL", "LOADER_NEAR_DUP_MIN_FIELDS"):
        os.environ.pop(k, None)
    win_now, prs2 = load_from_notion_payload(rc_payload)
    now_out = relational.compute(win_now, prs2)

    assert legacy_out["pairs_computed"] == 59
    assert now_out["pairs_computed"] == 24
    assert now_out["days_analyzed"] == legacy_out["days_analyzed"] == 23


# ──────────────────────────────────────────────────────────────────────
# REFUSAL
# ──────────────────────────────────────────────────────────────────────
def test_refuses_below_the_loader_gate(live_rows):
    """13 eligible days is under the relational floor of 20. No score."""
    win = build_window(live_rows)
    out = relational.compute(win, [])
    assert out["status"] == "insufficient_data"
    assert out["relational_coherence_score"] is None
    assert out["validation_status"] == "insufficient_data"
    assert out["required"] == 20


def test_refusal_precedes_the_missing_pair_table(live_rows):
    """An insufficient window must not be reported as a pair-table problem."""
    win = build_window(live_rows)
    out = relational.compute(win, [])
    assert "error" not in out


def test_missing_pair_table_is_named(rc_payload, legacy):
    win, _ = load_from_notion_payload(rc_payload, legacy)
    out = relational.compute(win, [])
    assert out["validation_status"] == "no_pair_table"
    assert out["relational_coherence_score"] is None


def test_analyzer_never_refilters(rc_payload, legacy):
    """days_analyzed must equal the Window's count exactly. If the analyzer
    ever drops a day of its own accord, the two definitions have diverged
    again and this catches it."""
    win, prs = load_from_notion_payload(rc_payload, legacy)
    out = relational.compute(win, prs)
    assert out["days_analyzed"] == win.n_eligible


# ──────────────────────────────────────────────────────────────────────
# REGRESSIONS FROM v2 THAT MUST NOT RETURN
# ──────────────────────────────────────────────────────────────────────
def test_rc_uses_a_true_weighted_mean(rc_payload, legacy):
    """v2 divided a weighted sum by the pair count and then by a constant
    on the unweighted scale, confining RC to ~88-100. RC must be a weighted
    mean deviation mapped onto the same 0..2 scale as its denominator."""
    win, prs = load_from_notion_payload(rc_payload, legacy)
    out = relational.compute(win, prs)
    expected_rc = round(100 - (out["weighted_mean_deviation"] * 100 / 2.0), 1)
    assert out["relational_coherence_score"] == expected_rc


def test_independent_pairs_are_weighted_like_every_other_pair(pairs, legacy,
                                                              rc_payload):
    """A violated Independent pair is among the most diagnostically
    interesting events in the matrix. v2 weighted it ~7x lower and it
    vanished from RC."""
    win, prs = load_from_notion_payload(rc_payload, legacy)
    out = relational.compute(win, prs)
    indep = [v for v in out["matrix"].values()
             if v["relationship_type"] == "Independent"
             and v["weighted_deviation"] is not None]
    assert indep
    for v in indep:
        # weighted_deviation is deviation * diagnostic_weight, never
        # deviation * abs(expected_r), which would be 0 for these.
        assert v["weighted_deviation"] > 0


def test_deviation_direction_is_new_without_a_baseline(rc_payload, legacy):
    win, prs = load_from_notion_payload(rc_payload, legacy)
    out = relational.compute(win, prs, previous_matrix={})
    directions = {v["deviation_direction"] for v in out["matrix"].values()}
    assert directions <= {"new", "insufficient_data", "unknown"}


def test_previous_matrix_drives_direction(rc_payload, legacy):
    """The signal that has never once worked in production."""
    win, prs = load_from_notion_payload(rc_payload, legacy)
    baseline = relational.compute(win, prs)

    target = next(v for v in baseline["matrix"].values()
                  if v["actual_r"] is not None and v["deviation"] is not None
                  and v["deviation"] > 0.2)
    pid, exp_r, actual_r = (target["pair_id"], target["expected_r"],
                            target["actual_r"])

    # A previous r further from expected than the current one => narrowing.
    worse = exp_r + (actual_r - exp_r) * 2
    narrowed = relational.compute(win, prs, {pid: {"actual_r": worse}})
    assert narrowed["matrix"][pid]["deviation_direction"] == "narrowing"

    # A previous r exactly on expectation => the gap has widened.
    widened = relational.compute(win, prs, {pid: {"actual_r": exp_r}})
    assert widened["matrix"][pid]["deviation_direction"] == "widening"

    # An identical previous r => stable.
    stable = relational.compute(win, prs, {pid: {"actual_r": actual_r}})
    assert stable["matrix"][pid]["deviation_direction"] == "stable"


def test_top_deviations_only_rank_sufficient_pairs(rc_payload, legacy):
    win, prs = load_from_notion_payload(rc_payload, legacy)
    out = relational.compute(win, prs)
    for t in out["top_deviations"]:
        assert out["matrix"][t["pair_id"]]["sufficient_data"] is True
    devs = [t["deviation"] for t in out["top_deviations"]]
    assert devs == sorted(devs, reverse=True)


def test_matrix_json_string_fits_notion_rich_text(rc_payload, legacy):
    """Notion caps rich_text at 2000 characters."""
    win, prs = load_from_notion_payload(rc_payload, legacy)
    out = relational.compute(win, prs)
    assert len(out["matrix_json_string"]) < 2000


def test_pair_thresholds_are_configurable(rc_payload, legacy, monkeypatch):
    win, prs = load_from_notion_payload(rc_payload, legacy)
    monkeypatch.setenv("RC_SUFFICIENT_POINTS", "999")
    out = relational.compute(win, prs, config=relational.RelationalConfig())
    assert out["pairs_computed"] == 0
    assert out["top_deviations"] == []


def test_purity_analyzer_does_not_mutate_the_window(rc_payload, legacy):
    win, prs = load_from_notion_payload(rc_payload, legacy)
    before = win.to_dict()
    relational.compute(win, prs)
    assert win.to_dict() == before


def test_result_is_json_serialisable(rc_payload, legacy):
    win, prs = load_from_notion_payload(rc_payload, legacy)
    json.dumps(relational.compute(win, prs))
