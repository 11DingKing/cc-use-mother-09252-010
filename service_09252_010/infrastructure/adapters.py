"""基础设施：真实时钟、序列标识、原子 JSON 快照存储。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any

from ..application.ports import Clock, IdGenerator, SnapshotStore


class SystemClock(Clock):
    def now(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SequenceIdGenerator(IdGenerator):
    """带类型前缀的进程内序列；时间戳注入时钟保证可测试。"""

    def __init__(self, clock: Clock | None = None) -> None:
        self._lock = threading.Lock()
        self._seq: dict[str, int] = {}
        self._clock = clock

    def next_id(self, kind: str) -> str:
        with self._lock:
            n = self._seq.get(kind, 0) + 1
            self._seq[kind] = n
        prefix = {"batch": "bat", "point": "pnt", "task": "tsk",
                  "report": "rpt", "review": "rev", "org": "org",
                  "project": "prj", "principal": "usr"}.get(kind, kind)
        return f"{prefix}_{n:06d}"


class JsonSnapshotStore(SnapshotStore):
    """快照写入数据目录（绝不写入源码目录）；临时文件 + rename 保证原子性。"""

    def __init__(self, path: str) -> None:
        self._path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def load(self) -> dict[str, Any] | None:
        if not os.path.exists(self._path):
            return None
        with open(self._path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save(self, snapshot: dict[str, Any]) -> None:
        directory = os.path.dirname(os.path.abspath(self._path))
        fd, tmp = tempfile.mkstemp(prefix=".snap-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
