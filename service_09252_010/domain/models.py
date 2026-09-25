"""领域模型：指标定义、数据版本、换算规则、会签、计算任务、报告、复核。

模型均为可序列化 dataclass；报告对计算时所用的指标/规则版本做完整快照，
因此指标定义更新或规则回滚不会改写历史报告。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# ---- 枚举（字符串常量，便于序列化）-----------------------------------------

METRIC_KIND_MEASURE = "measure"   # 普通计量指标
METRIC_KIND_RATIO = "ratio"       # 比率指标（由分子/分母两个指标合成）

AGG_SUM = "sum"
AGG_AVG = "avg"
AGG_LATEST = "latest"

MISSING_EXCLUDE = "exclude"       # 缺失值不参与
MISSING_ZERO = "zero"             # 缺失按 0 处理
MISSING_FAIL = "fail"             # 存在缺失则该指标结论不可判定

SIGNOFF_PENDING = "pending"
SIGNOFF_APPROVED = "approved"
SIGNOFF_REJECTED = "rejected"

RULE_ACTIVE = "active"
RULE_PENDING = "pending"     # 新版本待会签
RULE_REJECTED = "rejected"   # 会签驳回
RULE_RETIRED = "retired"

TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_COMPLETED = "completed"
TASK_FAILED = "failed"

REVIEW_APPROVED = "approved"
REVIEW_REJECTED = "rejected"

# 权限粒度（scope）
SCOPE_IMPORT = "import"            # /projects/{id} 由具体授权条目收窄
SCOPE_SIGNOFF = "signoff"
SCOPE_CALC = "calc"
SCOPE_REVIEW = "review"
SCOPE_READ = "read"
SCOPE_ADMIN = "admin"


def canonical_dumps(payload: Any) -> str:
    """以稳定键序序列化，用于指纹与快照。"""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(payload: Any) -> str:
    return hashlib.sha256(canonical_dumps(payload).encode("utf-8")).hexdigest()


@dataclass
class Org:
    id: str
    name: str
    country: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Project:
    id: str
    name: str
    owner_org: str
    countries: list[str] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Principal:
    id: str
    name: str
    org_id: str
    # 授权条目：(scope, project_id|None)；project_id 为 None 表示该 scope 全项目
    grants: list[tuple[str, Optional[str]]] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)

    def can(self, scope: str, project_id: Optional[str] = None) -> bool:
        for s, p in self.grants:
            if s == SCOPE_ADMIN:
                return True
            if s == scope and (p is None or p == project_id):
                return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "org_id": self.org_id,
                "grants": [[s, p] for s, p in self.grants],
                "tokens": list(self.tokens)}


@dataclass
class MetricVersion:
    """指标定义的一个不可变版本。"""
    code: str
    version_no: int
    name: str
    category: str                       # enrollment / employment / teacher ...
    kind: str = METRIC_KIND_MEASURE
    unit: str = ""
    aggregation: str = AGG_SUM
    standard_caliber: str = "standard"
    numerator_code: Optional[str] = None
    denominator_code: Optional[str] = None
    target: Optional[float] = None
    missing_policy: str = MISSING_EXCLUDE
    created_by: str = ""
    created_at: str = ""

    def spec(self) -> dict[str, Any]:
        """计算时冻结进报告的定义快照。"""
        return {k: getattr(self, k) for k in (
            "code", "version_no", "name", "category", "kind", "unit",
            "aggregation", "standard_caliber", "numerator_code",
            "denominator_code", "target", "missing_policy")}


@dataclass
class Evidence:
    source: str           # 来源类型：agreement / registry / report ...
    locator: str          # 文件号、链接或台账定位
    sha256: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DataPoint:
    """同一逻辑键（项目+指标+国家+口径+期间）迟到数据以更大的 version_no 形成新版本。"""
    id: str
    batch_id: str
    project_id: str
    metric_code: str
    version_no: int
    country: str
    caliber: str
    period_start: str
    period_end: str
    value: Optional[float]
    evidence: Evidence
    owner_org: str
    recorded_at: str
    note: str = ""

    def logical_key(self) -> tuple[str, str, str, str, str, str]:
        return (self.project_id, self.metric_code, self.country, self.caliber,
                self.period_start, self.period_end)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


@dataclass
class RuleVersion:
    rule_id: str
    version_no: int
    source_caliber: str
    target_caliber: str
    country: str                         # 适用国家，"*" 表示通用
    transform: dict[str, Any]            # {"type": "factor", "factor": x} 或 linear
    effective_date: str
    status: str = RULE_ACTIVE
    note: str = ""
    created_by: str = ""
    created_at: str = ""

    def applies_to(self, country: str, source_caliber: str, target_caliber: str,
                   on_date: str) -> bool:
        if self.status != RULE_ACTIVE:
            return False
        if self.source_caliber != source_caliber or self.target_caliber != target_caliber:
            return False
        if self.country != "*" and self.country != country:
            return False
        return self.effective_date <= on_date

    def convert(self, value: float) -> float:
        t = self.transform
        kind = t.get("type", "factor")
        if kind == "factor":
            return value * float(t["factor"])
        if kind == "linear":
            return float(t["slope"]) * value + float(t["intercept"])
        raise ValueError(f"未知换算类型: {kind}")

    def spec(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in (
            "rule_id", "version_no", "source_caliber", "target_caliber",
            "country", "transform", "effective_date", "status", "note")}


@dataclass
class Signoff:
    """口径会签：须所有必备机构全部同意才生效；并发同意靠应用层锁保证只生效一次。"""
    rule_id: str
    version_no: int
    required_parties: list[str]
    approvals: dict[str, dict[str, str]] = field(default_factory=dict)
    rejections: dict[str, dict[str, str]] = field(default_factory=dict)
    state: str = SIGNOFF_PENDING
    decided_at: str = ""

    def remaining(self) -> list[str]:
        return [p for p in self.required_parties if p not in self.approvals]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CalculationTask:
    id: str
    idempotency_key: str
    project_id: str
    window_start: str
    window_end: str
    requested_by: str
    created_at: str
    updated_at: str = ""
    status: str = TASK_PENDING
    checkpoint: list[str] = field(default_factory=list)   # 已完成指标 code
    attempts: int = 0
    report_id: Optional[str] = None
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Report:
    id: str
    project_id: str
    window_start: str
    window_end: str
    generated_at: str
    generated_by: str
    task_id: str
    metric_versions: dict[str, dict[str, Any]]      # code -> 冻结定义
    rule_versions: list[dict[str, Any]]             # 冻结规则版本
    rows: list[dict[str, Any]]
    conclusion: dict[str, Any]
    fingerprint: str
    data_versions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewRecord:
    id: str
    report_id: str
    org_id: str
    reviewer_id: str
    state: str
    comment: str
    reviewed_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
