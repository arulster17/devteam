import json
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path("run/log.jsonl")


def log_event(event_type: str, data: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        **data,
    }
    with LOG_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")
