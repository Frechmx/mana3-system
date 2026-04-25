import functions_framework
import json
import math

@functions_framework.http
def compute_relational_matrix(request):
    """
    S9 — Relational Matrix Computation
    Updated to accept Notion API raw response format directly from Make.com.
    
    Expected JSON body:
    {
        "notion_daily": { "results": [ ...Notion page objects... ] },
        "notion_pairs": { "results": [ ...Notion page objects... ] },
        "previous_matrix": { "F1_F2": { "actual_r": 0.65 }, ... }  // optional
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
        notion_pairs = payload.get("notion_pairs", {})
        previous_matrix = payload.get("previous_matrix", {})

        # ── PARSE NOTION DAILY RECORDS ──
        daily_pages = notion_daily.get("results", [])
        
        if len(daily_pages) < 20:
            return (json.dumps({
                "error": "Insufficient data",
                "message": f"Need at least 20 days, received {len(daily_pages)}",
                "relational_coherence_score": None
            }), 200, headers)

        def get_number(props, key):
            """Safely extract a number from Notion properties."""
            prop = props.get(key, {})
            if prop.get("type") == "number":
                return prop.get("number")
            return None

        def get_date(props, key):
            """Safely extract a date string from Notion properties."""
            prop = props.get(key, {})
            if prop.get("type") == "date" and prop.get("date"):
                return prop["date"].get("start", "")
            return ""

        def get_text(props, key):
            """Safely extract text from Notion rich_text or title properties."""
            prop = props.get(key, {})
            if prop.get("type") == "title":
                items = prop.get("title", [])
            elif prop.get("type") == "rich_text":
                items = prop.get("rich_text", [])
            else:
                return ""
            if items:
                return items[0].get("plain_text", "")
            return ""

        def get_select(props, key):
            """Safely extract select value from Notion properties."""
            prop = props.get(key, {})
            if prop.get("type") == "select" and prop.get("select"):
                return prop["select"].get("name", "")
            return ""

        # Build daily scores from Notion pages
        fields = ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12"]
        field_keys = {
            "F1": "f1_score", "F2": "f2_score", "F3": "f3_score",
            "F4": "f4_score", "F5": "f5_score", "F6": "f6_score",
            "F7": "f7_score", "F8": "f8_score", "F9": "f9_score",
            "F10": "f10_score", "F11": "f11_score", "F12": "f12_score"
        }

        daily_scores = []
        for page in daily_pages:
            props = page.get("properties", {})
            record = {"date": get_date(props, "date")}
            for f in fields:
                val = get_number(props, field_keys[f])
                record[f] = val
            daily_scores.append(record)

        # Sort by date
        daily_scores.sort(key=lambda d: d.get("date", ""))

        # ── PARSE NOTION EXPECTED PAIRS ──
        pair_pages = notion_pairs.get("results", [])
        expected_pairs = []
        for page in pair_pages:
            props = page.get("properties", {})
            pair = {
                "pair_id": get_text(props, "pair_id"),
                "field_a": get_select(props, "field_a"),
                "field_b": get_select(props, "field_b"),
                "relationship_type": get_select(props, "relationship_type"),
                "expected_r": get_number(props, "expected_r") or 0.0,
                "lag_days": get_number(props, "lag_days") or 0,
                "gate_field": get_select(props, "gate_field") or "None",
                "gate_threshold": get_number(props, "gate_threshold") or 0,
                "diagnostic_weight": get_number(props, "diagnostic_weight") or 0.10,
                "layer_scope": get_select(props, "layer_scope") or "Cross-layer"
            }
            if pair["pair_id"]:
                expected_pairs.append(pair)

        if not expected_pairs:
            return (json.dumps({
                "error": "No expected pairs found",
                "message": "Expected Covariance Table returned 0 records"
            }), 200, headers)

        # ── STEP 1: Build field time series ──
        series = {}
        raw_series = {}
        for f in fields:
            series[f] = [d.get(f) for d in daily_scores]
            raw_series[f] = [d.get(f) for d in daily_scores]

        # ── STEP 2: Z-score normalize ──
        z_series = {}
        for f in fields:
            values = [v for v in series[f] if v is not None]
            if len(values) < 5:
                z_series[f] = [None] * len(series[f])
                continue
            mean = sum(values) / len(values)
            variance = sum((v - mean) ** 2 for v in values) / len(values)
            stdev = math.sqrt(variance) if variance > 0 else 1.0
            z_series[f] = [
                (v - mean) / stdev if v is not None else None
                for v in series[f]
            ]

        # ── STEP 3: Compute pairwise correlations ──
        field_layer = {
            "F1": "Structure", "F2": "Structure", "F3": "Structure",
            "F4": "Electricity", "F5": "Electricity", "F6": "Electricity",
            "F7": "Energy", "F8": "Energy", "F9": "Energy",
            "F10": "Regulation", "F11": "Regulation", "F12": "Regulation"
        }

        def pearson_r(xs, ys):
            pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
            n = len(pairs)
            if n < 15:
                return None, n
            mean_x = sum(p[0] for p in pairs) / n
            mean_y = sum(p[1] for p in pairs) / n
            num = sum((p[0] - mean_x) * (p[1] - mean_y) for p in pairs)
            den_x = math.sqrt(sum((p[0] - mean_x) ** 2 for p in pairs))
            den_y = math.sqrt(sum((p[1] - mean_y) ** 2 for p in pairs))
            if den_x == 0 or den_y == 0:
                return 0.0, n
            return num / (den_x * den_y), n

        def apply_lag(sa, sb, lag):
            if lag <= 0 or lag >= len(sa):
                return sa, sb
            return sa[:len(sa) - lag], sb[lag:]

        def apply_gate(sa, sb, gate_raw, threshold):
            fa, fb = [], []
            for i in range(min(len(sa), len(sb), len(gate_raw))):
                if gate_raw[i] is not None and gate_raw[i] >= threshold:
                    fa.append(sa[i])
                    fb.append(sb[i])
                else:
                    fa.append(None)
                    fb.append(None)
            return fa, fb

        matrix = {}
        all_weighted_deviations = []
        layer_deviations = {
            "Structure": [], "Electricity": [], "Energy": [],
            "Regulation": [], "Cross-layer": []
        }

        for pair in expected_pairs:
            pid = pair["pair_id"]
            fa = pair["field_a"]
            fb = pair["field_b"]
            rel = pair["relationship_type"]
            exp_r = float(pair["expected_r"])
            lag = int(pair["lag_days"])
            gf = pair["gate_field"]
            gt = float(pair["gate_threshold"])
            dw = float(pair["diagnostic_weight"])

            sa = list(z_series.get(fa, []))
            sb = list(z_series.get(fb, []))

            if not sa or not sb:
                matrix[pid] = {
                    "pair_id": pid, "expected_r": exp_r, "actual_r": None,
                    "deviation": None, "weighted_deviation": None,
                    "deviation_direction": "unknown", "data_points": 0,
                    "sufficient_data": False, "relationship_type": rel
                }
                continue

            if lag > 0:
                sa, sb = apply_lag(sa, sb, lag)

            if gf != "None" and gf != "" and gf in raw_series:
                gr = list(raw_series[gf])
                if lag > 0:
                    gr = gr[:len(gr) - lag]
                sa, sb = apply_gate(sa, sb, gr, gt)

            r, n = pearson_r(sa, sb)
            sufficient = r is not None and n >= 20

            if r is not None:
                deviation = abs(exp_r - r)
                w_dev = deviation * abs(exp_r) if exp_r != 0 else deviation * dw

                prev = previous_matrix.get(pid, {})
                prev_r = prev.get("actual_r")
                if prev_r is not None:
                    prev_dev = abs(exp_r - prev_r)
                    if deviation < prev_dev - 0.02:
                        direction = "narrowing"
                    elif deviation > prev_dev + 0.02:
                        direction = "widening"
                    else:
                        direction = "stable"
                else:
                    direction = "new"

                all_weighted_deviations.append(w_dev)
                la = field_layer.get(fa, "")
                lb = field_layer.get(fb, "")
                bucket = la if la == lb else "Cross-layer"
                layer_deviations[bucket].append(deviation)
            else:
                deviation = None
                w_dev = None
                direction = "insufficient_data"

            matrix[pid] = {
                "pair_id": pid, "expected_r": exp_r,
                "actual_r": round(r, 4) if r is not None else None,
                "deviation": round(deviation, 4) if deviation is not None else None,
                "weighted_deviation": round(w_dev, 4) if w_dev is not None else None,
                "deviation_direction": direction, "data_points": n,
                "sufficient_data": sufficient, "relationship_type": rel
            }

        # ── STEP 4: RC Score ──
        if all_weighted_deviations:
            mean_wd = sum(all_weighted_deviations) / len(all_weighted_deviations)
            rc = max(0, min(100, round(100 - (mean_wd * 100 / 1.80), 1)))
        else:
            rc = None

        # ── STEP 5: Top deviations ──
        ranked = sorted(
            [v for v in matrix.values() if v["deviation"] is not None and v["sufficient_data"]],
            key=lambda x: x["deviation"], reverse=True
        )
        top_devs = [{
            "pair_id": p["pair_id"], "deviation": p["deviation"],
            "actual_r": p["actual_r"], "expected_r": p["expected_r"],
            "relationship_type": p["relationship_type"],
            "deviation_direction": p["deviation_direction"]
        } for p in ranked[:5]]

        # ── STEP 6: Layer summaries ──
        layer_summaries = {}
        for layer, devs in layer_deviations.items():
            layer_summaries[layer] = {
                "mean_deviation": round(sum(devs) / len(devs), 4) if devs else None,
                "pairs_computed": len(devs)
            }

        computed = sum(1 for v in matrix.values() if v["sufficient_data"])
        insufficient = sum(1 for v in matrix.values() if not v["sufficient_data"])

        result = {
            "relational_coherence_score": rc,
            "pairs_computed": computed,
            "pairs_insufficient": insufficient,
            "days_analyzed": len(daily_scores),
            "top_deviations": top_devs,
            "layer_summaries": layer_summaries,
            "matrix": matrix
        }

        summary = {
            "relational_coherence_score": rc,
            "pairs_computed": computed,
            "pairs_insufficient": insufficient,
            "days_analyzed": len(daily_scores),
            "top_deviations": top_devs[:3],
            "layer_summaries": layer_summaries
        }
        result["matrix_json_string"] = json.dumps(summary, separators=(',',':')).replace('"', '\\"')

        return (json.dumps(result), 200, headers)

    except Exception as e:
        import traceback
        return (json.dumps({
            "error": str(e),
            "trace": traceback.format_exc()
        }), 500, headers)
