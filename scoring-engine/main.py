"""
MANA³ S3 — Coherence Scoring Engine v4.1
v5: ambulatory activity (steps / calories) is read from YESTERDAY'S COMPLETED
record, never from today's partial day. S3 runs at 10:45, when only a third of
today's movement exists; scoring it as a whole day cratered F3 and F7 every
morning and fired false low-score flags. This also matches the three-phase
model: yesterday's load is the stimulus last night's recovery answered.
v4: data_state gate (full/partial/stale/no_sync). Sentinel-zero sanitation at
the ingress boundary (S3 sends 0 for empty Notion numbers), stale carry-forward
detection against yesterday's record, and score freezing on stale/no_sync days.
v3: Integration of S8 Subjective Check-Ins (1-7 scale), F6 gaps.
"""
import sentry_sdk

sentry_sdk.init(
    dsn="https://fcfb3ab2657ca0f6e7a314b7cb5019dd@o4511251750322176.ingest.de.sentry.io/4511258499612752",
    traces_sample_rate=0.2,
    environment="production",
)

import json
import math
from statistics import mean, stdev

from data_state import compute_data_state, RECOVERY_BLOCK_FIELDS

# Every nightly-sync field. On stale/no_sync days these are wiped before
# Tier-2 scoring so no field is scored from fabricated data.
NIGHT_FIELDS = RECOVERY_BLOCK_FIELDS + ("sleep_deep_pct", "sleep_rem_pct")

# S3 (Make) maps empty Notion numbers to 0 via ifempty(x; 0). 0 is never a
# real value for these fields, so 0 -> None at the boundary. Activity fields
# (steps, calories) are excluded: their zeros stay ambiguous by design.
SENTINEL_ZERO_FIELDS = NIGHT_FIELDS + (
    "overall_score", "layer_structure", "layer_electricity", "layer_energy",
    "layer_regulation",
) + tuple(f"f{i}_score" for i in range(1, 13))


def sanitize_sentinel_zeros(d):
    if not d:
        return {}
    # stress_proxy_normalized is the one night field where 0 is a REAL reading
    # (Polar ans_charge_status 5 -> stress 0). Treat its 0 as a sentinel only
    # when the rest of the recharge block is absent too.
    recharge_present = any(
        (d.get(f) or 0) > 0
        for f in ("hrv_overnight_rmssd", "resting_heart_rate", "respiration_rate_avg")
    )
    for f in SENTINEL_ZERO_FIELDS:
        if d.get(f) == 0:
            if f == "stress_proxy_normalized" and recharge_present:
                continue
            d[f] = None
    return d

INTRA_LAYER_PENALTY_MULTIPLIER = 25
CROSS_LAYER_PENALTY_MULTIPLIER = 30

COHERENCE_BANDS = [
    (85, "Deep"),
    (60, "Functional"),
    (30, "Emerging"),
    (15, "Fragmented"),
    (0,  "Systemic"),
]

LAYERS = {
    "structure":   ["F1", "F2", "F3"],
    "electricity": ["F4", "F5", "F6"],
    "energy":      ["F7", "F8", "F9"],
    "regulation":  ["F10", "F11", "F12"],
}

# ═══════════════════════════════════════════════
# Ambulatory volume accumulates across the calendar day. S3 runs at 10:45, when
# today's figure is a fraction of the eventual total, so it is never a valid
# scoring input. F3 and F7 read these, so they are sourced from yesterday's
# finished day instead - which also matches the three-phase model: yesterday's
# load is the stimulus last night's recovery answered.
COMPLETED_DAY_FIELDS = ("steps", "calories_active", "calories_total")


