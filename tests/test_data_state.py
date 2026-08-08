"""
Seed pytest suite for the MANA³ scoring engine (agreed as the suite's origin).
Fixtures are REAL MANA-TEST Daily Record values pulled from Notion on
2026-08-08 — including the live stale-carry-forward incident of Aug 5-7.
"""
import pytest
from data_state import compute_data_state

# 2026-08-04 — last genuine Polar sleep sync before the gap.
AUG4 = {
    "sleep_score_normalized": 58, "readiness_score_normalized": 40,
    "stress_proxy_normalized": 80, "hrv_overnight_rmssd": 75,
    "resting_heart_rate": 56, "sleep_duration_minutes": 453,
    "respiration_rate_avg": 14.4,
}
# 2026-08-05 — recovery block byte-identical to Aug 4 (carry-forward),
# while activity fields (steps 0, calories 381) moved independently.
AUG5 = dict(AUG4)
# 2026-08-08 — fresh sync, all values diverge.
AUG8 = {
    "sleep_score_normalized": 59, "readiness_score_normalized": 100,
    "stress_proxy_normalized": 40, "hrv_overnight_rmssd": 82,
    "resting_heart_rate": 50, "sleep_duration_minutes": 483,
    "respiration_rate_avg": 14.8,
}


def test_full_sync_fresh_values():
    r = compute_data_state(AUG8, yesterday=AUG4)
    assert r.state == "full"
    assert r.anchors_missing == ()


def test_stale_carry_forward_detected():
    # The exact live incident: anchors present, so a null-check calls this
    # "full" — the fingerprint must call it stale instead.
    r = compute_data_state(AUG5, yesterday=AUG4)
    assert r.state == "stale"
    assert r.reason == "carried_forward_recovery_block"


def test_no_sync_all_anchors_null():
    r = compute_data_state(
        {f: None for f in AUG8}, yesterday=AUG4)
    assert r.state == "no_sync"
    assert r.reason == "all_anchors_null"


def test_no_sync_flag_overrides_present_values():
    today = dict(AUG8, wearable_data_absent="__YES__")
    r = compute_data_state(today, yesterday=AUG4)
    assert r.state == "no_sync"
    assert r.reason == "wearable_data_absent_flag"


def test_partial_when_sleep_missing():
    today = dict(AUG8, sleep_score_normalized=None)
    r = compute_data_state(today, yesterday=AUG4)
    assert r.state == "partial"
    assert "sleep_score_normalized" in r.reason


def test_first_day_no_yesterday_is_not_stale():
    r = compute_data_state(AUG8, yesterday=None)
    assert r.state == "full"


def test_zero_is_a_reading_not_a_gap():
    today = dict(AUG8, stress_proxy_normalized=0)
    r = compute_data_state(today, yesterday=AUG4)
    assert r.state == "full"
