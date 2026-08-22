"""Parity proof: loader's day selection is identical to relational v3's.

Rebuilds a Notion payload from the Window the loader produced and feeds it
to the unmodified v3 function. v3's own filters are no-ops on already-clean
rows, so the only thing under test is whether the loader chose the same
days with the same values. Byte-identical output against rc_expected.json
means the port changed nothing.
"""
import json
import os
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "stub")

from longitudinal.loader import FIELD_PROPERTY, FIELDS, load_from_notion_payload
import importlib.util
_spec = importlib.util.spec_from_file_location(
    "rc_v3", os.path.join(os.path.dirname(__file__), "..", "relational", "main.py"))
rc_v3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc_v3)


class Req:
    method = "POST"

    def __init__(self, p):
        self._p = p

    def get_json(self):
        return self._p


def day_to_page(day):
    props = {
        "date": {"type": "date", "date": {"start": day.date}},
        "data_state": {
            "type": "select",
            "select": {"name": day.data_state} if day.data_state else None,
        },
        "wearable_data_absent": {
            "type": "checkbox", "checkbox": day.wearable_absent
        },
    }
    for f in FIELDS:
        props[FIELD_PROPERTY[f]] = {"type": "number", "number": day.get(f)}
    return {"properties": props}


def diff(a, b, path=""):
    out = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append((path + "/" + k, "<missing>", b[k]))
            elif k not in b:
                out.append((path + "/" + k, a[k], "<missing>"))
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


payload = json.load(open("tests/fixtures/rc_payload.json"))
expected = json.load(open("tests/fixtures/rc_expected.json"))

win, pairs = load_from_notion_payload(payload)

rebuilt = {
    "notion_daily": {"results": [day_to_page(d) for d in win.days]},
    "notion_pairs": payload["notion_pairs"],
    "previous_matrix": payload.get("previous_matrix", {}),
}

body, status, _ = rc_v3.compute_relational_matrix(Req(rebuilt))
got = json.loads(body)

d = diff(got, expected)
print("status:", status)
print("loader n_eligible:", win.n_eligible)
print("DIFFS vs rc_expected.json:", len(d))
for p, g, e in d[:20]:
    print("  ", p, "| got:", g, "| exp:", e)