def resolve_completed_day_activity(today, yesterday):
    """
    Replace today's partial ambulatory figures with yesterday's completed ones.

    Returns (today, activity_source):
      'yesterday' - yesterday's finished day is in use (normal)
      'none'      - no usable yesterday record (first day, or a gap). Fields are
                    set to None so the affected sub-metrics are skipped rather
                    than scored from a partial day.
    Today's raw partials are kept under '<field>_today_partial' for display;
    nothing in scoring reads them.
    """
    for f in COMPLETED_DAY_FIELDS:
        today[f"{f}_today_partial"] = today.get(f)

    # S3 sends 0 for empty Notion numbers and steps/calories are deliberately
    # excluded from sentinel sanitation (a real 0-step day is conceivable), so
    # 0 must be treated as absent HERE or a yesterday record with no wearable
    # data at all would score F3/F7 from a fabricated zero.
    usable = bool(yesterday) and any(
        (yesterday.get(f) or 0) > 0 for f in COMPLETED_DAY_FIELDS
    )
    if not usable:
        for f in COMPLETED_DAY_FIELDS:
            today[f] = None
        return today, "none"

    for f in COMPLETED_DAY_FIELDS:
        today[f] = yesterday.get(f)
    return today, "yesterday"


# TIER 2 FIELD MAP
# ═══════════════════════════════════════════════
TIER2_FIELD_MAP = {
    "F1": [  # Mechanical Integrity
        ("load_ratio", "load_ratio_norm"),
        ("activity_count_7d", "freq_norm"),
    ],
    "F2": [  # Structural Adaptability
        ("category_diversity", "diversity_norm"),
        ("activity_count_7d", "freq_norm"),
    ],
    "F3": [  # Gravitational Efficiency
        ("steps", "steps_structure_norm"),
        ("calories_active", "cal_structure_norm"),
    ],
    "F4": [  # Autonomic Balance
        ("hrv_overnight_rmssd", "hrv_norm"),
        ("resting_heart_rate", "rhr_norm"),
        ("stress_proxy_normalized", "direct"),
    ],
    "F5": [  # Neural Signal Quality
        ("sleep_deep_pct", "deep_norm"),
        ("sleep_rem_pct", "rem_norm"),
        ("sleep_score_normalized", "direct"),
    ],
    "F6": [  # Interoceptive Coherence — Subjective/Objective Gaps (S8 Check-ins)
        ("gap_sleep", "direct"),
        ("gap_recovery", "direct"),
        ("gap_stress", "direct"),
    ],
    "F7": [  # Metabolic Efficiency
        ("calories_active", "cal_norm"),
        ("steps", "steps_norm"),
    ],
    "F8": [  # Hormonal Coherence
        ("sleep_duration_minutes", "duration_norm"),
        ("respiration_rate_avg", "resp_norm"),
    ],
    "F9": [  # Cellular Vitality
        ("readiness_score_normalized", "direct"),
    ],
    "F10": [  # Recovery Architecture
        ("sleep_score_normalized", "direct"),
        ("readiness_score_normalized", "direct"),
    ],
    "F11": [  # Stress Resilience
        ("stress_proxy_normalized", "direct"),
        ("hrv_overnight_rmssd", "hrv_norm"),
    ],
    "F12": [  # Adaptive Intelligence
        ("readiness_score_normalized", "direct"),
        ("sleep_score_normalized", "direct"),
        ("stress_proxy_normalized", "direct"),
    ],
}

TIER_WEIGHTS_DEFAULT = {"tier1": 0.45, "tier2": 0.35, "tier3": 0.20}


# ═══════════════════════════════════════════════
# NORMALIZATION FUNCTIONS
# ═══════════════════════════════════════════════

def clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))

def normalize_hrv(v):
    return clamp((v - 20) / (120 - 20) * 100) if v else None

def normalize_rhr(v):
    return clamp((80 - v) / (80 - 40) * 100) if v else None

def normalize_deep_sleep(p):
    if p is None: return None
    if 15 <= p <= 25: return clamp(70 + (p - 15) / 10 * 30)
    elif p < 15: return clamp(p / 15 * 70)
    else: return clamp(100 - (p - 25) / 15 * 30)

def normalize_rem_sleep(p):
    if p is None: return None
    if 20 <= p <= 25: return clamp(70 + (p - 20) / 5 * 30)
    elif p < 20: return clamp(p / 20 * 70)
    else: return clamp(100 - (p - 25) / 15 * 30)

