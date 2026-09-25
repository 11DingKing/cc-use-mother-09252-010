"""计算服务：幂等任务、断点恢复、报告版本序列与差异。"""
from __future__ import annotations

import json
from typing import Any, Optional

from ..domain import engine, models as m
from ..domain.errors import ConflictError, NotFoundError, ValidationError
from ..persistence.uow import Database


class CalculationService:
    def __init__(self, store: Any = None) -> None:
        # store 可选：注入后每个检查点即时落盘，进程崩溃也能恢复
        self._store = store

    def create_task(self, db: Database, principal: m.Principal,
                    project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if project_id not in db.projects:
            raise NotFoundError(f"项目不存在: {project_id}")
        ws = payload.get("window_start")
        we = payload.get("window_end")
        _date(ws, "window_start"); _date(we, "window_end")
        if ws > we:
            raise ValidationError("观察期开始晚于结束")
        idem = payload.get("idempotency_key")
        if not idem:
            raise ValidationError("缺少 idempotency_key")
        existing = db.idem_index.get(("task", project_id, idem))
        if existing and existing in db.tasks:
            return db.tasks[existing].to_dict()

        task = m.CalculationTask(
            id=db.meta["idgen"].next_id("task"),
            idempotency_key=idem, project_id=project_id,
            window_start=ws, window_end=we, requested_by=principal.id,
            created_at=db.meta["clock"].now(), updated_at=db.meta["clock"].now(),
            status=m.TASK_PENDING)
        db.tasks[task.id] = task
        db.idem_index[("task", project_id, idem)] = task.id
        return task.to_dict()

    def get_task(self, db: Database, task_id: str) -> m.CalculationTask:
        task = db.tasks.get(task_id)
        if task is None:
            raise NotFoundError(f"任务不存在: {task_id}")
        return task

    def run_task(self, db: Database, task_id: str,
                 fail_after: Optional[int] = None,
                 resume: bool = False) -> dict[str, Any]:
        """执行（或断点恢复继续执行）任务。

        fail_after 仅用于测试断点恢复：完成 N 个指标后抛出注入错误。
        幂等：已完成任务直接返回；并发执行靠状态 CAS（pending/failed -> running）。
        resume=True 用于进程崩溃后接管残留的 running 任务（检查点不重算）。
        """
        task = self.get_task(db, task_id)
        if task.status == m.TASK_COMPLETED:
            return task.to_dict()
        if task.status == m.TASK_RUNNING and not resume:
            raise ConflictError(f"任务 {task_id} 正在执行中")
        if task.status not in (m.TASK_PENDING, m.TASK_FAILED, m.TASK_RUNNING):
            raise ConflictError(f"任务状态 {task.status} 不可执行")
        # CAS：仅 pending/failed 可启动（崩溃后 failed 允许恢复）
        task.status = m.TASK_RUNNING
        task.attempts += 1
        task.updated_at = db.meta["clock"].now()

        try:
            metric_versions = self._active_metrics(db)
            rules = self._active_rules(db)
            points = [p for p in db.points.values()
                      if p.project_id == task.project_id]
            order = engine.ordered_metric_codes(metric_versions)

            # 恢复检查点：已完成指标直接复算其暂存行
            rows_state = self._load_rows_state(db, task)
            done = set(task.checkpoint)
            produced = 0
            for code in order:
                if code in done:
                    continue
                metric = metric_versions[code]
                cache = self._cache_from_state(metric_versions, rows_state)
                if metric.kind == m.METRIC_KIND_RATIO:
                    row = engine._metric_value(metric, points, rules,
                                               task.window_start, task.window_end,
                                               cache)
                else:
                    row = engine._metric_value(metric, points, rules,
                                               task.window_start, task.window_end)
                rows_state[code] = row
                task.checkpoint.append(code)
                done.add(code)
                self._persist_checkpoint(db, task, rows_state)
                produced += 1
                if fail_after is not None and produced >= fail_after:
                    raise RuntimeError("注入的执行中断（模拟崩溃）")

            rows = [rows_state[c] for c in order]
            rows.sort(key=lambda r: (r["category"], r["metric_code"]))

            # 报告按 (项目,窗口) 成版本序列；迟到数据重算只追加新版本
            series_key = (task.project_id, task.window_start, task.window_end)
            prev_ids = db.report_series.get(series_key, [])
            report_id = db.meta["idgen"].next_id("report")
            report = engine.finalize_report(
                task.project_id, task.window_start, task.window_end,
                metric_versions, rules, rows, points,
                generated_at=db.meta["clock"].now(),
                generated_by=task.requested_by,
                task_id=task.id, report_id=report_id)
            db.reports[report_id] = report
            db.report_series[series_key] = prev_ids + [report_id]

            task.status = m.TASK_COMPLETED
            task.report_id = report_id
            task.updated_at = db.meta["clock"].now()
            self._clear_rows_state(db, task)
            return task.to_dict()
        except Exception as e:
            task.status = m.TASK_FAILED
            task.last_error = str(e)
            task.updated_at = db.meta["clock"].now()
            # 失败同样持久化：保留检查点，供断点恢复（外层 UoW 遇异常不会落盘）
            if self._store is not None:
                self._store.save(db.to_snapshot())
            raise

    def list_reports(self, db: Database, project_id: str,
                     window_start: Optional[str] = None,
                     window_end: Optional[str] = None) -> list[dict[str, Any]]:
        out = []
        for (pid, ws, we), ids in db.report_series.items():
            if pid != project_id:
                continue
            if window_start and ws != window_start:
                continue
            if window_end and we != window_end:
                continue
            for rid in ids:
                r = db.reports[rid]
                out.append({"report_id": r.id, "project_id": r.project_id,
                            "window": [r.window_start, r.window_end],
                            "generated_at": r.generated_at,
                            "generated_by": r.generated_by,
                            "fingerprint": r.fingerprint,
                            "conclusion": r.conclusion,
                            "version_index": ids.index(rid) + 1,
                            "series_length": len(ids)})
        out.sort(key=lambda x: (x["window"][0], x["generated_at"]))
        return out

    def get_report(self, db: Database, report_id: str) -> m.Report:
        r = db.reports.get(report_id)
        if r is None:
            raise NotFoundError(f"报告不存在: {report_id}")
        return r

    def report_diff(self, db: Database, project_id: str,
                    window_start: str, window_end: str) -> dict[str, Any]:
        ids = db.report_series.get((project_id, window_start, window_end))
        if not ids:
            raise NotFoundError("该观察期尚无报告")
        if len(ids) < 2:
            return {"changed": False, "note": "仅有一个版本，无差异可比",
                    "reports": [ids[0]]}
        return engine.diff_reports(db.reports[ids[-2]], db.reports[ids[-1]])

    # ---- 内部 -------------------------------------------------------------

    def _active_metrics(self, db: Database) -> dict[str, m.MetricVersion]:
        """取每个指标最新版本。报告内冻结版本号，旧报告永不随定义更新而变。"""
        return {code: vs[-1] for code, vs in db.metrics.items()}

    def _active_rules(self, db: Database) -> list[m.RuleVersion]:
        """每个 rule_id 只暴露当前生效版本；pending/rejected/retired 不参与计算。"""
        out = []
        for versions in db.rules.values():
            active = [v for v in versions if v.status == m.RULE_ACTIVE]
            if active:
                out.append(active[-1])
        return out

    def _load_rows_state(self, db: Database, task: m.CalculationTask) -> dict[str, Any]:
        raw = db.task_rows.get(task.id)
        return json.loads(raw) if raw else {}

    def _persist_checkpoint(self, db: Database, task: m.CalculationTask,
                            rows_state: dict[str, Any]) -> None:
        db.task_rows[task.id] = json.dumps(rows_state, ensure_ascii=False)
        if self._store is not None:
            # 即时落盘：即便进程在指标之间崩溃，已完成检查点也不丢
            self._store.save(db.to_snapshot())

    def _clear_rows_state(self, db: Database, task: m.CalculationTask) -> None:
        db.task_rows.pop(task.id, None)

    def _cache_from_state(self, metric_versions: dict[str, m.MetricVersion],
                          rows_state: dict[str, Any]) -> dict[str, Any]:
        cache: dict[str, Any] = {}
        for code, row in rows_state.items():
            if code in metric_versions and \
                    metric_versions[code].kind != m.METRIC_KIND_RATIO:
                cache[code] = {"value": row["value"],
                               "points_used": row["points_used"],
                               "rules_used": row["rules_used"]}
        return cache


def _date(s: Any, field: str) -> str:
    if not isinstance(s, str) or len(s) != 10:
        raise ValidationError(f"{field} 必须为 YYYY-MM-DD")
    y, mo, d = s.split("-")
    int(y); int(mo); int(d)
    return s
