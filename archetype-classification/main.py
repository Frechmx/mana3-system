import functions_framework
import json
import math

@functions_framework.http
def compute_archetype(request):
    """
    S10 — Archetype Classification
    Accepts Notion API raw response format directly from Make.com.
    
    Expected JSON body:
    {
        "notion_daily": { "results": [ ...last 4+ weekly records with RC scores... ] },
        "notion_latest": { "results": [ ...most recent scored Daily Record... ] }
    }
    
    Returns:
    {
        "archetype_profile": {
            "Overreacher": 0.15,
            "Guardian": 0.05,
            "Oscillator": 0.10,
            "Plateau": 0.60,
            "Rebuilder": 0.10
        },
        "dominant_archetype": "Plateau",
        "secondary_tendency": "Overreacher",
        "confidence": "Medium",
        "rc_volatility": 3.2,
        "layer_balance": {
            "Structure": 34.8,
            "Electricity": 54.0,
            "Energy": 49.3,
            "Regulation": 52.5
        },
        "key_axis": {
            "F4_F10_deviation": 0.35,
            "F11_F8_deviation": 0.12
        },
        "reasoning": "..."
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
        notion_latest = payload.get("notion_latest", {})

        # ── PARSE HELPERS ──
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

        def get_text(props, key):
            prop = props.get(key, {})
            if prop.get("type") == "rich_text":
                items = prop.get("rich_text", [])
            elif prop.get("type") == "title":
                items = prop.get("title", [])
            else:
                return ""
            if items:
                return items[0].get("plain_text", "")
            return ""

        # ── EXTRACT RC SCORES FROM WEEKLY RECORDS ──
        daily_pages = notion_daily.get("results", [])
        
        # Filter to records that have a relational_coherence_score
        rc_scores = []
        rc_matrices = []
        for page in daily_pages:
            props = page.get("properties", {})
            rc = get_number(props, "relational_coherence_score")
            if rc is not None:
                rc_scores.append(rc)
                # Try to parse the matrix JSON for key axis data
                matrix_str = get_text(props, "relational_matrix_json")
                if matrix_str:
                    try:
                        matrix_data = json.loads(matrix_str)
                        rc_matrices.append(matrix_data)
                    except (json.JSONDecodeError, TypeError):
                        rc_matrices.append(None)
                else:
                    rc_matrices.append(None)

        if len(rc_scores) < 4:
            return (json.dumps({
                "error": "Insufficient data",
                "message": f"Need at least 4 weekly RC scores, have {len(rc_scores)}",
                "dominant_archetype": "Gathering Data",
                "confidence": "None"
            }), 200, headers)

        # Use the last 4 RC scores
        recent_rc = rc_scores[-4:]
        recent_matrices = rc_matrices[-4:]

        # ── EXTRACT LAYER SCORES FROM LATEST RECORD ──
        latest_pages = notion_latest.get("results", [])
        if not latest_pages:
            return (json.dumps({
                "error": "No latest record found",
                "dominant_archetype": "Gathering Data",
                "confidence": "None"
            }), 200, headers)

        latest_props = latest_pages[-1].get("properties", {})
        
        layer_scores = {
            "Structure": get_number(latest_props, "layer_structure"),
            "Electricity": get_number(latest_props, "layer_electricity"),
            "Energy": get_number(latest_props, "layer_energy"),
            "Regulation": get_number(latest_props, "layer_regulation"),
        }

        # Replace None with 0 for computation
        for k in layer_scores:
            if layer_scores[k] is None:
                layer_scores[k] = 0.0

        # ── STEP 1: TRAJECTORY VOLATILITY ──
        rc_mean = sum(recent_rc) / len(recent_rc)
        rc_variance = sum((r - rc_mean) ** 2 for r in recent_rc) / len(recent_rc)
        rc_volatility = math.sqrt(rc_variance)

        # ── STEP 2: LAYER BALANCE SIGNATURE ──
        all_layers = [layer_scores["Structure"], layer_scores["Electricity"],
                      layer_scores["Energy"], layer_scores["Regulation"]]
        layer_mean = sum(all_layers) / 4 if any(v > 0 for v in all_layers) else 0
        layer_stdev = math.sqrt(sum((v - layer_mean) ** 2 for v in all_layers) / 4) if layer_mean > 0 else 0

        # Compute deviations from mean in stdev units
        def layer_z(layer_name):
            if layer_stdev == 0:
                return 0
            return (layer_scores[layer_name] - layer_mean) / layer_stdev

        structure_z = layer_z("Structure")
        electricity_z = layer_z("Electricity")
        energy_z = layer_z("Energy")
        regulation_z = layer_z("Regulation")

        # Output fields are the higher-output layers (Structure + Energy)
        output_z = (structure_z + energy_z) / 2
        # Recovery fields are the regulation-related layers
        recovery_z = regulation_z

        # ── STEP 3: KEY AXIS DEVIATIONS ──
        # Extract from the most recent matrix if available
        f4_f10_dev = None
        f11_f8_dev = None

        latest_matrix = None
        for m in reversed(recent_matrices):
            if m is not None:
                latest_matrix = m
                break

        if latest_matrix:
            matrix_data = latest_matrix.get("matrix", latest_matrix)
            
            f4_f10 = matrix_data.get("F4_F10", {})
            if isinstance(f4_f10, dict):
                f4_f10_dev = f4_f10.get("deviation")

            # F8_F11 or F11_F8
            f11_f8 = matrix_data.get("F11_F8", matrix_data.get("F8_F11", {}))
            if isinstance(f11_f8, dict):
                f11_f8_dev = f11_f8.get("deviation")

        # ── STEP 4: ARCHETYPE SCORING ──
        scores = {
            "Overreacher": 0.0,
            "Guardian": 0.0,
            "Oscillator": 0.0,
            "Plateau": 0.0,
            "Rebuilder": 0.0,
        }
        reasoning_parts = []

        # --- OVERREACHER ---
        # High output (Structure + Energy) > 2 stdev above Regulation
        if output_z > 0.5 and regulation_z < -0.5:
            scores["Overreacher"] += 0.30
            reasoning_parts.append("Output layers above regulation — overreach pattern")
        if output_z > 1.0 and regulation_z < -1.0:
            scores["Overreacher"] += 0.20
            reasoning_parts.append("Strong output-regulation divergence")
        # F4-F10 diverging (high deviation = break in autonomic-recovery axis)
        if f4_f10_dev is not None and f4_f10_dev > 0.25:
            scores["Overreacher"] += 0.25
            reasoning_parts.append(f"F4-F10 axis deviation {f4_f10_dev:.2f} — recovery lagging activation")
        # Low volatility (overreachers are consistent in their overreach)
        if rc_volatility < 5:
            scores["Overreacher"] += 0.10

        # --- GUARDIAN ---
        # High Regulation, low Electricity
        if regulation_z > 0.5 and electricity_z < -0.5:
            scores["Guardian"] += 0.30
            reasoning_parts.append("Strong regulation, suppressed electricity — protective pattern")
        if regulation_z > 1.0 and electricity_z < -1.0:
            scores["Guardian"] += 0.20
            reasoning_parts.append("Strong guardian signature")
        # F4 and F5 depressed (low activation)
        f4_score = get_number(latest_props, "f4_score") or 0
        f5_score = get_number(latest_props, "f5_score") or 0
        if f4_score < 45 and f5_score < 45:
            scores["Guardian"] += 0.20
            reasoning_parts.append("F4 and F5 both below 45 — nervous system in protective mode")
        if rc_volatility < 3:
            scores["Guardian"] += 0.10

        # --- OSCILLATOR ---
        # High RC volatility
        if rc_volatility > 8:
            scores["Oscillator"] += 0.35
            reasoning_parts.append(f"RC volatility {rc_volatility:.1f} — unstable relational coherence")
        elif rc_volatility > 5:
            scores["Oscillator"] += 0.20
            reasoning_parts.append(f"Moderate RC volatility {rc_volatility:.1f}")
        # High layer score variance
        if layer_stdev > 15:
            scores["Oscillator"] += 0.20
            reasoning_parts.append(f"High layer divergence (stdev {layer_stdev:.1f})")
        # F11-F8 regulatory pair unstable
        if f11_f8_dev is not None and f11_f8_dev > 0.30:
            scores["Oscillator"] += 0.15
            reasoning_parts.append(f"F11-F8 regulatory pair deviation {f11_f8_dev:.2f}")

        # --- PLATEAU ---
        # All layers moderate, high RC, flat trajectory
        all_moderate = all(35 < v < 75 for v in all_layers if v > 0)
        if all_moderate and layer_stdev < 10:
            scores["Plateau"] += 0.30
            reasoning_parts.append("All layers moderate and balanced — stable but not improving")
        if rc_volatility < 3:
            scores["Plateau"] += 0.20
            reasoning_parts.append(f"Very low RC volatility {rc_volatility:.1f} — flat trajectory")
        # RC trending flat (last 4 scores similar)
        rc_range = max(recent_rc) - min(recent_rc)
        if rc_range < 5:
            scores["Plateau"] += 0.20
            reasoning_parts.append(f"RC range only {rc_range:.1f} over 4 weeks — stagnant")

        # --- REBUILDER ---
        # Regulation improving while Structure/Energy lag
        # We check if Regulation is highest layer and others are low
        if regulation_z > 0.5 and (structure_z < 0 or energy_z < 0):
            scores["Rebuilder"] += 0.25
            reasoning_parts.append("Regulation leading, other layers lagging — rebuilding pattern")
        # RC trending upward (last score > first score)
        if len(recent_rc) >= 4 and recent_rc[-1] > recent_rc[0] + 3:
            scores["Rebuilder"] += 0.25
            reasoning_parts.append(f"RC improving: {recent_rc[0]:.1f} → {recent_rc[-1]:.1f}")
        # Moderate volatility (rebuilding is dynamic)
        if 3 < rc_volatility < 8:
            scores["Rebuilder"] += 0.10

        # ── STEP 5: NORMALIZE TO PROBABILITY DISTRIBUTION ──
        total = sum(scores.values())
        if total > 0:
            profile = {k: round(v / total, 2) for k, v in scores.items()}
        else:
            # No signals detected — default flat distribution
            profile = {k: 0.20 for k in scores}
            reasoning_parts.append("No strong archetype signals detected — flat distribution")

        # Ensure sums to 1.0
        remainder = 1.0 - sum(profile.values())
        if abs(remainder) > 0.001:
            max_key = max(profile, key=profile.get)
            profile[max_key] = round(profile[max_key] + remainder, 2)

        # ── STEP 6: DETERMINE DOMINANT AND SECONDARY ──
        sorted_archetypes = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        dominant = sorted_archetypes[0][0]
        dominant_prob = sorted_archetypes[0][1]
        secondary = sorted_archetypes[1][0]
        secondary_prob = sorted_archetypes[1][1]

        # Confidence based on separation between dominant and secondary
        if dominant_prob >= 0.45:
            confidence = "High"
        elif dominant_prob >= 0.30:
            confidence = "Medium"
        else:
            confidence = "Low"

        # ── BUILD RESULT ──
        result = {
            "archetype_profile": profile,
            "dominant_archetype": dominant,
            "dominant_probability": dominant_prob,
            "secondary_tendency": secondary,
            "secondary_probability": secondary_prob,
            "confidence": confidence,
            "rc_volatility": round(rc_volatility, 2),
            "rc_scores_used": recent_rc,
            "layer_balance": layer_scores,
            "layer_stdev": round(layer_stdev, 2),
            "key_axis": {
                "F4_F10_deviation": round(f4_f10_dev, 4) if f4_f10_dev is not None else None,
                "F11_F8_deviation": round(f11_f8_dev, 4) if f11_f8_dev is not None else None
            },
            "reasoning": " | ".join(reasoning_parts) if reasoning_parts else "Insufficient signal differentiation"
        }

        # ── PRE-SERIALIZE FOR MAKE.COM ──
        # Make cannot serialize parsed objects in template substitution ({{6.data}} → [object Object]).
        # Pre-escape the JSON so {{6.data.profile_json_string}} is safe inside another JSON body.
        profile_json = json.dumps(result, separators=(",", ":"))
        if len(profile_json) > 1900:
            profile_json = profile_json[:1900]
        result["profile_json_string"] = profile_json.replace('"', '\\"')

        return (json.dumps(result), 200, headers)

    except Exception as e:
        import traceback
        return (json.dumps({
            "error": str(e),
            "trace": traceback.format_exc()
        }), 500, headers)