def normalize_duration(minutes):
    if minutes is None: return None
    if 420 <= minutes <= 480: return clamp(80 + (minutes - 420) / 60 * 20)
    elif minutes < 420: return clamp(minutes / 420 * 80)
    else: return clamp(100 - (minutes - 480) / 120 * 30)

def normalize_respiration(rate):
    if rate is None: return None
    if 12 <= rate <= 16: return clamp(75 + (16 - rate) / 4 * 25)
    elif rate < 12: return clamp(50 + rate / 12 * 25)
    else: return clamp(75 - (rate - 16) / 8 * 50)

def normalize_calories(active_cal):
    if active_cal is None: return None
    return clamp(active_cal / 600 * 80)

def normalize_steps(steps):
    if steps is None: return None
    return clamp(steps / 10000 * 75)

def normalize_load_ratio(ratio):
    if ratio is None or ratio == 0: return None
    if 0.8 <= ratio <= 1.3:
        return clamp(70 + (1.0 - abs(ratio - 1.05)) / 0.25 * 30)
    elif ratio < 0.8:
        return clamp(ratio / 0.8 * 70)
    else:
        return clamp(max(20, 70 - (ratio - 1.3) / 0.7 * 50))

def normalize_frequency(count_7d):
    if count_7d is None: return None
    count = float(count_7d)
    if count == 0: return 10
    if 3 <= count <= 5: return clamp(70 + (count - 3) / 2 * 30)
    elif count < 3: return clamp(20 + count / 3 * 50)
    else:
        return clamp(max(50, 100 - (count - 5) / 3 * 30))

def normalize_diversity(diversity_count):
    if diversity_count is None: return None
    d = float(diversity_count)
    if d == 0: return 5
    if d == 1: return 30
    if d == 2: return 55
    if d == 3: return 75
    if d >= 4: return clamp(80 + min(d - 4, 2) * 10)
    return 50

def normalize_steps_structure(steps):
    if steps is None: return None
    if 6000 <= steps <= 10000:
        return clamp(65 + (steps - 6000) / 4000 * 35)
    elif steps < 6000:
        return clamp(steps / 6000 * 65)
    else:
        return clamp(min(100, 100 - (steps - 10000) / 10000 * 15))

def normalize_calories_structure(active_cal):
    if active_cal is None: return None
    if 200 <= active_cal <= 500:
        return clamp(60 + (active_cal - 200) / 300 * 40)
    elif active_cal < 200:
        return clamp(active_cal / 200 * 60)
    else:
        return clamp(min(100, 100 - (active_cal - 500) / 500 * 10))

NORMALIZERS = {
    "direct": lambda v: v,
    "hrv_norm": normalize_hrv,
    "rhr_norm": normalize_rhr,
    "deep_norm": normalize_deep_sleep,
    "rem_norm": normalize_rem_sleep,
    "duration_norm": normalize_duration,
    "resp_norm": normalize_respiration,
    "cal_norm": normalize_calories,
    "steps_norm": normalize_steps,
    "load_ratio_norm": normalize_load_ratio,
    "freq_norm": normalize_frequency,
    "diversity_norm": normalize_diversity,
    "steps_structure_norm": normalize_steps_structure,
    "cal_structure_norm": normalize_calories_structure,
}

# ROM reference ranges for normalization: (min_poor, max_good)
ROM_REFERENCE_RANGES = {
    "rom_ktw_left":       (3,  12),
    "rom_ktw_right":      (3,  12),
    "rom_aslr_left":      (45, 90),
    "rom_aslr_right":     (45, 90),
    "rom_hip_ir_left":    (25, 50),
    "rom_hip_ir_right":   (25, 50),
    "rom_hip_er_left":    (30, 55),
    "rom_hip_er_right":   (30, 55),
    "rom_thoracic_left":  (35, 60),
    "rom_thoracic_right": (35, 60),
    "rom_shoulder_left":  (150, 180),
    "rom_shoulder_right": (150, 180),
    "rom_cervical_left":  (60, 80),
    "rom_cervical_right": (60, 80),
}

