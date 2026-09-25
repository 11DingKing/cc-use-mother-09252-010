"""复核服务：授权机构对报告出具通过/驳回结论；报告复核状态按机构汇总。"""
from __future__ import annotations

from typing import Any

from ..domain import models as m
from ..domain.errors import ConflictError, NotFoundError, ValidationError
from ..persistence.uow import Database


class ReviewService:
    def submit(self, db: Database, principal: m.Principal,
               report_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        report = db.reports.get(report_id)
        if report is None:
            raise NotFoundError(f"报告不存在: {report_id}")
        state = payload.get("state")
        if state not in (m.REVIEW_APPROVED, m.REVIEW_REJECTED):
            raise ValidationError("state 必须为 approved / rejected")
        key = (report_id, principal.org_id)
        existing = next((r for r in db.reviews.values()
                         if r.report_id == report_id and r.org_id == principal.org_id),
                        None)
        if existing and existing.state == m.REVIEW_APPROVED and state == m.REVIEW_APPROVED:
            return existing.to_dict()  # 幂等
        if existing and existing.state == m.REVIEW_APPROVED:
            raise ConflictError("已通过的复核不可改判，如需纠正请生成新版本报告")
        rid = db.meta["idgen"].next_id("review")
        rec = m.ReviewRecord(
            id=rid, report_id=report_id, org_id=principal.org_id,
            reviewer_id=principal.id, state=state,
            comment=payload.get("comment", ""),
            reviewed_at=db.meta["clock"].now())
        if existing:
            db.reviews.pop(existing.id, None)
        db.reviews[rid] = rec
        return rec.to_dict()

    def status(self, db: Database, report_id: str) -> dict[str, Any]:
        if report_id not in db.reports:
            raise NotFoundError(f"报告不存在: {report_id}")
        recs = [r.to_dict() for r in db.reviews.values() if r.report_id == report_id]
        recs.sort(key=lambda r: r["reviewed_at"])
        approved = [r for r in recs if r["state"] == m.REVIEW_APPROVED]
        rejected = [r for r in recs if r["state"] == m.REVIEW_REJECTED]
        if rejected:
            overall = "rejected"
        elif approved:
            overall = "approved"
        else:
            overall = "pending"
        return {"report_id": report_id, "overall": overall, "reviews": recs}
