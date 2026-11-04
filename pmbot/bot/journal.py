"""Append-only JSONL journal of everything the bot decides and does."""
import json
import os
import time


class Journal:
    def __init__(self, directory):
        os.makedirs(directory, exist_ok=True)
        self.dir = directory
        self.f = open(os.path.join(directory, "events.jsonl"), "a", buffering=1)

    def log(self, kind, **kw):
        rec = {"t": round(time.time(), 3), "type": kind, **kw}
        self.f.write(json.dumps(rec, default=str, separators=(",", ":")) + "\n")

    def close(self):
        self.f.close()
