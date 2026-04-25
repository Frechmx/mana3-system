"""
MANA³ Scoring Engine — Test Suite
Run: pytest test_scoring_engine.py -v
"""
import pytest
import json
import sys
import os

# Import scoring engine (adjust path if needed)
sys.path.insert(0, os.path.dirname(__file__))

from main import (
    clamp,
    normalize_hrv, normalize_rhr,
    normalize_deep_sleep, normalize_rem_sleep,
    normalize_duration, normalize_respiration,
    normalize_calories, normalize_steps,
    normalize_load_ratio, normalize_frequency, normalize_diversity,
    normalize_steps_structure, normalize_calories_structure,
    normalize_rom, normalize_grip, normalize_balance,
    compute_structure_anchor,
    process_checkins_and_gaps,
    compute_tier2_score,
    parse_tier3_scores,
    compute_field_score,
    compute_layer_score,
    compute_overall_score,
    linear_regression_slope,
    classify_direction,
    compute_trajectory,
    compute_priority_flags,
    compute_activity_counts,
    compute_category_diversity,
    safe_num,
    COHERENCE_BANDS,
    LAYERS,
    TIER_WEIGHTS_DEFAULT,
)

from datetime import date, timedelta


# ═══════════════════════════════════════════════
# CLAMP
# ═══════════════════════════════════════════════

class TestClamp:
    def test_within_range(self):
        assert clamp(50) == 50

    def test_below_min(self):
        assert clamp(-10) == 0

    def test_above_max(self):
        assert clamp(150) == 100

    def test_at_boundaries(self):
        assert clamp(0) == 0
        assert clamp(100) == 100

    def test_custom_range(self):
        assert clamp(5, 0, 10) == 5
        assert clamp(-1, 0, 10) == 0
        assert clamp(15, 0, 10) == 10


# ═══════════════════════════════════════════════
# NORMALIZATION FUNCTIONS
# ═══════════════════════════════════════════════

class TestNormalizeHRV:
    def test_none(self):
        assert normalize_hrv(None) is None

    def test_zero(self):
        # 0 is falsy, returns None
        assert normalize_hrv(0) is None

    def test_low(self):
        assert normalize_hrv(20) == 0

    def test_high(self):
        assert normalize_hrv(120) == 100

    def test_midrange(self):
        result = normalize_hrv(70)
        assert 45 <= result <= 55  # (70-20)/(120-20)*100 = 50

    def test_below_range(self):
        assert normalize_hrv(10) == 0  # clamped


class TestNormalizeRHR:
    def test_none(self):
        assert normalize_rhr(None) is None

    def test_zero(self):
        assert normalize_rhr(0) is None

    def test_low_rhr_is_good(self):
        assert normalize_rhr(40) == 100

    def test_high_rhr_is_bad(self):
        assert normalize_rhr(80) == 0

    def test_midrange(self):
        result = normalize_rhr(60)
        assert 45 <= result <= 55  # (80-60)/(80-40)*100 = 50


class TestNormalizeDeepSleep:
    def test_none(self):
        assert normalize_deep_sleep(None) is None

    def test_optimal_low(self):
        result = normalize_deep_sleep(15)
        assert result == 70

    def test_optimal_high(self):
        result = normalize_deep_sleep(25)
        assert result == 100

    def test_below_optimal(self):
        result = normalize_deep_sleep(7.5)
        assert 30 <= result <= 40  # 7.5/15*70 = 35

    def test_above_optimal(self):
        result = normalize_deep_sleep(30)
        assert result < 100  # penalized


class TestNormalizeRemSleep:
    def test_none(self):
        assert normalize_rem_sleep(None) is None

    def test_optimal_band(self):
        result = normalize_rem_sleep(22.5)
        assert result >= 70

    def test_below_optimal(self):
        result = normalize_rem_sleep(10)
        assert result < 70  # 10/20*70 = 35

    def test_above_optimal(self):
        result = normalize_rem_sleep(30)
        assert result < 100


class TestNormalizeDuration:
    def test_none(self):
        assert normalize_duration(None) is None

    def test_optimal_7h(self):
        result = normalize_duration(420)
        assert result == 80

    def test_optimal_8h(self):
        result = normalize_duration(480)
        assert result == 100

    def test_short_sleep(self):
        result = normalize_duration(300)
        assert result < 80  # 300/420*80 ≈ 57

    def test_oversleep(self):
        result = normalize_duration(540)
        assert result < 100  # penalized


