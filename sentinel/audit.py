"""Proof-of-work log: append-only JSONL, each entry hashed and chained to the previous one."""
import hashlib
import json
import threading
import time
from pathlib import Path


def _digest(prev: str, body: dict) -> str:
    payload = prev + json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


class AuditLog:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.prev = "0" * 64
        if self.path.exists():
            lines = self.path.read_text().strip().splitlines()
            if lines:
                self.prev = json.loads(lines[-1])["hash"]

    def record(self, **fields) -> str:
        body = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields}
        with self._lock:  # worker threads log concurrently; the hash chain must stay ordered
            h = _digest(self.prev, body)
            with self.path.open("a") as f:
                f.write(json.dumps({"body": body, "prev": self.prev, "hash": h}) + "\n")
            self.prev = h
        return h


def verify(path) -> bool:
    prev = "0" * 64
    for line in Path(path).read_text().splitlines():
        e = json.loads(line)
        if e["prev"] != prev or _digest(prev, e["body"]) != e["hash"]:
            return False
        prev = e["hash"]
    return True