def normalize_rom(value, min_poor, max_good):
    """Normalize a ROM value against reference range. Returns 0–100."""
    if value is None:
        return None
    v = float(value)
    if v >= max_good:
        return 100.0
    elif v <= min_poor:
        return 0.0
    else:
        return clamp((v - min_poor) / (max_good - min_poor) * 100)

def normalize_grip(kg):
    """Normalize grip strength. 20kg=0, 60kg=100."""
    if kg is None:
        return None
    return clamp((float(kg) - 20) / (60 - 20) * 100)

def normalize_balance(seconds):
    """Normalize single-leg balance. 0s=0, 60s=100."""
    if seconds is None:
        return None
    return clamp(float(seconds) / 60 * 100)

def compute_structure_anchor(assessment):
    """
    Compute F1/F2/F3 anchor scores (0–100) from a Structure Assessment record.
    Returns dict: {"f1": score|None, "f2": score|None, "f3": score|None,
                   "age_days": int, "valid": bool}
    """
    from datetime import date
    import datetime

    if not assessment:
        return {"f1": None, "f2": None, "f3": None, "age_days": None, "valid": False}

    # ── Assessment age ──
    assessment_date_str = assessment.get("assessment_date")
    age_days = None
    if assessment_date_str:
        try:
            a_date = datetime.date.fromisoformat(str(assessment_date_str)[:10])
            age_days = (date.today() - a_date).days
        except (ValueError, TypeError):
            pass

    # Only use assessment within 28-day window
    if age_days is None or age_days > 28:
        return {"f1": None, "f2": None, "f3": None, "age_days": age_days, "valid": False}

    def g(key):
        v = assessment.get(key)
        return float(v) if v not in (None, "", False, True) else None

    def gb(key):
        v = assessment.get(key)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.lower() == "true"
        return bool(v) if v is not None else False

    # ── F1: Mechanical Integrity ──
    flag_count = sum([
        gb("flag_pelvic_compensation"),
        gb("flag_lumbar_compensation"),
        gb("flag_rib_flare"),
        gb("flag_cervical_pain"),
    ])
    flag_score = clamp(100 - (flag_count * 20))

    grip_l = normalize_grip(g("grip_left"))
    grip_r = normalize_grip(g("grip_right"))
    grip_scores = [s for s in [grip_l, grip_r] if s is not None]
    grip_score = mean(grip_scores) if grip_scores else None

    impression = g("prac_impression")
    impression_score = clamp(impression * 10) if impression is not None else None

    urgent_penalty = 15 if gb("prac_urgent_flag") else 0

    f1_components = [s for s in [flag_score, grip_score, impression_score] if s is not None]
    f1_raw = mean(f1_components) if f1_components else None
    f1 = round(clamp(f1_raw - urgent_penalty), 1) if f1_raw is not None else None

    # ── F2: Structural Adaptability ──
    rom_scores = []
    for key, (min_poor, max_good) in ROM_REFERENCE_RANGES.items():
        val = g(key)
        if val is not None:
            s = normalize_rom(val, min_poor, max_good)
            if s is not None:
                rom_scores.append(s)
    rom_avg = mean(rom_scores) if rom_scores else None

    fms = g("fms_composite")
    fms_score = clamp(fms / 21 * 100) if fms is not None else None

    f2_components = [s for s in [rom_avg, fms_score] if s is not None]
    if rom_avg is not None and fms_score is not None:
        f2 = round(clamp(rom_avg * 0.6 + fms_score * 0.4), 1)
    elif f2_components:
        f2 = round(mean(f2_components), 1)
    else:
        f2 = None

    # ── F3: Gravitational Efficiency ──
    bal_l = normalize_balance(g("balance_left"))
    bal_r = normalize_balance(g("balance_right"))
    bal_scores = [s for s in [bal_l, bal_r] if s is not None]
    bal_score = mean(bal_scores) if bal_scores else None

    f3_components = [s for s in [bal_score, grip_score, fms_score] if s is not None]
    if f3_components:
        weights_map = []
        if bal_score is not None:
            weights_map.append((bal_score, 0.5))
        if grip_score is not None:
            weights_map.append((grip_score, 0.25))
        if fms_score is not None:
            weights_map.append((fms_score, 0.25))
        if weights_map:
            total_w = sum(w for _, w in weights_map)
            f3 = round(clamp(sum(s * w for s, w in weights_map) / total_w), 1)
        else:
            f3 = None
    else:
        f3 = None

    return {
        "f1": f1, "f2": f2, "f3": f3,
        "age_days": age_days, "valid": True
    }