class TestNormalizeRespiration:
    def test_none(self):
        assert normalize_respiration(None) is None

    def test_optimal_low(self):
        result = normalize_respiration(12)
        assert result == 100  # 75 + (16-12)/4*25 = 100

    def test_optimal_high(self):
        result = normalize_respiration(16)
        assert result == 75

    def test_high_rate(self):
        result = normalize_respiration(20)
        assert result < 75

    def test_very_low(self):
        result = normalize_respiration(6)
        assert result >= 50


class TestNormalizeCalories:
    def test_none(self):
        assert normalize_calories(None) is None

    def test_zero(self):
        assert normalize_calories(0) == 0

    def test_moderate(self):
        result = normalize_calories(300)
        assert result == 40  # 300/600*80

    def test_high(self):
        result = normalize_calories(600)
        assert result == 80


class TestNormalizeSteps:
    def test_none(self):
        assert normalize_steps(None) is None

    def test_target(self):
        result = normalize_steps(10000)
        assert result == 75

    def test_half(self):
        result = normalize_steps(5000)
        assert 35 <= result <= 40


class TestNormalizeLoadRatio:
    def test_none(self):
        assert normalize_load_ratio(None) is None

    def test_zero(self):
        assert normalize_load_ratio(0) is None

    def test_optimal(self):
        result = normalize_load_ratio(1.05)
        assert result >= 90

    def test_underloaded(self):
        result = normalize_load_ratio(0.5)
        assert result < 70

    def test_overloaded(self):
        result = normalize_load_ratio(1.8)
        assert result < 70


class TestNormalizeFrequency:
    def test_none(self):
        assert normalize_frequency(None) is None

    def test_zero(self):
        assert normalize_frequency(0) == 10

    def test_optimal(self):
        result = normalize_frequency(4)
        assert result >= 80

    def test_low(self):
        result = normalize_frequency(1)
        assert 30 <= result <= 45

    def test_high(self):
        result = normalize_frequency(7)
        assert result >= 50


class TestNormalizeDiversity:
    def test_none(self):
        assert normalize_diversity(None) is None

    def test_zero(self):
        assert normalize_diversity(0) == 5

    def test_one(self):
        assert normalize_diversity(1) == 30

    def test_three(self):
        assert normalize_diversity(3) == 75

    def test_five(self):
        result = normalize_diversity(5)
        assert result >= 90


class TestNormalizeStepsStructure:
    def test_none(self):
        assert normalize_steps_structure(None) is None

    def test_optimal(self):
        result = normalize_steps_structure(8000)
        assert result >= 80

    def test_low(self):
        result = normalize_steps_structure(3000)
        assert result < 65

    def test_above_10k(self):
        result = normalize_steps_structure(15000)
        assert result >= 85  # small penalty


class TestNormalizeCaloriesStructure:
    def test_none(self):
        assert normalize_calories_structure(None) is None

    def test_optimal(self):
        result = normalize_calories_structure(350)
        assert result >= 80

    def test_low(self):
        result = normalize_calories_structure(100)
        assert result < 60


class TestNormalizeROM:
    def test_none(self):
        assert normalize_rom(None, 30, 55) is None

    def test_at_max(self):
        assert normalize_rom(55, 30, 55) == 100

    def test_at_min(self):
        assert normalize_rom(30, 30, 55) == 0

    def test_midrange(self):
        result = normalize_rom(42.5, 30, 55)
        assert 45 <= result <= 55


class TestNormalizeGrip:
    def test_none(self):
        assert normalize_grip(None) is None

    def test_low(self):
        assert normalize_grip(20) == 0

    def test_high(self):
        assert normalize_grip(60) == 100

    def test_mid(self):
        result = normalize_grip(40)
        assert result == 50


class TestNormalizeBalance:
    def test_none(self):
        assert normalize_balance(None) is None

    def test_zero(self):
        assert normalize_balance(0) == 0

    def test_full(self):
        assert normalize_balance(60) == 100

    def test_mid(self):
        result = normalize_balance(30)
        assert result == 50


# ═══════════════════════════════════════════════
# STRUCTURE ANCHOR
# ═══════════════════════════════════════════════

