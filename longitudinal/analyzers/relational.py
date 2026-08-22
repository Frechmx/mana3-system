"""
relational.py — the relational matrix, as a pure function.

Ported from relational/main.py v3 with the maths untouched. Everything the
old module did before it started correlating — Notion parsing, no_sync
dropping, duplicate detection, the calendar window cut, the minimum-days
gate — is gone from here. It belongs to loader.py, and this analyzer now
receives its decisions rather than repeating them.

Contract:
  * PURE. Takes a Window and a list of ExpectedPair. No Notion, no HTTP,
    no environment reads outside RelationalConfig.
  * Never re-filters. If a day is in window.days it is an observation;
    that judgement was already made and is not this module's to revisit.
  * Refuses rather than guesses. Below the loader's relational gate it
    returns window.insufficiency("relational") and no score.

Pair-level thresholds (how many paired points a correlation needs) stay
here, because they are a property of the correlation, not of the day.
Day-level thresholds live in LoaderConfig. If you find yourself adding a
day-level rule to this file, it is in the wrong file.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, List, Optional, Tuple

from longitudinal.loader import FIELD_LAYER, FIELDS, ExpectedPair, Window

RC_VERSION = 3

# Deviation is |expected_r - actual_r|, which ranges 0..2. RC maps that
# range onto 0..100. The denominator MUST match the scale of the numerator:
# v2 divided a weighted mean by a constant on the unweighted scale, which
# shrank every RC by roughly the mean weight and confined the metric to
# ~88-100. Do not change one without the other.
RC_DEVIATION_MAX = 2.0


class RelationalConfig:
    """Read at construction, not at import, so tests can monkeypatch env."""

    def __init__(self) -> None:
        # Minimum paired observations before a correlation is computed at
        # all. Below this the pair reports actual_r = None.
        self.min_pair_points = int(os.environ.get("RC_MIN_PAIR_POINTS", "15"))
        # Minimum paired observations before a computed correlation is
        # marked trustworthy enough to rank or narrate.
        self.sufficient_points = int(os.environ.get("RC_SUFFICIENT_POINTS", "20"))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "min_pair_points": self.min_pair_points,
            "sufficient_points": self.sufficient_points,
        }


# ──────────────────────────────────────────────────────────────────────
# MATHS — lifted from v3 unchanged
# ──────────────────────────────────────────────────────────────────────
def _pearson_r(
    xs: List[Optional[float]],
    ys: List[Optional[float]],
    min_points: int,
) -> Tuple[Optional[float], int]:
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < min_points:
        return None, n
    mean_x = sum(p[0] for p in pairs) / n
    mean_y = sum(p[1] for p in pairs) / n
    num = sum((p[0] - mean_x) * (p[1] - mean_y) for p in pairs)
    den_x = math.sqrt(sum((p[0] - mean_x) ** 2 for p in pairs))
    den_y = math.sqrt(sum((p[1] - mean_y) ** 2 for p in pairs))
    if den_x == 0 or den_y == 0:
        return 0.0, n
    return num / (den_x * den_y), n


def _apply_lag(sa: List, sb: List, lag: int) -> Tuple[List, List]:
    if lag <= 0 or lag >= len(sa):
        return sa, sb
    return sa[:len(sa) - lag], sb[lag:]


def _apply_gate(sa: List, sb: List, gate_raw: List, threshold: float):
    fa, fb = [], []
    for i in range(min(len(sa), len(sb), len(gate_raw))):
        if gate_raw[i] is not None and gate_raw[i] >= threshold:
            fa.append(sa[i])
            fb.append(sb[i])
        else:
            fa.append(None)
            fb.append(None)
    return fa, fb


def _z_normalize(values: List[Optional[float]]) -> List[Optional[float]]:
    """Pearson r is invariant under linear rescaling, so this does not move
    any correlation. Retained because raw series are used for gate
    thresholds and keeping the two parallel avoids confusion."""
    present = [v for v in values if v is not None]
    if len(present) < 5:
        return [None] * len(values)
    mean = sum(present) / len(present)
    variance = sum((v - mean) ** 2 for v in present) / len(present)
    stdev = math.sqrt(variance) if variance > 0 else 1.0
    return [(v - mean) / stdev if v is not None else None for v in values]


# ──────────────────────────────────────────────────────────────────────
# ANALYZER
# ──────────────────────────────────────────────────────────────────────
def compute(
    window: Window,
    pairs: List[ExpectedPair],
    previous_matrix: Optional[Dict[str, Any]] = None,
    config: Optional[RelationalConfig] = None,
) -> Dict[str, Any]:
    """Window + expected pairs -> relational matrix.

    previous_matrix is the legacy shape: {pair_id: {"actual_r": float}}.
    It drives deviation_direction only. Note that comparing two heavily
    overlapping windows produces "stable" for reasons that have nothing to
    do with physiology; refusing that comparison belongs to the snapshot
    resolver, not here, and until it exists this field should be read with
    that caveat.
    """
    cfg = config or RelationalConfig()
    previous_matrix = previous_matrix or {}

    if not window.is_sufficient("relational"):
        out = window.insufficiency("relational")
        out["relational_coherence_score"] = None
        out["rc_version"] = RC_VERSION
        out["validation_status"] = "insufficient_data"
        out["window"] = window.to_dict()
        return out

    if not pairs:
        return {
            "error": "No expected pairs found",
            "message": "Expected Covariance Table returned 0 records",
            "relational_coherence_score": None,
            "rc_version": RC_VERSION,
            "validation_status": "no_pair_table",
            "window": window.to_dict(),
        }

    raw_series = window.all_series()
    z_series = {f: _z_normalize(raw_series[f]) for f in FIELDS}

    matrix: Dict[str, Any] = {}
    weighted_dev_sum = 0.0
    weight_sum = 0.0
    weights_missing = 0
    layer_deviations: Dict[str, List[float]] = {
        "Structure": [], "Electricity": [], "Energy": [],
        "Regulation": [], "Cross-layer": [],
    }

    for pair in pairs:
        if pair.weight_was_missing:
            weights_missing += 1

        sa = list(z_series.get(pair.field_a, []))
        sb = list(z_series.get(pair.field_b, []))

        if not sa or not sb:
            matrix[pair.pair_id] = {
                "pair_id": pair.pair_id, "expected_r": pair.expected_r,
                "actual_r": None, "deviation": None,
                "weighted_deviation": None, "deviation_direction": "unknown",
                "data_points": 0, "sufficient_data": False,
                "relationship_type": pair.relationship_type,
            }
            continue

        if pair.lag_days > 0:
            sa, sb = _apply_lag(sa, sb, pair.lag_days)

        if pair.gate_field not in ("None", "") and pair.gate_field in raw_series:
            gr = list(raw_series[pair.gate_field])
            if pair.lag_days > 0:
                gr = gr[:len(gr) - pair.lag_days]
            sa, sb = _apply_gate(sa, sb, gr, pair.gate_threshold)

        r, n = _pearson_r(sa, sb, cfg.min_pair_points)
        sufficient = r is not None and n >= cfg.sufficient_points

        if r is not None:
            deviation = abs(pair.expected_r - r)

            # Single weighting scheme for every pair. v2 used
            # `deviation * abs(expected_r)` for correlated pairs and
            # `deviation * diagnostic_weight` for Independent ones, which
            # weighted a violated Independent pair — arguably the most
            # diagnostically interesting event in the matrix — about 7x
            # lower than a Synergistic one, so it vanished from RC.
            w_dev = deviation * pair.diagnostic_weight
            weighted_dev_sum += w_dev
            weight_sum += pair.diagnostic_weight

            prev_r = (previous_matrix.get(pair.pair_id) or {}).get("actual_r")
            if prev_r is not None:
                prev_dev = abs(pair.expected_r - prev_r)
                if deviation < prev_dev - 0.02:
                    direction = "narrowing"
                elif deviation > prev_dev + 0.02:
                    direction = "widening"
                else:
                    direction = "stable"
            else:
                direction = "new"

            la = FIELD_LAYER.get(pair.field_a, "")
            lb = FIELD_LAYER.get(pair.field_b, "")
            layer_deviations[la if la == lb else "Cross-layer"].append(deviation)
        else:
            deviation = None
            w_dev = None
            direction = "insufficient_data"

        matrix[pair.pair_id] = {
            "pair_id": pair.pair_id, "expected_r": pair.expected_r,
            "actual_r": round(r, 4) if r is not None else None,
            "deviation": round(deviation, 4) if deviation is not None else None,
            "weighted_deviation": round(w_dev, 4) if w_dev is not None else None,
            "deviation_direction": direction, "data_points": n,
            "sufficient_data": sufficient,
            "relationship_type": pair.relationship_type,
        }

    if weight_sum > 0:
        weighted_mean_dev = weighted_dev_sum / weight_sum
        rc = max(0, min(100, round(
            100 - (weighted_mean_dev * 100 / RC_DEVIATION_MAX), 1
        )))
    else:
        weighted_mean_dev = None
        rc = None

    ranked = sorted(
        [v for v in matrix.values()
         if v["deviation"] is not None and v["sufficient_data"]],
        key=lambda x: x["deviation"], reverse=True,
    )
    top_devs = [{
        "pair_id": p["pair_id"], "deviation": p["deviation"],
        "actual_r": p["actual_r"], "expected_r": p["expected_r"],
        "relationship_type": p["relationship_type"],
        "deviation_direction": p["deviation_direction"],
    } for p in ranked[:5]]

    layer_summaries = {
        layer: {
            "mean_deviation": round(sum(d) / len(d), 4) if d else None,
            "pairs_computed": len(d),
        }
        for layer, d in layer_deviations.items()
    }

    computed = sum(1 for v in matrix.values() if v["sufficient_data"])
    insufficient = sum(1 for v in matrix.values() if not v["sufficient_data"])
    validation_status = window.sufficiency["relational"]["validation_status"]

    result = {
        "relational_coherence_score": rc,
        "rc_version": RC_VERSION,
        "validation_status": validation_status,
        "weighted_mean_deviation": (
            round(weighted_mean_dev, 4) if weighted_mean_dev is not None else None
        ),
        "pairs_computed": computed,
        "pairs_insufficient": insufficient,
        "days_analyzed": window.n_eligible,
        "config": _legacy_config_block(window, cfg, weights_missing,
                                       weight_sum, computed),
        "window": window.to_dict(),
        "top_deviations": top_devs,
        "layer_summaries": layer_summaries,
        "matrix": matrix,
    }

    result["matrix_json_string"] = _matrix_json_string(
        result, window, top_devs, layer_summaries
    )
    return result


def _legacy_config_block(
    window: Window,
    cfg: RelationalConfig,
    weights_missing: int,
    weight_sum: float,
    computed: int,
) -> Dict[str, Any]:
    """The v3 config shape, rebuilt from the Window.

    Kept byte-compatible so tests/parity_check.py can diff a full response
    against rc_expected.json, and so S7's existing parsers keep working
    until they are replaced. The honest, complete account of what was
    dropped and why now lives in result["window"]; this block is a
    compatibility surface and should be deleted once nothing reads it.
    """
    ex = window.excluded_counts()
    return {
        "rc_version": RC_VERSION,
        "window_days": window.config.get("window_days"),
        "window_start": window.window_start,
        "window_end": window.window_end,
        "span_days": window.span_days,
        "coverage_pct": window.coverage_pct,
        "min_days": window.config.get("relational_min_days"),
        "min_pair_points": cfg.min_pair_points,
        "rows_received": window.rows_received,
        "rows_used": window.n_eligible,
        "rows_dropped": len(window.excluded),
        "dropped_no_sync": ex["no_sync"],
        "dropped_duplicate": ex["exact_duplicate"] + ex["near_duplicate"],
        "dropped_empty": ex["no_field_data"],
        "dropped_out_of_window": ex["out_of_window"],
        "weights_missing": weights_missing,
        "mean_weight": round(weight_sum / computed, 4) if computed else None,
    }


def _matrix_json_string(
    result: Dict[str, Any],
    window: Window,
    top_devs: List[Dict[str, Any]],
    layer_summaries: Dict[str, Any],
) -> str:
    """Compact summary for Notion rich_text, which caps at 2000 chars."""
    summary = {
        "relational_coherence_score": result["relational_coherence_score"],
        "rc_version": RC_VERSION,
        "validation_status": result["validation_status"],
        "pairs_computed": result["pairs_computed"],
        "pairs_insufficient": result["pairs_insufficient"],
        "days_analyzed": window.n_eligible,
        "rows_dropped": len(window.excluded),
        "window_days": window.config.get("window_days"),
        "window_start": window.window_start,
        "window_end": window.window_end,
        "span_days": window.span_days,
        "top_deviations": top_devs[:3],
        "layer_summaries": layer_summaries,
    }
    return json.dumps(summary, separators=(",", ":")).replace('"', '\\"')
