"""Per-decision JSON snapshots.

Every committee decision is written as one self-contained JSON file holding
every input, every model opinion, the arbiter ruling, the Risk Marshal verdict
and the resulting order intent. That is what makes a post-hoc review possible:
you can always answer "what did each model see, and what did it say?"

Layout:  <root>/<YYYY-MM-DD>/<cycle_id>/<TICKER>.json  (+ _cycle.json summary)
Writes are atomic (tmp file + rename), so a crash never leaves half a snapshot.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def to_jsonable(obj: Any) -> Any:
    """Recursively convert Decimal/datetime/Enum/dataclass/tuple/set into JSON types."""
    if hasattr(obj, "to_dict") and callable(obj.to_dict):
        return to_jsonable(obj.to_dict())
    if is_dataclass(obj) and not isinstance(obj, type):
        return to_jsonable(asdict(obj))
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in obj]
    return obj


def _safe(name: str) -> str:
    if not _SAFE_NAME.match(name):
        raise ValueError(f"unsafe path component: {name!r}")
    return name


class AuditTrail:
    def __init__(self, root: str | Path, today: date | None = None) -> None:
        self.root = Path(root)
        self.today = (today or datetime.now(timezone.utc).date()).isoformat()

    def cycle_dir(self, cycle_id: str) -> Path:
        return self.root / self.today / _safe(cycle_id)

    def _write(self, path: Path, payload: dict[str, Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(to_jsonable(payload), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def save_decision(self, cycle_id: str, ticker: str, snapshot: dict[str, Any]) -> Path:
        record = {
            "schema_version": SCHEMA_VERSION,
            "cycle_id": cycle_id,
            "ticker": ticker,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            **snapshot,
        }
        return self._write(self.cycle_dir(cycle_id) / f"{_safe(ticker.upper())}.json", record)

    def save_cycle_summary(self, cycle_id: str, summary: dict[str, Any]) -> Path:
        record = {"schema_version": SCHEMA_VERSION, "cycle_id": cycle_id,
                  "saved_at": datetime.now(timezone.utc).isoformat(), **summary}
        return self._write(self.cycle_dir(cycle_id) / "_cycle.json", record)

    def load_decision(self, cycle_id: str, ticker: str) -> dict[str, Any]:
        path = self.cycle_dir(cycle_id) / f"{_safe(ticker.upper())}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def list_cycles(self) -> list[str]:
        day = self.root / self.today
        return sorted(p.name for p in day.iterdir() if p.is_dir()) if day.exists() else []
