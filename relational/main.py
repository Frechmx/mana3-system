import functions_framework
import json
import math
import os
from datetime import date, timedelta

# ──────────────────────────────────────────────────────────────────────
# CONFIG — all thresholds are env-overridable so test-phase values never
# become permanent by accident. Defaults are the production values.
# ──────────────────────────────────────────────────────────────────────
# RC_WINDOW_DAYS is CALENDAR days, not row count. The window is the N days
# ending on the most recent record; whatever valid rows fall inside it are
# what gets correlated. A row-count window would silently stretch the span
# backwards in proportion to the drop rate — at ~45% drops, a 60-row window
# reaches back ~108 calendar days and blends unrelated physiological regimes.
RC_WINDOW_DAYS       = int(os.environ.get("RC_WINDOW_DAYS", "60"))
RC_MIN_DAYS          = int(os.environ.get("RC_MIN_DAYS", "20"))
RC_MIN_PAIR_POINTS   = int(os.environ.get("RC_MIN_PAIR_POINTS", "15"))
RC_SUFFICIENT_POINTS = int(os.environ.get("RC_SUFFICIENT_POINTS", "20"))
# Below this many valid rows, correlations are too noisy to narrate —
# the run still computes, but is labelled test_mode so S10/S7 can refuse it.
RC_VALID_MIN_ROWS    = int(os.environ.get("RC_VALID_MIN_ROWS", "21"))
RC_DROP_NO_SYNC      = os.environ.get("RC_DROP_NO_SYNC", "true").lower() == "true"
RC_DROP_DUPLICATES   = os.environ.get("RC_DROP_DUPLICATES", "true").lower() == "true"
RC_VERSION           = 2

# Deviation is |expected_r - actual_r|, which ranges 0..2.
# RC maps that range onto 0..100. Denominator MUST match the scale of the
# numerator — see RC scaling note below.
RC_DEVIATION_MAX = 2.0


