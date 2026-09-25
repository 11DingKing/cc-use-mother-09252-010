"""内存数据库 + 工作单元（UoW）：线程锁内完成读改写，支持快照持久化与恢复。"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from typing import Any, Optional

from ..domain import models as m


def _evidence(d: dict[str, Any]) -> m.Evidence:
    return m.Evidence(**d)


def _principal(d: dict[str, Any]) -> m.Principal:
    return m.Principal(id=d["id"], name=d["name"], org_id=d["org_id"],
                       grants=[tuple(g) for g in d.get("grants", [])],
                       tokens=list(d.get("tokens", [])))


class Database:
    """全部实体的线程安全容器。结构刻意朴素，便于整体序列化快照。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.orgs: dict[str, m.Org] = {}
        self.principals: dict[str, m.Principal] = {}
        self.projects: dict[str, m.Project] = {}
        self.metrics: dict[str, list[m.MetricVersion]] = {}
        self.rules: dict[str, list[m.RuleVersion]] = {}
        self.signoffs: dict[tuple[str, int], m.Signoff] = {}
        self.points: dict[str, m.DataPoint] = {}
        self.batches: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, m.CalculationTask] = {}
        self.idem_index: dict[tuple[str, str, str], str] = {}
        self.reports: dict[str, m.Report] = {}
        self.report_series: dict[tuple[str, str, str], list[str]] = {}
        self.reviews: dict[str, m.ReviewRecord] = {}
        # 计算任务断点暂存：task_id -> JSON 行结果
        self.task_rows: dict[str, str] = {}
        self.dirty = False
        # 运行时注入的端口（不进入快照）
        self.meta: dict[str, Any] = {"clock": None, "idgen": None}

    # ---- 快照 -------------------------------------------------------------

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "orgs": {k: v.to_dict() for k, v in self.orgs.items()},
            "principals": {k: v.to_dict() for k, v in self.principals.items()},
            "projects": {k: v.to_dict() for k, v in self.projects.items()},
            "metrics": {k: [v.__dict__ for v in vs] for k, vs in self.metrics.items()},
            "rules": {k: [v.__dict__ for v in vs] for k, vs in self.rules.items()},
            "signoffs": {f"{k[0]}|{k[1]}": v.to_dict()
                         for k, v in self.signoffs.items()},
            "points": {k: v.to_dict() for k, v in self.points.items()},
            "batches": self.batches,
            "tasks": {k: v.to_dict() for k, v in self.tasks.items()},
            "idem_index": {json.dumps(list(k), ensure_ascii=False): v
                           for k, v in self.idem_index.items()},
            "reports": {k: v.to_dict() for k, v in self.reports.items()},
            "report_series": {f"{k[0]}|{k[1]}|{k[2]}": v
                              for k, v in self.report_series.items()},
            "reviews": {k: v.to_dict() for k, v in self.reviews.items()},
            "task_rows": self.task_rows,
        }

    @classmethod
    def from_snapshot(cls, snap: dict[str, Any]) -> "Database":
        db = cls()
        db.orgs = {k: m.Org(**v) for k, v in snap.get("orgs", {}).items()}
        db.principals = {k: _principal(v) for k, v in snap.get("principals", {}).items()}
        db.projects = {k: m.Project(**v) for k, v in snap.get("projects", {}).items()}
        db.metrics = {
            k: [m.MetricVersion(**v) for v in vs]
            for k, vs in snap.get("metrics", {}).items()}
        db.rules = {
            k: [m.RuleVersion(**v) for v in vs]
            for k, vs in snap.get("rules", {}).items()}
        for key, v in snap.get("signoffs", {}).items():
            rid, vn = key.rsplit("|", 1)
            db.signoffs[(rid, int(vn))] = m.Signoff(
                rule_id=v["rule_id"], version_no=v["version_no"],
                required_parties=v["required_parties"],
                approvals=v.get("approvals", {}), rejections=v.get("rejections", {}),
                state=v.get("state", m.SIGNOFF_PENDING), decided_at=v.get("decided_at", ""))
        db.points = {}
        for k, v in snap.get("points", {}).items():
            v = dict(v)
            v["evidence"] = _evidence(v["evidence"])
            db.points[k] = m.DataPoint(**v)
        db.batches = dict(snap.get("batches", {}))
        db.tasks = {k: m.CalculationTask(**v) for k, v in snap.get("tasks", {}).items()}
        db.idem_index = {}
        for key, v in snap.get("idem_index", {}).items():
            kind, pid, ikey = json.loads(key)
            db.idem_index[(kind, pid, ikey)] = v
        db.reports = {k: m.Report(**v) for k, v in snap.get("reports", {}).items()}
        db.report_series = {}
        for key, v in snap.get("report_series", {}).items():
            pid, ws, we = key.split("|", 2)
            db.report_series[(pid, ws, we)] = list(v)
        db.reviews = {k: m.ReviewRecord(**v) for k, v in snap.get("reviews", {}).items()}
        db.task_rows = dict(snap.get("task_rows", {}))
        return db


class UnitOfWork:
    """在锁内暴露数据库；正常退出时落快照。保存失败不影响内存状态一致性。"""

    def __init__(self, db: Database, store: Optional[Any] = None) -> None:
        self._db = db
        self._store = store

    def __enter__(self) -> Database:
        self._db.lock.acquire()
        return self._db

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None and self._store is not None:
                self._store.save(self._db.to_snapshot())
            self._db.dirty = exc_type is not None
        finally:
            self._db.lock.release()


@contextmanager
def transaction(db: Database, store: Optional[Any] = None):
    uow = UnitOfWork(db, store)
    with uow as d:
        yield d
