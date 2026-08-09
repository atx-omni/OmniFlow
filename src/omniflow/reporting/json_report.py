from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..security import redact


def write_json_report(path: str | Path, payload: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(redact(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")