@functions_framework.http
def compute_relational_matrix(request):
    """
    S9 — Relational Matrix Computation
    Accepts Notion API raw response format directly from Make.com.

    Expected JSON body:
    {
        "notion_daily": { "results": [ ...Notion page objects... ] },
        "notion_pairs": { "results": [ ...Notion page objects... ] },
        "previous_matrix": { "F1_F2": { "actual_r": 0.65 }, ... }  // optional
    }

    v2 changes:
      - Drops no_sync days and carry-forward duplicate days before correlating.
      - RC numerator and denominator now share a scale (was a unit mismatch
        that compressed all RC values into ~88-100).
      - Single consistent weighting scheme (was two incompatible ones).
      - All thresholds env-configurable; config echoed in the response.
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
        previous_matrix = payload.get("previous_matrix", {}) or {}

        # ── PARSE NOTION DAILY RECORDS ──
        daily_pages = notion_daily.get("results", [])

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

        def get_checkbox(props, key):
            """Safely extract checkbox value from Notion properties."""
            prop = props.get(key, {})
            if prop.get("type") == "checkbox":
                return bool(prop.get("checkbox"))
            return False

        # Build daily scores from Notion pages
        fields = ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12"]
        field_keys = {
            "F1": "f1_score", "F2": "f2_score", "F3": "f3_score",
            "F4": "f4_score", "F5": "f5_score", "F6": "f6_score",
            "F7": "f7_score", "F8": "f8_score", "F9": "f9_score",
            "F10": "f10_score", "F11": "f11_score", "F12": "f12_score"
        }

        parsed_rows = []
        for page in daily_pages:
            props = page.get("properties", {})
            record = {
                "date": get_date(props, "date"),
                "data_state": get_select(props, "data_state"),
                "wearable_absent": get_checkbox(props, "wearable_data_absent"),
            }
            for f in fields:
                record[f] = get_number(props, field_keys[f])
            parsed_rows.append(record)

        # Sort by date ascending
        parsed_rows.sort(key=lambda d: d.get("date", ""))

        # ──────────────────────────────────────────────────────────────
        # DATA QUALITY FILTER
        #
        # Two distinct contaminants, both of which manufacture fake
        # correlation structure:
        #
        #  1. no_sync days — the ingest pipeline carries the previous
        #     day's scores forward when the wearable didn't sync. These
        #     are not observations.
        #  2. Carry-forward duplicates — rows byte-identical to their
        #     predecessor across all 12 fields. Catches the same problem
        #     on historical rows where data_state was never populated
        #     (data_state only exists from 2026-08-01 onward), so the
        #     no_sync filter alone is blind to them.
        #
        # Duplicate detection runs on the ORIGINAL sequence, before any
        # rows are removed, so that dropping row N doesn't make rows
        # N-1 and N+1 look like neighbours and mask a real repeat.
        # ──────────────────────────────────────────────────────────────
        def field_vector(row):
            return tuple(row.get(f) for f in fields)

        dropped_no_sync = 0
        dropped_duplicate = 0
        dropped_empty = 0
        daily_scores = []

        for i, row in enumerate(parsed_rows):
            vec = field_vector(row)

            # Rows with no field data at all carry no signal.
            if all(v is None for v in vec):
                dropped_empty += 1
                continue

            if RC_DROP_NO_SYNC and (
                row.get("data_state") == "no_sync" or row.get("wearable_absent")
            ):
                dropped_no_sync += 1
                continue

            if RC_DROP_DUPLICATES and i > 0:
                if vec == field_vector(parsed_rows[i - 1]):
                    dropped_duplicate += 1
                    continue

            daily_scores.append(row)

        rows_received = len(parsed_rows)
        rows_dropped = dropped_no_sync + dropped_duplicate + dropped_empty

        # ──────────────────────────────────────────────────────────────
        # CALENDAR WINDOW
        #
        # The window is the RC_WINDOW_DAYS calendar days ending on the most
        # recent surviving record. Rows outside it are cut regardless of how
        # many remain, so "60-day window" always means 60 days of elapsed
        # time — never "however far back we had to reach to find 60 rows".
        # ──────────────────────────────────────────────────────────────
        window_start = window_end = None
        dropped_out_of_window = 0

        if daily_scores and RC_WINDOW_DAYS > 0:
            try:
                end_d = date.fromisoformat(daily_scores[-1]["date"][:10])
                start_d = end_d - timedelta(days=RC_WINDOW_DAYS - 1)
                before = len(daily_scores)
                daily_scores = [
                    r for r in daily_scores
                    if r.get("date")
                    and date.fromisoformat(r["date"][:10]) >= start_d
                ]
                dropped_out_of_window = before - len(daily_scores)
                window_start, window_end = start_d.isoformat(), end_d.isoformat()
            except (ValueError, TypeError):
                # Unparseable dates: fall through uncut rather than silently
                # correlating an arbitrary slice.
                window_start = window_end = None

        # Actual elapsed span of the surviving rows, which can be shorter
        # than the nominal window if the client started mid-window.
        span_days = None
        if daily_scores:
            try:
                first = date.fromisoformat(daily_scores[0]["date"][:10])
                last = date.fromisoformat(daily_scores[-1]["date"][:10])
                span_days = (last - first).days + 1
            except (ValueError, TypeError):
                span_days = None

        config_block = {
            "rc_version": RC_VERSION,
            "window_days": RC_WINDOW_DAYS,
            "window_start": window_start,
            "window_end": window_end,
            "span_days": span_days,
            "coverage_pct": (
                round(100 * len(daily_scores) / span_days, 1)
                if span_days else None
            ),
            "min_days": RC_MIN_DAYS,
            "min_pair_points": RC_MIN_PAIR_POINTS,
            "rows_received": rows_received,
            "rows_used": len(daily_scores),
            "rows_dropped": rows_dropped,
            "dropped_no_sync": dropped_no_sync,
            "dropped_duplicate": dropped_duplicate,
            "dropped_empty": dropped_empty,
            "dropped_out_of_window": dropped_out_of_window,
        }

        # Floor is checked AFTER filtering — the old code checked the raw
        # page count, so a window of 60 rows that was 53% duplicates passed
        # a "20 day" gate while carrying ~28 real observations.
        if len(daily_scores) < RC_MIN_DAYS:
            return (json.dumps({
                "error": "Insufficient data",
                "message": (
                    f"Need at least {RC_MIN_DAYS} valid days, "
                    f"have {len(daily_scores)} after dropping {rows_dropped} "
                    f"of {rows_received} rows"
                ),
                "relational_coherence_score": None,
                "validation_status": "insufficient_data",
                "config": config_block
            }), 200, headers)

        # ── PARSE NOTION EXPECTED PAIRS ──
        pair_pages = notion_pairs.get("results", [])
        expected_pairs = []
        for page in pair_pages:
            props = page.get("properties", {})
            dw_raw = get_number(props, "diagnostic_weight")
            pair = {
                "pair_id": get_text(props, "pair_id"),
                "field_a": get_select(props, "field_a"),
                "field_b": get_select(props, "field_b"),
                "relationship_type": get_select(props, "relationship_type"),
                "expected_r": get_number(props, "expected_r") or 0.0,
                "lag_days": get_number(props, "lag_days") or 0,
                "gate_field": get_select(props, "gate_field") or "None",
                "gate_threshold": get_number(props, "gate_threshold") or 0,
                # Default is 1.0, not 0.10. A missing weight must not silently
                # mute a pair to a tenth of its neighbours.
                "diagnostic_weight": dw_raw if dw_raw is not None else 1.0,
                "weight_was_missing": dw_raw is None,
                "layer_scope": get_select(props, "layer_scope") or "Cross-layer"
            }
            if pair["pair_id"]:
                expected_pairs.append(pair)

        if not expected_pairs:
            return (json.dumps({
                "error": "No expected pairs found",
                "message": "Expected Covariance Table returned 0 records",
                "config": config_block
            }), 200, headers)

        # ── STEP 1: Build field time series ──
        series = {}
        raw_series = {}
        for f in fields:
            series[f] = [d.get(f) for d in daily_scores]
            raw_series[f] = [d.get(f) for d in daily_scores]

        # ── STEP 2: Z-score normalize ──
        # Note: Pearson r is invariant under linear rescaling, so this has no
        # effect on the correlations. Retained because raw_series is used for
        # gate thresholds and keeping the two parallel avoids confusion.
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
            if n < RC_MIN_PAIR_POINTS:
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
        weighted_dev_sum = 0.0
        weight_sum = 0.0
        weights_missing = 0
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
            if pair["weight_was_missing"]:
                weights_missing += 1

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
            sufficient = r is not None and n >= RC_SUFFICIENT_POINTS

            if r is not None:
                deviation = abs(exp_r - r)

                # ──────────────────────────────────────────────────────
                # WEIGHTING — single scheme for every pair.
                #
                # Was: `deviation * abs(exp_r)` for pairs with a non-zero
                # expected_r, and `deviation * dw` (default 0.10) for
                # Independent pairs. Two incompatible schemes: a violated
                # Independent pair — arguably the most diagnostically
                # interesting event in the matrix — was weighted ~7x lower
                # than a Synergistic one and vanished from RC.
                #
                # Now: diagnostic_weight governs all pairs, which is what
                # the field is named for.
                # ──────────────────────────────────────────────────────
                w_dev = deviation * dw
                weighted_dev_sum += w_dev
                weight_sum += dw

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

        # ──────────────────────────────────────────────────────────────
        # STEP 4: RC Score
        #
        # Was: rc = 100 - (mean_wd * 100 / 1.80), where mean_wd was a
        # WEIGHTED deviation averaged over the pair COUNT, divided by a
        # constant on the UNWEIGHTED 0..2 scale. Numerator and denominator
        # were in different units, shrinking every RC by roughly the mean
        # weight (~0.36 in practice) and confining the metric to ~88-100.
        #
        # Now: divide the weighted deviation sum by the weight sum, giving
        # a true weighted-mean deviation on the same 0..2 scale as the
        # denominator. RC becomes interpretable across its full range.
        # ──────────────────────────────────────────────────────────────
        if weight_sum > 0:
            weighted_mean_dev = weighted_dev_sum / weight_sum
            rc = max(0, min(100, round(
                100 - (weighted_mean_dev * 100 / RC_DEVIATION_MAX), 1
            )))
        else:
            weighted_mean_dev = None
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

        # Validity keys off the number of real observations, not the nominal
        # window length — a 60-day window containing 12 usable days is just
        # as noisy as a 12-day one. Short runs still compute, but are
        # labelled so S10 and S7 can refuse to narrate them.
        validation_status = (
            "valid" if len(daily_scores) >= RC_VALID_MIN_ROWS else "test_mode"
        )

        config_block["weights_missing"] = weights_missing
        config_block["mean_weight"] = (
            round(weight_sum / computed, 4) if computed else None
        )

        result = {
            "relational_coherence_score": rc,
            "rc_version": RC_VERSION,
            "validation_status": validation_status,
            "weighted_mean_deviation": (
                round(weighted_mean_dev, 4) if weighted_mean_dev is not None else None
            ),
            "pairs_computed": computed,
            "pairs_insufficient": insufficient,
            "days_analyzed": len(daily_scores),
            "config": config_block,
            "top_deviations": top_devs,
            "layer_summaries": layer_summaries,
            "matrix": matrix
        }

        # Kept compact — Notion rich_text caps at 2000 chars.
        summary = {
            "relational_coherence_score": rc,
            "rc_version": RC_VERSION,
            "validation_status": validation_status,
            "pairs_computed": computed,
            "pairs_insufficient": insufficient,
            "days_analyzed": len(daily_scores),
            "rows_dropped": rows_dropped,
            "window_days": RC_WINDOW_DAYS,
            "window_start": window_start,
            "window_end": window_end,
            "span_days": span_days,
            "top_deviations": top_devs[:3],
            "layer_summaries": layer_summaries
        }
        result["matrix_json_string"] = json.dumps(
            summary, separators=(',', ':')
        ).replace('"', '\\"')

        return (json.dumps(result), 200, headers)

    except Exception as e:
        import traceback
        return (json.dumps({
            "error": str(e),
            "trace": traceback.format_exc()
        }), 500, headers)