class TestComputeStructureAnchor:
    def test_empty_assessment(self):
        result = compute_structure_anchor({})
        assert result["valid"] is False
        assert result["f1"] is None

    def test_none_assessment(self):
        result = compute_structure_anchor(None)
        assert result["valid"] is False

    def test_old_assessment_rejected(self):
        old_date = (date.today() - timedelta(days=35)).isoformat()
        result = compute_structure_anchor({"assessment_date": old_date})
        assert result["valid"] is False
        assert result["age_days"] == 35

    def test_recent_assessment_accepted(self):
        recent_date = (date.today() - timedelta(days=5)).isoformat()
        assessment = {
            "assessment_date": recent_date,
            "fms_composite": 14,
            "grip_left": 40,
            "grip_right": 42,
            "balance_left": 30,
            "balance_right": 35,
            "prac_impression": 7,
            "flag_pelvic_compensation": False,
            "flag_lumbar_compensation": False,
            "flag_rib_flare": False,
            "flag_cervical_pain": False,
        }
        result = compute_structure_anchor(assessment)
        assert result["valid"] is True
        assert result["age_days"] == 5
        assert result["f1"] is not None
        assert result["f2"] is not None
        assert result["f3"] is not None
        assert 0 <= result["f1"] <= 100
        assert 0 <= result["f2"] <= 100
        assert 0 <= result["f3"] <= 100

    def test_flags_penalize_f1(self):
        recent_date = date.today().isoformat()
        no_flags = compute_structure_anchor({
            "assessment_date": recent_date,
            "grip_left": 40, "grip_right": 40,
            "prac_impression": 7,
            "flag_pelvic_compensation": False,
            "flag_lumbar_compensation": False,
            "flag_rib_flare": False,
            "flag_cervical_pain": False,
        })
        with_flags = compute_structure_anchor({
            "assessment_date": recent_date,
            "grip_left": 40, "grip_right": 40,
            "prac_impression": 7,
            "flag_pelvic_compensation": True,
            "flag_lumbar_compensation": True,
            "flag_rib_flare": False,
            "flag_cervical_pain": False,
        })
        assert with_flags["f1"] < no_flags["f1"]

    def test_urgent_flag_penalty(self):
        recent_date = date.today().isoformat()
        base = {
            "assessment_date": recent_date,
            "grip_left": 40, "grip_right": 40,
            "prac_impression": 7,
            "flag_pelvic_compensation": False,
            "flag_lumbar_compensation": False,
            "flag_rib_flare": False,
            "flag_cervical_pain": False,
        }
        no_urgent = compute_structure_anchor({**base, "prac_urgent_flag": False})
        urgent = compute_structure_anchor({**base, "prac_urgent_flag": True})
        assert urgent["f1"] < no_urgent["f1"]


# ═══════════════════════════════════════════════
# CHECK-IN PROCESSING & GAPS
# ═══════════════════════════════════════════════

class TestProcessCheckinsAndGaps:
    def test_normalizes_checkins(self):
        today = {"checkin_q1": 4, "checkin_q2": 5}
        result = process_checkins_and_gaps(today)
        assert "q1_norm" in result
        assert "q2_norm" in result
        assert result["q1_norm"] == 50  # (4-1)/6*100

    def test_q3_nonlinear(self):
        today = {"checkin_q3": 4}
        result = process_checkins_and_gaps(today)
        assert result["q3_norm"] == 50

    def test_gap_sleep_computed(self):
        today = {
            "checkin_q1": 7,  # q1_norm = 100
            "sleep_score_normalized": 80,
        }
        result = process_checkins_and_gaps(today)
        assert "gap_sleep" in result
        # gap = 100 - abs(100 - 80) = 80
        assert result["gap_sleep"] == 80

    def test_gap_not_computed_without_data(self):
        today = {"checkin_q1": 4}
        result = process_checkins_and_gaps(today)
        assert "gap_sleep" not in result

    def test_perfect_alignment(self):
        today = {
            "checkin_q1": 4,  # q1_norm = 50
            "sleep_score_normalized": 50,
        }
        result = process_checkins_and_gaps(today)
        assert result["gap_sleep"] == 100  # perfect alignment


# ═══════════════════════════════════════════════
# TIER 2 SCORE
# ═══════════════════════════════════════════════

