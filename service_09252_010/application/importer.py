"""数据导入：按逻辑键做版本化登记；迟到数据只形成新版本，绝不覆盖旧值。"""
from __future__ import annotations

from typing import Any

from ..domain import models as m
from ..domain.errors import NotFoundError, ValidationError
from ..persistence.uow import Database


class ImportService:
    def submit_batch(self, db: Database, principal: m.Principal,
                     project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if project_id not in db.projects:
            raise NotFoundError(f"项目不存在: {project_id}")
        rows = payload.get("rows")
        if not isinstance(rows, list) or not rows:
            raise ValidationError("rows 必须为非空数组")
        idem = payload.get("idempotency_key")
        if idem and ("batch", project_id, idem) in db.idem_index:
            # 批次幂等：相同键重复提交返回原批次
            batch_id = db.idem_index[("batch", project_id, idem)]
            return self._batch_view(db, batch_id, replayed=True)

        batch_id = db.meta["idgen"].next_id("batch")
        now = db.meta["clock"].now()
        new_versions = 0
        point_ids: list[str] = []
        for i, row in enumerate(rows):
            try:
                point, is_new_version = self._build_point(
                    db, principal, project_id, batch_id, now, row)
            except ValidationError as e:
                raise ValidationError(f"第 {i} 行: {e}") from e
            db.points[point.id] = point
            point_ids.append(point.id)
            new_versions += int(is_new_version)

        db.batches[batch_id] = {
            "id": batch_id, "project_id": project_id,
            "submitted_by": principal.id, "submitted_at": now,
            "idempotency_key": idem, "point_ids": point_ids,
            "accepted": len(point_ids), "late_versions": new_versions,
            "note": payload.get("note", "")}
        if idem:
            db.idem_index[("batch", project_id, idem)] = batch_id
        return self._batch_view(db, batch_id, replayed=False)

    def _build_point(self, db: Database, principal: m.Principal, project_id: str,
                     batch_id: str, now: str, row: dict[str, Any]
                     ) -> tuple[m.DataPoint, bool]:
        for key in ("metric_code", "country", "caliber", "period_start", "period_end"):
            if not row.get(key):
                raise ValidationError(f"缺少 {key}")
        ps, pe = row["period_start"], row["period_end"]
        if ps > pe:
            raise ValidationError("period_start 晚于 period_end")
        value = row.get("value", None)
        if value is not None:
            value = float(value)
        ev_raw = row.get("evidence") or {}
        evidence = m.Evidence(
            source=ev_raw.get("source", "unknown"),
            locator=ev_raw.get("locator", ""),
            sha256=ev_raw.get("sha256"))

        logical = (project_id, row["metric_code"], row["country"], row["caliber"],
                   ps, pe)
        max_version = 0
        for p in db.points.values():
            if p.logical_key() == logical:
                max_version = max(max_version, p.version_no)
        if max_version:
            # 迟到数据：形成新版本
            version_no = max_version + 1
            is_new = True
        else:
            version_no = 1
            is_new = False
        pid = db.meta["idgen"].next_id("point")
        point = m.DataPoint(
            id=pid, batch_id=batch_id, project_id=project_id,
            metric_code=row["metric_code"], version_no=version_no,
            country=row["country"], caliber=row["caliber"],
            period_start=ps, period_end=pe, value=value, evidence=evidence,
            owner_org=row.get("owner_org", principal.org_id),
            recorded_at=now, note=row.get("note", ""))
        return point, is_new

    def list_points(self, db: Database, project_id: str,
                    metric_code: str | None = None) -> list[dict[str, Any]]:
        out = []
        for p in db.points.values():
            if p.project_id != project_id:
                continue
            if metric_code and p.metric_code != metric_code:
                continue
            out.append(p.to_dict())
        out.sort(key=lambda p: (p["metric_code"], p["country"], p["period_start"],
                                p["version_no"]))
        return out

    def list_batches(self, db: Database, project_id: str) -> list[dict[str, Any]]:
        return [b for b in db.batches.values() if b["project_id"] == project_id]

    def _batch_view(self, db: Database, batch_id: str, replayed: bool) -> dict[str, Any]:
        view = dict(db.batches[batch_id])
        view["replayed"] = replayed
        return view
