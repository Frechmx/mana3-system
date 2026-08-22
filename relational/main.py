"""
S9 — Relational Matrix Computation (Cloud Run entry point)

This file is transport only: HTTP in, HTTP out. It parses the request,
hands the payload to loader.py, hands the Window to the analyzer, and
serialises the result.

There is deliberately no logic here. Everything that decides what counts
as an observation lives in longitudinal/loader.py; everything that decides
what the correlations mean lives in longitudinal/analyzers/relational.py.
Both are pure and both are tested from fixtures. If a bug can only be
reproduced by sending an HTTP request, it belongs in one of those two
files instead.

The request and response contracts are unchanged from v3, so the existing
Make scenario keeps working without modification. The response gains one
new key, "window", which is the honest account of what was dropped and
why; the legacy "config" block is retained for backward compatibility and
should be removed once nothing reads it.

Configuration
-------------
Day-level thresholds are LOADER_* and are read by loader.py:
    LOADER_WINDOW_DAYS, LOADER_CLEAN_DATA_FLOOR, LOADER_DROP_NO_SYNC,
    LOADER_DROP_DUPLICATES, LOADER_NEAR_DUP_MIN_FIELDS,
    LOADER_ZERO_AS_NULL, LOADER_RELATIONAL_MIN_DAYS,
    LOADER_RELATIONAL_VALID_MIN

Pair-level thresholds stay here:
    RC_MIN_PAIR_POINTS, RC_SUFFICIENT_POINTS

The old RC_WINDOW_DAYS / RC_MIN_DAYS / RC_VALID_MIN_ROWS / RC_DROP_NO_SYNC
/ RC_DROP_DUPLICATES variables are SUPERSEDED and are no longer read. If
any are still set on the Cloud Run service they will be silently ignored,
so remove them rather than leaving them to mislead the next reader.
"""

import json
import traceback

import functions_framework

from longitudinal.analyzers import relational
from longitudinal.loader import LoaderConfig, load_from_notion_payload

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "3600",
}


@functions_framework.http
def compute_relational_matrix(request):
    """
    Expected JSON body:
    {
        "notion_daily": { "results": [ ...Notion page objects... ] },
        "notion_pairs": { "results": [ ...Notion page objects... ] },
        "previous_matrix": { "F1_F2": { "actual_r": 0.65 }, ... }   // optional
    }
    """
    if request.method == "OPTIONS":
        return ("", 204, CORS_HEADERS)

    headers = {
        "Access-Control-Allow-Origin": "*",
        "Content-Type": "application/json",
    }

    # GET returns the live configuration. Being able to ask a running
    # service what thresholds it is actually using — rather than inferring
    # them from a deploy log — is worth the four lines.
    if request.method == "GET":
        cfg = LoaderConfig()
        return (json.dumps({
            "service": "relational-matrix",
            "rc_version": relational.RC_VERSION,
            "loader_config": cfg.as_dict(),
            "relational_config": relational.RelationalConfig().as_dict(),
        }), 200, headers)

    try:
        payload = request.get_json(silent=True) or {}
        window, pairs = load_from_notion_payload(payload, LoaderConfig())
        result = relational.compute(
            window, pairs, payload.get("previous_matrix")
        )
        return (json.dumps(result), 200, headers)

    except Exception as e:
        return (json.dumps({
            "error": str(e),
            "trace": traceback.format_exc(),
        }), 500, headers)