class TestComputeTier2Score:
    def test_with_valid_data(self):
        today = {"hrv_overnight_rmssd": 70, "resting_heart_rate": 55, "stress_proxy_normalized": 65}
        result = compute_tier2_score("F4", today)
        assert result is not None
        assert 0 <= result <= 100

    def test_missing_all_data(self):
        result = compute_tier2_score("F4", {})
        assert result is None

    def test_partial_data(self):
        today = {"hrv_overnight_rmssd": 70}
        result = compute_tier2_score("F4", today)
        assert result is not None

    def test_unknown_field(self):
        result = compute_tier2_score("F99", {"foo": 1})
        assert result is None

    def test_zero_values_skipped(self):
        # raw == 0 is skipped in the function
        today = {"hrv_overnight_rmssd": 0, "resting_heart_rate": 60}
        result = compute_tier2_score("F4", today)
        assert result is not None  # only RHR used


# ═══════════════════════════════════════════════
# TIER 3 (VOICE EXTRACTION)
# ═══════════════════════════════════════════════

class TestParseTier3Scores:
    def test_none(self):
        result = parse_tier3_scores(None)
        assert all(v is None for v in result.values())

    def test_empty_string(self):
        result = parse_tier3_scores("")
        assert all(v is None for v in result.values())

    def test_valid_extraction(self):
        ve = {
            "overall_tone": "positive",
            "signals": [
                {
                    "direction": "positive",
                    "confidence": 0.8,
                    "fields": [
                        {"field": "F4", "weight": 0.7},
                        {"field": "F5", "weight": 0.5},
                    ],
                }
            ],
        }
        result = parse_tier3_scores(json.dumps(ve))
        assert result["F4"] is not None
        assert result["F4"] > 0
        assert result["F5"] is not None

    def test_negative_tone(self):
        ve = {
            "overall_tone": "negative",
            "signals": [
                {
                    "direction": "negative",
                    "confidence": 0.9,
                    "fields": [{"field": "F7", "weight": 0.8}],
                }
            ],
        }
        result = parse_tier3_scores(json.dumps(ve))
        assert result["F7"] is not None
        assert result["F7"] < 50


# ═══════════════════════════════════════════════
# FIELD SCORE BLENDING
# ═══════════════════════════════════════════════

class TestComputeFieldScore:
    def test_all_tiers(self):
        score, conf = compute_field_score("F7", t1s=60, t2s=70, t3s=55, t1age=3)
        assert score is not None
        assert conf == "High"
        assert 55 <= score <= 70

    def test_only_tier2(self):
        score, conf = compute_field_score("F7", t1s=None, t2s=70, t3s=None)
        assert score == 70
        assert conf == "Medium"

    def test_no_data(self):
        score, conf = compute_field_score("F7", t1s=None, t2s=None, t3s=None)
        assert score is None
        assert conf == "Low"

    def test_tier1_freshness_decay(self):
        fresh, _ = compute_field_score("F7", t1s=80, t2s=50, t3s=None, t1age=1)
        old, _ = compute_field_score("F7", t1s=80, t2s=50, t3s=None, t1age=50)
        # Fresh t1 should pull score higher toward 80
        assert fresh > old

    def test_structure_anchor_applied(self):
        anchor = {"f1": 70, "f2": 60, "f3": 65, "valid": True, "age_days": 5}
        score, conf = compute_field_score("F1", t1s=None, t2s=50, t3s=None, structure_anchor=anchor)
        # 70*0.75 + 50*0.25 = 65
        assert score == 65.0
        assert conf == "High"

    def test_structure_anchor_without_drift(self):
        anchor = {"f1": 70, "f2": 60, "f3": 65, "valid": True, "age_days": 5}
        score, conf = compute_field_score("F1", t1s=None, t2s=None, t3s=None, structure_anchor=anchor)
        assert score == 70.0

    def test_structure_anchor_not_applied_to_f7(self):
        anchor = {"f1": 70, "f2": 60, "f3": 65, "valid": True, "age_days": 5}
        score, conf = compute_field_score("F7", t1s=None, t2s=50, t3s=None, structure_anchor=anchor)
        # Anchor should NOT be applied to F7
        assert score == 50.0

    def test_invalid_anchor_falls_back(self):
        anchor = {"f1": 70, "f2": 60, "f3": 65, "valid": False, "age_days": 35}
        score, conf = compute_field_score("F1", t1s=None, t2s=50, t3s=None, structure_anchor=anchor)
        assert score == 50.0  # no anchor, just tier2