# ═══════════════════════════════════════════════
# CHECK-IN PROCESSING & GAP CALCULATIONS (S8)
# ═══════════════════════════════════════════════

def process_checkins_and_gaps(today):
    """Normalize 1-7 check-in scores and compute calibration gaps."""
    for q in [1, 2, 4, 5, 6, 7]:
        val = today.get(f"checkin_q{q}")
        if val is not None and str(val).strip() != "":
            today[f"q{q}_norm"] = clamp((float(val) - 1) / 6.0 * 100)

    q3_map = {1: 2, 2: 18, 3: 35, 4: 50, 5: 68, 6: 84, 7: 100}
    q3_val = today.get("checkin_q3")
    if q3_val is not None and str(q3_val).strip() != "":
        today["q3_norm"] = float(q3_map.get(int(float(q3_val)), 50))

    if "q1_norm" in today and today.get("sleep_score_normalized") not in [None, ""]:
        today["gap_sleep"] = clamp(100 - abs(today["q1_norm"] - float(today["sleep_score_normalized"])))

    if "q2_norm" in today and today.get("readiness_score_normalized") not in [None, ""]:
        today["gap_recovery"] = clamp(100 - abs(today["q2_norm"] - float(today["readiness_score_normalized"])))

    if "q5_norm" in today and today.get("stress_proxy_normalized") not in [None, ""]:
        today["gap_stress"] = clamp(100 - abs(today["q5_norm"] - float(today["stress_proxy_normalized"])))

    return today


# ═══════════════════════════════════════════════
# TIER 2 SCORE COMPUTATION
# ═══════════════════════════════════════════════

def compute_tier2_score(field_id, today):
    mappings = TIER2_FIELD_MAP.get(field_id, [])
    if not mappings:
        return None

    scores = []
    for metric_key, norm_key in mappings:
        raw = today.get(metric_key)
        if raw is not None and raw != "" and raw != 0:
            normalizer = NORMALIZERS.get(norm_key, lambda v: v)
            try:
                normalized = normalizer(float(raw))
                if normalized is not None:
                    scores.append(normalized)
            except (ValueError, TypeError):
                pass

    if not scores:
        return None
    return round(mean(scores), 1)


# ═══════════════════════════════════════════════
# TIER 3 SCORE COMPUTATION
# ═══════════════════════════════════════════════

def parse_tier3_scores(voice_extraction):
    tier3 = {f"F{i}": None for i in range(1, 13)}
    if not voice_extraction:
        return tier3

    if isinstance(voice_extraction, str):
        try:
            ve = json.loads(voice_extraction.replace("'", '"'))
        except (json.JSONDecodeError, ValueError):
            return tier3
    else:
        ve = voice_extraction

    signals = ve.get("signals", [])
    field_accum = {f"F{i}": [] for i in range(1, 13)}
    base_mod = 0
    tone = ve.get("overall_tone", "neutral")
    if tone == "positive":
        base_mod = 5
    elif tone == "negative":
        base_mod = -5

    direction_scores = {"positive": 70, "neutral": 50, "negative": 30, "mixed": 45}

    for sig in signals:
        ds = direction_scores.get(sig.get("direction", "neutral"), 50)
        conf = sig.get("confidence", 0.5)
        for fm in sig.get("fields", []):
            fid = fm.get("field", "")
            w = fm.get("weight", 0.5)
            if fid in field_accum:
                field_accum[fid].append(clamp(ds * w * conf + base_mod))

    for fid in tier3:
        if field_accum.get(fid):
            tier3[fid] = round(mean(field_accum[fid]), 1)

    return tier3


# ═══════════════════════════════════════════════
# FIELD SCORE BLENDING
# ═══════════════════════════════════════════════

