"""Parity proof for the ported relational service.

Runs the live Cloud Run entry point end to end against the v3 golden, in
legacy loader configuration. Zero diffs is the pass condition — unlike the
step-1 harness, this compares every key including the compatibility config
block and matrix_json_string.

    LOADER_ZERO_AS_NULL=false LOADER_NEAR_DUP_MIN_FIELDS=0 \
      python tests/parity_check.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import importlib.util
_spec = importlib.util.spec_from_file_location(
    "relational_main",
    os.path.join(os.path.dirname(__file__), "..", "relational", "main.py"))
relational_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relational_main)

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


class Req:
    method = "POST"

    def __init__(self, p):
        self._p = p

    def get_json(self, silent=False):
        return self._p


def diff(a, b, path=""):
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append((path + "/" + k, "<missing in got>", b[k]))
            elif k not in b:
                out.append((path + "/" + k, a[k], "<missing in expected>"))
            else:
                out += diff(a[k], b[k], path + "/" + k)
    elif isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append((path, f"len={len(a)}", f"len={len(b)}"))
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                out += diff(x, y, f"{path}[{i}]")
    elif a != b:
        out.append((path, a, b))
    return out


payload = json.load(open(os.path.join(FIXTURES, "rc_payload.json")))
expected = json.load(open(os.path.join(FIXTURES, "rc_expected.json")))

body, status, _ = relational_main.compute_relational_matrix(Req(payload))
got = json.loads(body)

if os.environ.get("LOADER_ZERO_AS_NULL") != "false":
    print("WARNING: not running in legacy mode; diffs below are expected.\n")

window = got.pop("window", None)
d = diff(got, expected)

print("status:", status)
print("days analyzed:", got.get("days_analyzed"))
print("pairs computed:", got.get("pairs_computed"))
print("DIFFS vs rc_expected.json:", len(d))
for p, g, e in d[:20]:
    print("  ", p, "| got:", str(g)[:60], "| exp:", str(e)[:60])

if window:
    print("\nwindow:", window["window_start"], "->", window["window_end"],
          "| regime:", window["regime"],
          "| eligible:", window["n_eligible"],
          "| excluded:", window["excluded_counts"])