# ═══════════════════════════════════════════════
# LAYER SCORE
# ═══════════════════════════════════════════════

class TestComputeLayerScore:
    def test_uniform_scores(self):
        result = compute_layer_score([70, 70, 70])
        assert result == 70  # no penalty when stdev=0

    def test_divergent_scores(self):
        result = compute_layer_score([90, 50, 70])
        assert result < 70  # mean=70, penalty applied

    def test_all_none(self):
        result = compute_layer_score([None, None, None])
        assert result is None

    def test_partial_none(self):
        result = compute_layer_score([80, None, 80])
        assert result == 80  # stdev=0

    def test_penalty_proportional_to_divergence(self):
        low_div = compute_layer_score([68, 70, 72])
        high_div = compute_layer_score([40, 70, 100])
        assert low_div > high_div


# ═══════════════════════════════════════════════
# OVERALL SCORE + BANDS
# ═══════════════════════════════════════════════

class TestComputeOverallScore:
    def test_uniform_layers(self):
        score, band = compute_overall_score([90, 90, 90, 90])
        assert score == 90
        assert band == "Deep"

    def test_all_none(self):
        score, band = compute_overall_score([None, None, None, None])
        assert score is None
        assert band == "Systemic"

    def test_band_boundaries(self):
        score, band = compute_overall_score([85, 85, 85, 85])
        assert band == "Deep"

        score, band = compute_overall_score([60, 60, 60, 60])
        assert band == "Functional"

        score, band = compute_overall_score([30, 30, 30, 30])
        assert band == "Emerging"

        score, band = compute_overall_score([15, 15, 15, 15])
        assert band == "Fragmented"

        score, band = compute_overall_score([5, 5, 5, 5])
        assert band == "Systemic"

    def test_cross_layer_penalty(self):
        uniform_score, _ = compute_overall_score([70, 70, 70, 70])
        divergent_score, _ = compute_overall_score([40, 60, 80, 100])
        assert divergent_score < uniform_score


# ═══════════════════════════════════════════════
# TRAJECTORY
# ═══════════════════════════════════════════════

class TestTrajectory:
    def test_linear_regression_flat(self):
        vals = [(0, 50), (1, 50), (2, 50)]
        assert linear_regression_slope(vals) == 0

    def test_linear_regression_rising(self):
        vals = [(0, 40), (1, 50), (2, 60)]
        assert linear_regression_slope(vals) == 10

    def test_classify_improving(self):
        d, v = classify_direction(2.0, [1, 2, 3])
        assert d == "Improving"

    def test_classify_declining(self):
        d, v = classify_direction(-2.0, [3, 2, 1])
        assert d == "Declining"

    def test_classify_stable(self):
        d, v = classify_direction(0.1, [50, 50, 50])
        assert d == "Stable"

    def test_too_few_values(self):
        d, v = classify_direction(5.0, [50, 60])
        assert d == "Stable"  # <3 values

    def test_compute_trajectory_empty(self):
        d, v = compute_trajectory([], 7)
        assert d == "Stable"

    def test_compute_trajectory_rising(self):
        hist = [40, 45, 50, 55, 60, 65, 70]
        d, v = compute_trajectory(hist, 7)
        assert d == "Improving"


# ═══════════════════════════════════════════════
# PRIORITY FLAGS
# ═══════════════════════════════════════════════

class TestPriorityFlags:
    def test_low_score_flagged(self):
        fs = {f"F{i}": 50 for i in range(1, 13)}
        fs["F4"] = 20  # below 30
        flags = compute_priority_flags(fs, {}, 50, {})
        assert any(f.get("field") == "F4" and f.get("type") == "low_score" for f in flags)

    def test_no_flags_healthy(self):
        fs = {f"F{i}": 65 for i in range(1, 13)}
        ls = {"structure": 65, "electricity": 65, "energy": 65, "regulation": 65}
        traj = {"overall": {"micro_dir": "Stable", "meso_dir": "Stable"}}
        flags = compute_priority_flags(fs, ls, 65, traj)
        assert len(flags) == 0

    def test_high_divergence_flagged(self):
        fs = {f"F{i}": 60 for i in range(1, 13)}
        fs["F1"] = 20  # creates high stdev in structure layer
        fs["F3"] = 95
        flags = compute_priority_flags(fs, {}, 50, {})
        assert any(f.get("type") == "high_divergence" for f in flags)

    def test_sustained_decline_flagged(self):
        traj = {"overall": {"micro_dir": "Declining", "meso_dir": "Declining"}}
        fs = {f"F{i}": 50 for i in range(1, 13)}
        flags = compute_priority_flags(fs, {}, 50, traj)
        assert any(f.get("type") == "sustained_decline" for f in flags)


