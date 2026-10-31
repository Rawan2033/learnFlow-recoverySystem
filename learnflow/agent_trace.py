"""Append-only local JSONL trace. No raw prompts, credentials, or model reasoning."""
import json
from datetime import datetime, timezone
from pathlib import Path

from learnflow.agent_config import Redactor


class TraceWriter:
    def __init__(self, path, redact=None):
        self.redact = redact or Redactor()
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Do not overwrite an earlier investigation (or any other existing file).
        self.file = path.open('x', encoding='utf-8')

    def record(self, event, **data):
        entry = {'time': datetime.now(timezone.utc).isoformat(), 'event': event, **data}
        self.file.write(json.dumps(self.redact(entry), ensure_ascii=True) + '\n')
        self.file.flush()

    def close(self):
        self.file.close()
