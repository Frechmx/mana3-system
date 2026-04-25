import functions_framework
import json
import math

@functions_framework.http
def compute_oscillation(request):
    """
    S11 — Oscillation Analysis
    Accepts Notion API raw response format directly from Make.com.
    
    Expected JSON body:
    {
        "notion_daily": { "results": [ ...Notion page objects... ] }
    }
    """
    # CORS preflight
    if request.method == "OPTIONS":
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST",
            "Access-Control-Allow-Headers": "Content-Type",
            "Access-Control-Max-Age": "3600",
        }
        return ("", 204, headers)

    headers = {"Access-Control-Allow-Origin": "*", "Content-Type": "application/json"}

    try:
        payload = request.get_json()
        notion_daily = payload.get("notion_daily", {})
        daily_pages = notion_daily.get("results", [])

        if len(daily_pages) < 21:
            return (json.dumps({
                "error": "Insufficient data",
                "message": f"Need at least 21 days for weekly rhythm, received {len(daily_pages)}",
                "oscillation_health_index": None
            }), 200, headers)

        # ── PARSE NOTION PAGES ──
        def get_number(props, key):
            prop = props.get(key, {})
            if prop.get("type") == "number":
                return prop.get("number")
            return None

        def get_date(props, key):
            prop = props.get(key, {})
            if prop.get("type") == "date" and prop.get("date"):
                return prop["date"].get("start", "")
            return ""

        hf_fields = ["F4", "F5", "F10", "F11"]
        field_keys = {
            "F4": "f4_score", "F5": "f5_score",
            "F10": "f10_score", "F11": "f11_score"
        }

        daily_scores = []
        for page in daily_pages:
            props = page.get("properties", {})
            record = {"date": get_date(props, "date")}
            for f in hf_fields:
                record[f] = get_number(props, field_keys[f])
            daily_scores.append(record)

        daily_scores.sort(key=lambda d: d.get("date", ""))

        # ── BUILD TIME SERIES ──
        series = {}
        for f in hf_fields:
            series[f] = [d.get(f) for d in daily_scores]

        # ── HELPER FUNCTIONS ──
        def fill_none(arr):
            filled = list(arr)
            last = None
            for i, v in enumerate(filled):
                if v is not None:
                    last = v
                elif last is not None:
                    filled[i] = last
            first = None
            for v in filled:
                if v is not None:
                    first = v
                    break
            if first is not None:
                for i in range(len(filled)):
                    if filled[i] is None:
                        filled[i] = first
                    else:
                        break
            return filled

        def autocorrelation(arr, lag):
            clean = fill_none(arr)
            clean = [v for v in clean if v is not None]
            n = len(clean)
            if n < lag + 10:
                return None
            mean = sum(clean) / n
            var = sum((v - mean) ** 2 for v in clean) / n
            if var == 0:
                return 0.0
            num = sum((clean[i] - mean) * (clean[i + lag] - mean) for i in range(n - lag))
            return num / ((n - lag) * var)

        def cross_correlation_lag0(arr_a, arr_b):
            fa = fill_none(arr_a)
            fb = fill_none(arr_b)
            pairs = [(a, b) for a, b in zip(fa, fb) if a is not None and b is not None]
            n = len(pairs)
            if n < 15:
                return None
            mean_a = sum(p[0] for p in pairs) / n
            mean_b = sum(p[1] for p in pairs) / n
            num = sum((p[0] - mean_a) * (p[1] - mean_b) for p in pairs)
            den_a = math.sqrt(sum((p[0] - mean_a) ** 2 for p in pairs))
            den_b = math.sqrt(sum((p[1] - mean_b) ** 2 for p in pairs))
            if den_a == 0 or den_b == 0:
                return 0.0
            return num / (den_a * den_b)

        def compute_amplitude(arr):
            clean = fill_none(arr)
            clean = [v for v in clean if v is not None]
            if len(clean) < 14:
                return None
            amplitudes = []
            for i in range(0, len(clean) - 6, 7):
                window = clean[i:i + 7]
                if len(window) >= 5:
                    amplitudes.append(max(window) - min(window))
            if not amplitudes:
                return None
            return sum(amplitudes) / len(amplitudes)

        def compute_regularity(arr):
            clean = fill_none(arr)
            clean = [v for v in clean if v is not None]
            if len(clean) < 21:
                return None
            peak_days = []
            for i in range(0, len(clean) - 6, 7):
                window = clean[i:i + 7]
                if len(window) >= 5:
                    peak_idx = window.index(max(window))
                    peak_days.append(peak_idx)
            if len(peak_days) < 3:
                return None
            mean_peak = sum(peak_days) / len(peak_days)
            var = sum((d - mean_peak) ** 2 for d in peak_days) / len(peak_days)
            stdev = math.sqrt(var)
            return max(0, round(1.0 - (stdev / 3.0), 4))

        # ── PER-FIELD RHYTHM ANALYSIS ──
        field_rhythms = {}
        valid_amplitudes = []
        valid_regularities = []

        for f in hf_fields:
            s = series[f]
            ac7 = autocorrelation(s, 7)
            amp = compute_amplitude(s)
            reg = compute_regularity(s)

            field_rhythms[f] = {
                "autocorr_lag7": round(ac7, 4) if ac7 is not None else None,
                "amplitude": round(amp, 2) if amp is not None else None,
                "regularity": round(reg, 4) if reg is not None else None,
                "rhythm_detected": ac7 is not None and ac7 > 0.25
            }
            if amp is not None:
                valid_amplitudes.append(amp)
            if reg is not None:
                valid_regularities.append(reg)

        # ── PHASE ALIGNMENT ──
        phase_pairs_to_check = [
            ("F4", "F10"), ("F4", "F5"),
            ("F10", "F11"), ("F4", "F11"),
        ]
        phase_pairs = {}
        phase_scores = []

        for fa, fb in phase_pairs_to_check:
            pid = f"{fa}_{fb}"
            cc = cross_correlation_lag0(series[fa], series[fb])
            if cc is not None:
                phase_pairs[pid] = {
                    "cross_corr_lag0": round(cc, 4),
                    "in_phase": cc > 0.40
                }
                phase_scores.append(max(0, cc))
            else:
                phase_pairs[pid] = {
                    "cross_corr_lag0": None,
                    "in_phase": None
                }

        # ── COMPOSITE SCORES ──
        if valid_amplitudes:
            mean_amp = sum(valid_amplitudes) / len(valid_amplitudes)
            amp_norm = min(100, max(0, (mean_amp / 30.0) * 100))
        else:
            mean_amp = None
            amp_norm = 0

        if valid_regularities:
            mean_reg = sum(valid_regularities) / len(valid_regularities)
            reg_norm = mean_reg * 100
        else:
            mean_reg = None
            reg_norm = 0

        if phase_scores:
            mean_phase = sum(phase_scores) / len(phase_scores)
            phase_norm = min(100, mean_phase * 100)
        else:
            mean_phase = None
            phase_norm = 0

        ohi = round((amp_norm * 0.30) + (reg_norm * 0.40) + (phase_norm * 0.30), 1)

        rhythm_count = sum(1 for f in field_rhythms.values() if f.get("rhythm_detected"))
        weekly_detected = rhythm_count >= 2

        # ── MONTHLY RHYTHM ──
        monthly = {"detected": False, "dominant_period": None, "amplitude": None}
        if len(daily_scores) >= 56:
            best_ac = None
            best_lag = None
            for lag in range(28, 36):
                ac = autocorrelation(series.get("F4", []), lag)
                if ac is not None and (best_ac is None or ac > best_ac):
                    best_ac = ac
                    best_lag = lag
            if best_ac is not None and best_ac > 0.20:
                monthly = {
                    "detected": True,
                    "dominant_period": best_lag,
                    "amplitude": round(best_ac, 4)
                }

        result = {
            "oscillation_health_index": ohi,
            "weekly_amplitude": round(mean_amp, 2) if mean_amp is not None else None,
            "weekly_regularity": round(mean_reg, 4) if mean_reg is not None else None,
            "phase_alignment_score": round(phase_norm, 1),
            "weekly_rhythm_detected": weekly_detected,
            "monthly_rhythm_detected": monthly["detected"],
            "field_rhythms": field_rhythms,
            "phase_pairs": phase_pairs,
            "monthly": monthly,
            "data_days": len(daily_scores)
        }

        return (json.dumps(result), 200, headers)

    except Exception as e:
        import traceback
        return (json.dumps({
            "error": str(e),
            "trace": traceback.format_exc()
        }), 500, headers)