# ═══════════════════════════════════════════════
# ACTIVITY COUNTS
# ═══════════════════════════════════════════════

class TestActivityCounts:
    def test_empty(self):
        c7, c28 = compute_activity_counts([], [])
        assert c7 == 0
        assert c28 == 0

    def test_none(self):
        c7, c28 = compute_activity_counts(None, None)
        assert c7 == 0
        assert c28 == 0

    def test_counts(self):
        c7, c28 = compute_activity_counts([1, 2, 3], [1, 2, 3, 4, 5])
        assert c7 == 3
        assert c28 == 5


class TestCategoryDiversity:
    def test_empty(self):
        assert compute_category_diversity([]) == 0

    def test_none(self):
        assert compute_category_diversity(None) == 0

    def test_distinct_categories(self):
        activities = [
            {"sport_category": "Running"},
            {"sport_category": "Swimming"},
            {"sport_category": "Running"},
            {"sport_category": "Cycling"},
        ]
        assert compute_category_diversity(activities) == 3


# ═══════════════════════════════════════════════
# SAFE_NUM
# ═══════════════════════════════════════════════

class TestSafeNum:
    def test_none(self):
        assert safe_num(None) == 0

    def test_value(self):
        assert safe_num(42) == 42

    def test_zero(self):
        assert safe_num(0) == 0


# ═══════════════════════════════════════════════
# INTEGRATION: FULL SCORING PIPELINE
# ═══════════════════════════════════════════════

class TestIntegrationPipeline:
    """End-to-end test with realistic data."""

    def _build_today(self):
        return {
            "hrv_overnight_rmssd": 55,
            "resting_heart_rate": 58,
            "stress_proxy_normalized": 62,
            "sleep_deep_pct": 20,
            "sleep_rem_pct": 22,
            "sleep_score_normalized": 72,
            "sleep_duration_minutes": 450,
            "respiration_rate_avg": 14,
            "readiness_score_normalized": 68,
            "calories_active": 350,
            "steps": 8500,
            "checkin_q1": 5,
            "checkin_q2": 4,
            "checkin_q3": 5,
            "checkin_q4": 5,
            "checkin_q5": 3,
            "checkin_q6": 5,
            "checkin_q7": 4,
        }

    def test_all_fields_scored(self):
        today = self._build_today()
        today = process_checkins_and_gaps(today)
        today["activity_count_7d"] = 3
        today["category_diversity"] = 2
        today["load_ratio"] = 1.1

        for i in range(1, 13):
            fid = f"F{i}"
            t2 = compute_tier2_score(fid, today)
            score, conf = compute_field_score(fid, None, t2, None)
            if t2 is not None:
                assert score is not None, f"{fid} should have a score"
                assert 0 <= score <= 100, f"{fid} score out of range: {score}"

    def test_layers_computed(self):
        fs = {f"F{i}": 60 + i for i in range(1, 13)}
        for ln, lf in LAYERS.items():
            layer_scores = [fs[f] for f in lf]
            result = compute_layer_score(layer_scores)
            assert result is not None
            assert 0 <= result <= 100

    def test_overall_and_band(self):
        ls = [65, 70, 60, 72]
        score, band = compute_overall_score(ls)
        assert score is not None
        assert band in ["Deep", "Functional", "Emerging", "Fragmented", "Systemic"]

    def test_coherence_penalty_matters(self):
        """Verify that incoherent systems score lower than coherent ones
        even when the average is identical."""
        coherent = compute_overall_score([65, 65, 65, 65])
        incoherent = compute_overall_score([35, 65, 65, 95])
        assert coherent[0] > incoherent[0]
