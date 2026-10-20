from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum

import pytest

from audit import AuditTrail, to_jsonable


class Color(Enum):
    RED = "red"


@dataclass
class Thing:
    a: int
    when: datetime


def test_to_jsonable_handles_common_types():
    out = to_jsonable({"d": Decimal("1.5"), "t": (1, 2), "e": Color.RED, "s": {3},
                       "dc": Thing(1, datetime(2026, 1, 2, tzinfo=timezone.utc)), "day": date(2026, 1, 2)})
    assert out == {"d": 1.5, "t": [1, 2], "e": "red", "s": [3],
                   "dc": {"a": 1, "when": "2026-01-02T00:00:00+00:00"}, "day": "2026-01-02"}
    json.dumps(out)


def test_save_and_load_decision(tmp_path):
    trail = AuditTrail(tmp_path, today=date(2026, 3, 6))
    path = trail.save_decision("c1", "acme", {"council": {"x": 1}, "price": Decimal("10.25")})
    assert path == tmp_path / "2026-03-06" / "c1" / "ACME.json"
    rec = trail.load_decision("c1", "ACME")
    assert rec["schema_version"] == 1 and rec["price"] == 10.25 and rec["council"] == {"x": 1}
    assert not list(path.parent.glob("*.tmp"))


def test_cycle_summary_and_listing(tmp_path):
    trail = AuditTrail(tmp_path, today=date(2026, 3, 6))
    trail.save_cycle_summary("c2", {"n": 1})
    trail.save_cycle_summary("c1", {"n": 1})
    assert trail.list_cycles() == ["c1", "c2"]


@pytest.mark.parametrize("bad", ["../escape", "a/b", "", "x" * 100])
def test_path_traversal_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        AuditTrail(tmp_path).save_decision(bad, "ACME", {})