def compute_field_score(fid, t1s, t2s, t3s, t1age=None, structure_anchor=None):
    """
    Blend tier1/2/3 scores. For F1/F2/F3, if a valid structure anchor exists
    (assessment within 28 days), blend anchor (75%) with wearable drift (25%).
    Falls back to standard blending if no anchor.
    """
    if fid in ("F1", "F2", "F3") and structure_anchor and structure_anchor.get("valid"):
        anchor_score = structure_anchor.get(fid.lower())
        if anchor_score is not None:
            drift_score = t2s
            if drift_score is not None:
                blended = round(clamp(anchor_score * 0.75 + drift_score * 0.25), 1)
            else:
                blended = round(clamp(anchor_score), 1)
            return blended, "High"

    available, weights = {}, {}

    if t1s is not None and t1age is not None:
        f = max(0.1, 1.0 - (t1age / 56))
        weights["t1"] = TIER_WEIGHTS_DEFAULT["tier1"] * f
        available["t1"] = t1s

    if t2s is not None:
        weights["t2"] = TIER_WEIGHTS_DEFAULT["tier2"]
        available["t2"] = t2s

    if t3s is not None:
        weights["t3"] = TIER_WEIGHTS_DEFAULT["tier3"]
        available["t3"] = t3s

    if not available:
        return None, "Low"

    tw = sum(weights.values())
    blended = sum(available[k] * (weights[k] / tw) for k in available)
    conf = "High" if len(available) >= 2 else ("Medium" if len(available) == 1 else "Low")
    return round(clamp(blended), 1), conf


# ═══════════════════════════════════════════════
# LAYER + OVERALL SCORE COMPUTATION
# ═══════════════════════════════════════════════

def compute_layer_score(fs):
    valid = [s for s in fs if s is not None]
    if not valid:
        return None
    avg = mean(valid)
    penalty = stdev(valid) * INTRA_LAYER_PENALTY_MULTIPLIER / 100 if len(valid) >= 2 else 0
    return round(clamp(avg - penalty), 1)

def compute_overall_score(ls):
    valid = [s for s in ls if s is not None]
    if not valid:
        return None, "Systemic"
    avg = mean(valid)
    penalty = stdev(valid) * CROSS_LAYER_PENALTY_MULTIPLIER / 100 if len(valid) >= 2 else 0
    score = round(clamp(avg - penalty), 1)
    band = "Systemic"
    for threshold, name in COHERENCE_BANDS:
        if score >= threshold:
            band = name
            break
    return score, band


# ═══════════════════════════════════════════════
# TRAJECTORY COMPUTATION
# ═══════════════════════════════════════════════

def linear_regression_slope(values):
    if len(values) < 2:
        return 0.0
    xv = [v[0] for v in values]
    yv = [v[1] for v in values]
    xm = mean(xv)
    ym = mean(yv)
    num = sum((x - xm) * (y - ym) for x, y in values)
    den = sum((x - xm) ** 2 for x in xv)
    return num / den if den else 0.0

def classify_direction(slope, values):
    if len(values) < 3:
        return "Stable", 0.0
    velocity = abs(slope)
    if slope > 0.5:
        return "Improving", round(velocity, 2)
    elif slope < -0.5:
        return "Declining", round(velocity, 2)
    else:
        return "Stable", round(velocity, 2)

def compute_trajectory(history, window):
    if not history or len(history) < 3:
        return "Stable", 0.0
    recent = history[-window:] if len(history) >= window else history
    indexed = list(enumerate(recent))
    slope = linear_regression_slope(indexed)
    return classify_direction(slope, recent)


# ═══════════════════════════════════════════════
# PRIORITY FLAGS
# ═══════════════════════════════════════════════

def compute_priority_flags(fs, ls, overall, traj):
    flags = []
    for fid, score in fs.items():
        if score is not None and score < 30:
            flags.append({"field": fid, "type": "low_score", "value": score})
    for ln, lf in LAYERS.items():
        layer_scores = [fs.get(f) for f in lf if fs.get(f) is not None]
        if len(layer_scores) >= 2:
            sd = stdev(layer_scores)
            if sd > 20:
                flags.append({"layer": ln, "type": "high_divergence", "stdev": round(sd, 1)})
    for scope, t in traj.items():
        if t.get("micro_dir") == "Declining" and t.get("meso_dir") == "Declining":
            flags.append({"scope": scope, "type": "sustained_decline"})
    return flags


