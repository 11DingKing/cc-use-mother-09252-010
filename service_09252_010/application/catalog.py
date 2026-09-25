"""指标目录与换算规则的登记、版本化、会签与回滚。"""
from __future__ import annotations

from typing import Any, Optional

from ..domain import models as m
from ..domain.errors import ConflictError, NotFoundError, ValidationError
from ..persistence.uow import Database

VALID_AGG = {m.AGG_SUM, m.AGG_AVG, m.AGG_LATEST}
VALID_KIND = {m.METRIC_KIND_MEASURE, m.METRIC_KIND_RATIO}
VALID_MISSING = {m.MISSING_EXCLUDE, m.MISSING_ZERO, m.MISSING_FAIL}


def _require_date(s: str, field: str) -> str:
    if not isinstance(s, str) or len(s) != 10:
        raise ValidationError(f"{field} 必须为 YYYY-MM-DD")
    y, mo, d = s.split("-")
    int(y); int(mo); int(d)
    return s


class CatalogService:
    # ---- 指标定义 ---------------------------------------------------------

    def register_metric(self, db: Database, principal: m.Principal,
                        payload: dict[str, Any]) -> dict[str, Any]:
        code = payload.get("code")
        if not code:
            raise ValidationError("缺少指标 code")
        kind = payload.get("kind", m.METRIC_KIND_MEASURE)
        if kind not in VALID_KIND:
            raise ValidationError(f"非法指标类型: {kind}")
        agg = payload.get("aggregation", m.AGG_SUM)
        if agg not in VALID_AGG:
            raise ValidationError(f"非法聚合方式: {agg}")
        missing = payload.get("missing_policy", m.MISSING_EXCLUDE)
        if missing not in VALID_MISSING:
            raise ValidationError(f"非法缺失值策略: {missing}")
        if kind == m.METRIC_KIND_RATIO:
            if not payload.get("numerator_code") or not payload.get("denominator_code"):
                raise ValidationError("比率指标必须提供 numerator_code / denominator_code")
        target = payload.get("target")
        if target is not None:
            target = float(target)

        versions = db.metrics.setdefault(code, [])
        version_no = len(versions) + 1
        mv = m.MetricVersion(
            code=code, version_no=version_no,
            name=payload.get("name", code),
            category=payload.get("category", "general"),
            kind=kind, unit=payload.get("unit", ""), aggregation=agg,
            standard_caliber=payload.get("standard_caliber", "standard"),
            numerator_code=payload.get("numerator_code"),
            denominator_code=payload.get("denominator_code"),
            target=target, missing_policy=missing,
            created_by=principal.id, created_at=_now(db))
        versions.append(mv)
        return mv.__dict__.copy()

    def list_metrics(self, db: Database, code: Optional[str] = None) -> Any:
        if code:
            if code not in db.metrics:
                raise NotFoundError(f"指标不存在: {code}")
            return [v.__dict__.copy() for v in db.metrics[code]]
        return {c: [v.__dict__.copy() for v in vs] for c, vs in sorted(db.metrics.items())}

    def get_metric_version(self, db: Database, code: str,
                           version_no: Optional[int] = None) -> m.MetricVersion:
        if code not in db.metrics:
            raise NotFoundError(f"指标不存在: {code}")
        vs = db.metrics[code]
        if version_no is None:
            return vs[-1]
        for v in vs:
            if v.version_no == version_no:
                return v
        raise NotFoundError(f"指标 {code} 不存在版本 {version_no}")

    # ---- 换算规则 ---------------------------------------------------------

    def create_rule_version(self, db: Database, principal: m.Principal,
                            payload: dict[str, Any]) -> dict[str, Any]:
        rule_id = payload.get("rule_id")
        if not rule_id:
            raise ValidationError("缺少 rule_id")
        transform = payload.get("transform")
        self._validate_transform(transform)
        effective = _require_date(payload.get("effective_date", ""), "effective_date")
        versions = db.rules.setdefault(rule_id, [])
        if versions:
            prev_eff = versions[-1].effective_date
            if effective < prev_eff:
                raise ValidationError(
                    f"新版本生效日期 {effective} 早于上一版本 {prev_eff}，规则按时间序版本化")
        version_no = len(versions) + 1
        required = payload.get("required_parties") or []
        rv = m.RuleVersion(
            rule_id=rule_id, version_no=version_no,
            source_caliber=payload["source_caliber"],
            target_caliber=payload["target_caliber"],
            country=payload.get("country", "*"),
            transform=transform, effective_date=effective,
            status=m.RULE_PENDING if required else m.RULE_ACTIVE,
            note=payload.get("note", ""),
            created_by=principal.id, created_at=_now(db))
        versions.append(rv)
        if required:
            db.signoffs[(rule_id, version_no)] = m.Signoff(
                rule_id=rule_id, version_no=version_no,
                required_parties=list(required))
        return {"rule": rv.__dict__.copy(),
                "signoff_required": list(required)}

    def _validate_transform(self, transform: Any) -> None:
        if not isinstance(transform, dict) or "type" not in transform:
            raise ValidationError("transform 必须为含 type 的对象")
        kind = transform["type"]
        try:
            if kind == "factor":
                float(transform["factor"])
            elif kind == "linear":
                float(transform["slope"]); float(transform["intercept"])
            else:
                raise ValidationError(f"未知换算类型: {kind}")
        except (KeyError, TypeError, ValueError) as e:
            raise ValidationError(f"换算参数无效: {e}") from e

    def get_rule_version(self, db: Database, rule_id: str,
                         version_no: Optional[int] = None) -> m.RuleVersion:
        if rule_id not in db.rules:
            raise NotFoundError(f"规则不存在: {rule_id}")
        vs = db.rules[rule_id]
        if version_no is None:
            return vs[-1]
        for v in vs:
            if v.version_no == version_no:
                return v
        raise NotFoundError(f"规则 {rule_id} 不存在版本 {version_no}")

    def list_rules(self, db: Database) -> dict[str, Any]:
        return {rid: [v.__dict__.copy() for v in vs]
                for rid, vs in sorted(db.rules.items())}

    def rollback_rule(self, db: Database, principal: m.Principal,
                      rule_id: str, to_version: int) -> dict[str, Any]:
        """规则回滚：以新版本复制历史已会签版本的换算口径，绝不删除或改写历史版本。

        回滚版本直接生效（恢复的是曾经生效并完成会签的口径），并记录来源。
        """
        if rule_id not in db.rules:
            raise NotFoundError(f"规则不存在: {rule_id}")
        versions = db.rules[rule_id]
        target = next((v for v in versions if v.version_no == to_version), None)
        if target is None:
            raise NotFoundError(f"规则 {rule_id} 不存在版本 {to_version}")
        if target.status not in (m.RULE_ACTIVE, m.RULE_RETIRED):
            raise ValidationError(
                f"v{to_version} 状态为 {target.status}，从未生效，不可回滚恢复")
        new_no = versions[-1].version_no + 1
        rv = m.RuleVersion(
            rule_id=rule_id, version_no=new_no,
            source_caliber=target.source_caliber,
            target_caliber=target.target_caliber, country=target.country,
            transform=dict(target.transform), effective_date=_now(db)[:10],
            status=m.RULE_ACTIVE,
            note=f"回滚自 v{to_version}", created_by=principal.id,
            created_at=_now(db))
        versions.append(rv)
        return rv.__dict__.copy()

    # ---- 口径会签 ---------------------------------------------------------

    def signoff_approve(self, db: Database, principal: m.Principal,
                        rule_id: str, version_no: int, comment: str = "") -> dict[str, Any]:
        so = self._get_signoff(db, rule_id, version_no)
        rule = self.get_rule_version(db, rule_id, version_no)
        if principal.can(m.SCOPE_ADMIN):
            party = principal.org_id
            if party not in so.required_parties:
                # 管理员代为处理时须属于必备方
                raise ValidationError("管理员仅可代表其所在必备机构签批")
        else:
            party = principal.org_id
            if party not in so.required_parties:
                raise ValidationError(f"机构 {party} 不是该会签的必备方")
        # 幂等：同一机构重复同意直接返回现状
        if party in so.approvals:
            return so.to_dict()
        if so.state == m.SIGNOFF_REJECTED:
            raise ConflictError("会签已被驳回，不可再同意")
        so.approvals[party] = {"by": principal.id, "at": _now(db),
                               "comment": comment}
        so.rejections.pop(party, None)
        if not so.remaining():
            so.state = m.SIGNOFF_APPROVED
            so.decided_at = _now(db)
            rule.status = m.RULE_ACTIVE
        return so.to_dict()

    def signoff_reject(self, db: Database, principal: m.Principal,
                       rule_id: str, version_no: int, comment: str = "") -> dict[str, Any]:
        so = self._get_signoff(db, rule_id, version_no)
        rule = self.get_rule_version(db, rule_id, version_no)
        party = principal.org_id
        if party not in so.required_parties and not principal.can(m.SCOPE_ADMIN):
            raise ValidationError(f"机构 {party} 不是该会签的必备方")
        if so.state == m.SIGNOFF_APPROVED:
            raise ConflictError("会签已通过，不可驳回")
        so.rejections[party] = {"by": principal.id, "at": _now(db),
                                "comment": comment}
        so.state = m.SIGNOFF_REJECTED
        so.decided_at = _now(db)
        rule.status = m.RULE_REJECTED
        return so.to_dict()

    def _get_signoff(self, db: Database, rule_id: str, version_no: int) -> m.Signoff:
        so = db.signoffs.get((rule_id, version_no))
        if so is None:
            raise NotFoundError(f"规则 {rule_id} v{version_no} 无会签流程（可能创建时未要求会签）")
        return so

    def signoff_status(self, db: Database, rule_id: str, version_no: int) -> dict[str, Any]:
        return self._get_signoff(db, rule_id, version_no).to_dict()


def _now(db: Database) -> str:
    # 由 bootstrap 注入的时钟；放 db.meta 避免改构造签名扩散
    return db.meta["clock"].now()  # type: ignore[attr-defined]