# ═══════════════════════════════════════════════
# ACTIVITY COUNT COMPUTATION
# ═══════════════════════════════════════════════

def compute_activity_counts(activities_7d, activities_28d):
    count_7d = len(activities_7d) if activities_7d else 0
    count_28d = len(activities_28d) if activities_28d else 0
    return count_7d, count_28d


def compute_category_diversity(activities_28d):
    if not activities_28d:
        return 0
    categories = set()
    for act in activities_28d:
        cat = None
        props = act.get("properties", {})
        if props:
            ac = props.get("activity_category", {})
            if ac.get("type") == "select" and ac.get("select"):
                cat = ac["select"].get("name")
            if not cat:
                at = props.get("activity_type", {})
                if at.get("type") == "rich_text" and at.get("rich_text"):
                    cat = at["rich_text"][0].get("plain_text")
        else:
            cat = act.get("sport_category") or act.get("activity_type") or act.get("category")
        if cat:
            categories.add(str(cat).lower())
    return len(categories)


# ═══════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════

def safe_num(v):
    return 0 if v is None else v

def main(request):
    if request.method == "OPTIONS":
        return ("", 204, {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST",
            "Access-Control-Allow-Headers": "Content-Type",
        })

    headers = {"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"}

    try:
        rd = request.get_json(silent=True)
        if not rd:
            return (json.dumps({"error": "No JSON body"}), 400, headers)

        sentry_sdk.set_tag("client_id", rd.get("client_id", "unknown"))
        sentry_sdk.set_tag("date", rd.get("date", "unknown"))

        today = rd.get("today", {})
        tier1_data = rd.get("tier1", {})
        history = rd.get("history", {})
        activities_7d = rd.get("activities_7d", [])
        activities_28d = rd.get("activities_28d", [])
        structure_assessment_raw = rd.get("structure_assessment", {})

        # ── Data-state gate (Option B) ──
        today = sanitize_sentinel_zeros(today)
        yesterday = sanitize_sentinel_zeros(rd.get("yesterday", {}) or {})
        ds = compute_data_state(today, yesterday)
        sentry_sdk.set_tag("data_state", ds.state)
        if ds.state in ("stale", "no_sync"):
            # Fabricated or absent nightly data: no field may be scored from it.
            for f in NIGHT_FIELDS:
                today[f] = None

        # ── Completed-day activity (v5) ──
        # F3 and F7 read steps/calories. Use yesterday's finished day, never
        # today's partial accumulation.
        today, activity_source = resolve_completed_day_activity(today, yesterday)
        sentry_sdk.set_tag("activity_source", activity_source)

        # Compute structure anchor for F1/F2/F3
        structure_anchor = compute_structure_anchor(structure_assessment_raw)

        # ── Compute activity metrics ──
        count_7d, count_28d = compute_activity_counts(activities_7d, activities_28d)
        category_diversity = compute_category_diversity(activities_28d)

        today["activity_count_7d"] = count_7d
        today["activity_count_28d"] = count_28d
        today["category_diversity"] = category_diversity

        if not today.get("load_ratio") or today.get("load_ratio") == 0:
            load_7d = today.get("load_7d", 0) or 0
            load_28d = today.get("load_28d", 0) or 0
            if load_28d > 0:
                chronic_weekly = load_28d / 4
                if chronic_weekly > 0:
                    today["load_ratio"] = round(load_7d / chronic_weekly, 2)

        # ── Process Check-ins & Gaps (S8 Integration) ──
        today = process_checkins_and_gaps(today)

        # ── Tier 2 scores ──
        t2s = {f"F{i}": compute_tier2_score(f"F{i}", today) for i in range(1, 13)}

        # ── Tier 3 scores ──
        t3s = parse_tier3_scores(today.get("voice_extraction"))

        # ── Blend into field scores ──
        fs, fc, ft3 = {}, {}, {}
        for i in range(1, 13):
            fid = f"F{i}"
            t1 = tier1_data.get(fid, {})
            score, conf = compute_field_score(
                fid, t1.get("score"), t2s.get(fid), t3s.get(fid), t1.get("age_days"),
                structure_anchor=structure_anchor if fid in ("F1", "F2", "F3") else None
            )
            fs[fid] = score
            fc[fid] = conf
            ft3[fid] = t3s.get(fid)

        # ── Layer scores ──
        ls = {
            ln: compute_layer_score([fs.get(f) for f in lf])
            for ln, lf in LAYERS.items()
        }

        # ── Overall score ──
        overall, band = compute_overall_score(list(ls.values()))

        # ── Freeze on stale/no_sync ──
        # The score must not move for pipeline reasons. Carry yesterday's
        # numbers forward verbatim when they exist; the data_state stamp tells
        # S4 and the PWA why. Flags are suppressed: divergence computed from a
        # frozen record is noise.
        frozen = False
        if ds.state in ("stale", "no_sync") and yesterday.get("overall_score") is not None:
            frozen = True
            overall = yesterday["overall_score"]
            band = "Systemic"
            for threshold, name in COHERENCE_BANDS:
                if overall >= threshold:
                    band = name
                    break
            for ln in LAYERS:
                y = yesterday.get(f"layer_{ln}")
                if y is not None:
                    ls[ln] = y
            for i in range(1, 13):
                fid = f"F{i}"
                y = yesterday.get(f"f{i}_score")
                if y is not None:
                    fs[fid] = y
                    fc[fid] = "Low"

        # ── Trajectories ──
        traj = {}
        for scope in ["overall", "structure", "electricity", "energy", "regulation"]:
            key = scope if scope == "overall" else f"layer_{scope}"
            hist = history.get(key, [])
            md, mv = compute_trajectory(hist, 7)
            med, mev = compute_trajectory(hist, 28)
            mad, mav = compute_trajectory(hist, 90)
            traj[scope] = {
                "micro_dir": md, "micro_vel": mv,
                "meso_dir": med, "meso_vel": mev,
                "macro_dir": mad, "macro_vel": mav,
            }

        # ── Priority flags ──
        flags = [] if frozen else compute_priority_flags(fs, ls, overall, traj)

        # ── Build output ──
        output = {
            "data_state": ds.state,
            "data_state_reason": ds.reason,
            "activity_source": activity_source,
            "frozen": frozen,
            "field_scores": {},
            "layer_scores": {
                "structure": safe_num(ls.get("structure")),
                "electricity": safe_num(ls.get("electricity")),
                "energy": safe_num(ls.get("energy")),
                "regulation": safe_num(ls.get("regulation")),
            },
            "overall_score": safe_num(overall),
            "coherence_band": band or "Systemic",
            "trajectories": traj,
            "priority_flags": " | ".join(
                [
                    ", ".join(f"{k}: {v}" for k, v in f.items())
                    for f in flags
                ]
            ) if flags else "None",
            "activity_count_7d": count_7d,
            "activity_count_28d": count_28d,
            "category_diversity": category_diversity,
            "gaps": {
                "sleep": safe_num(today.get("gap_sleep")),
                "recovery": safe_num(today.get("gap_recovery")),
                "stress": safe_num(today.get("gap_stress")),
            },
            "structure_anchor": {
                "f1": structure_anchor.get("f1"),
                "f2": structure_anchor.get("f2"),
                "f3": structure_anchor.get("f3"),
                "age_days": structure_anchor.get("age_days"),
                "valid": structure_anchor.get("valid", False),
            }
        }

        for i in range(1, 13):
            fid = f"F{i}"
            fn = fid.lower()
            output["field_scores"][fn] = {
                "score": safe_num(fs[fid]),
                "confidence": fc[fid] or "Low",
                "tier3": safe_num(ft3[fid]),
            }

        return (json.dumps(output), 200, headers)

    except Exception as e:
        import traceback
        sentry_sdk.capture_exception(e)
        return (json.dumps({"error": str(e), "trace": traceback.format_exc()}), 500, headers)
