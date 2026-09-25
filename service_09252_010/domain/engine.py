"""纯计算引擎：无副作用，所有输入显式传入，保证结论可复算。"""
from __future__ import annotations

from typing import Any, Optional

from . import models as m
from .errors import ValidationError


def in_window(point: "m.DataPoint", window_start: str, window_end: str) -> bool:
    """期间与观察期有重叠即纳入（支持跨年度窗口，日期为 ISO 字符串可直接比较）。"""
    return point.period_start <= window_end and point.period_end >= window_start


def latest_per_key(points: list["m.DataPoint"]) -> dict[tuple, "m.DataPoint"]:
    """同一逻辑键只取 version_no 最大的迟到数据版本。"""
    chosen: dict[tuple, "m.DataPoint"] = {}
    for p in points:
        k = p.logical_key()
        cur = chosen.get(k)
        if cur is None or (p.version_no, p.recorded_at) > (cur.version_no, cur.recorded_at):
            chosen[k] = p
    return chosen


def pick_rule(rules: list["m.RuleVersion"], country: str, source: str, target: str,
              on_date: str) -> Optional["m.RuleVersion"]:
    """选择生效的换算规则：国家专属优先于通用，其次生效日期最新。"""
    candidates = [r for r in rules if r.applies_to(country, source, target, on_date)]
    if not candidates:
        return None
    # 国家专属规则优先；同类中取生效日期最新（稳定复算：再按 rule_id、version_no 排序）
    specific = [r for r in candidates if r.country == country]
    pool = specific if specific else candidates
    return sorted(pool,
                  key=lambda r: (r.effective_date, r.rule_id, r.version_no),
                  reverse=True)[0]


def _aggregate(values: list[tuple[float, str]], aggregation: str) -> Optional[float]:
    """values 为 (数值, period_end)；返回聚合结果。"""
    if not values:
        return None
    if aggregation == m.AGG_SUM:
        return round(sum(v for v, _ in values), 10)
    if aggregation == m.AGG_AVG:
        return round(sum(v for v, _ in values) / len(values), 10)
    if aggregation == m.AGG_LATEST:
        return sorted(values, key=lambda x: x[1])[-1][0]
    raise ValidationError(f"不支持的聚合方式: {aggregation}")


def _metric_value(metric: "m.MetricVersion",
                  points: list["m.DataPoint"],
                  rules: list["m.RuleVersion"],
                  window_start: str, window_end: str,
                  ratio_cache: Optional[dict[str, dict[str, Any]]] = None,
                  ) -> dict[str, Any]:
    """计算单个指标，返回行明细（含换算依据与证据）。"""
    if metric.kind == m.METRIC_KIND_RATIO:
        if ratio_cache is None or metric.numerator_code not in ratio_cache \
                or metric.denominator_code not in ratio_cache:
            raise ValidationError(f"比率指标 {metric.code} 的分子/分母未就绪")
        num = ratio_cache[metric.numerator_code]
        den = ratio_cache[metric.denominator_code]
        if num["value"] is None or den["value"] is None:
            return _row(metric, None, "indeterminate",
                        "分子或分母无法确定（缺失策略 fail）", [], [])
        if den["value"] == 0:
            return _row(metric, None, "indeterminate", "分母为 0", [], [])
        value = round(num["value"] / den["value"], 10)
        return _row(metric, value, _verdict(metric, value),
                    "", num["points_used"] + den["points_used"],
                    num["rules_used"] + den["rules_used"])

    in_w = [p for p in points if p.metric_code == metric.code and in_window(p, window_start, window_end)]
    chosen = latest_per_key(in_w)
    ordered = sorted(chosen.values(), key=lambda p: (p.country, p.period_start, p.id))

    had_missing = any(p.value is None for p in ordered)
    scaled: list[tuple[float, str]] = []
    points_used: list[str] = []
    rules_used: list[str] = []
    for p in ordered:
        points_used.append(p.id)
        if p.value is None:
            if metric.missing_policy == m.MISSING_FAIL:
                return _row(metric, None, "indeterminate",
                            f"存在缺失值且策略为 fail（{p.country} {p.period_start}）",
                            points_used, rules_used)
            if metric.missing_policy == m.MISSING_ZERO:
                scaled.append((0.0, p.period_end))
            # exclude: 跳过
            continue
        val = float(p.value)
        if p.caliber != metric.standard_caliber:
            rule = pick_rule(rules, p.country, p.caliber, metric.standard_caliber,
                             p.period_end)
            if rule is None:
                return _row(metric, None, "indeterminate",
                            f"缺少 {p.country} 口径 {p.caliber}->{metric.standard_caliber} 的生效换算规则",
                            points_used, rules_used)
            val = rule.convert(val)
            rules_used.append(f"{rule.rule_id}@v{rule.version_no}")
        scaled.append((round(val, 10), p.period_end))

    if not scaled:
        if metric.missing_policy == m.MISSING_ZERO and metric.aggregation == m.AGG_SUM:
            return _row(metric, 0.0, _verdict(metric, 0.0), "", points_used, rules_used)
        note = "窗口内无有效数据" + ("（全部缺失且策略为 exclude）" if had_missing else "")
        return _row(metric, None, "indeterminate", note, points_used, rules_used)

    value = _aggregate(scaled, metric.aggregation)
    return _row(metric, value, _verdict(metric, value), "", points_used, rules_used)


def _verdict(metric: "m.MetricVersion", value: Optional[float]) -> str:
    if metric.target is None:
        return "no_target"
    return "achieved" if value is not None and value >= metric.target else "not_achieved"


def _row(metric: "m.MetricVersion", value: Optional[float], verdict: str, note: str,
         points_used: list[str], rules_used: list[str]) -> dict[str, Any]:
    return {"metric_code": metric.code, "metric_version": metric.version_no,
            "category": metric.category, "name": metric.name, "unit": metric.unit,
            "value": value, "target": metric.target, "verdict": verdict,
            "note": note, "points_used": points_used,
            "rules_used": sorted(set(rules_used))}


def ordered_metric_codes(metric_versions: dict[str, "m.MetricVersion"]) -> list[str]:
    """计算顺序：普通指标先、比率指标后，各自按 code 排序——即断点恢复的检查点顺序。"""
    measures = sorted(c for c, mv in metric_versions.items()
                      if mv.kind != m.METRIC_KIND_RATIO)
    ratios = sorted(c for c, mv in metric_versions.items()
                    if mv.kind == m.METRIC_KIND_RATIO)
    return measures + ratios


def build_rows(project_id: str, window_start: str, window_end: str,
               metric_versions: dict[str, "m.MetricVersion"],
               points: list["m.DataPoint"],
               rules: list["m.RuleVersion"]) -> list[dict[str, Any]]:
    """逐指标生成行（供断点恢复分步执行；与 calculate 内部逻辑一致）。"""
    cache: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    for code in ordered_metric_codes(metric_versions):
        metric = metric_versions[code]
        if metric.kind == m.METRIC_KIND_RATIO:
            row = _metric_value(metric, points, rules, window_start, window_end, cache)
        else:
            row = _metric_value(metric, points, rules, window_start, window_end)
            cache[metric.code] = {"value": row["value"],
                                  "points_used": row["points_used"],
                                  "rules_used": row["rules_used"]}
        rows.append(row)
    rows.sort(key=lambda r: (r["category"], r["metric_code"]))
    return rows


def calculate(project_id: str, window_start: str, window_end: str,
              metric_versions: dict[str, "m.MetricVersion"],
              points: list["m.DataPoint"],
              rules: list["m.RuleVersion"],
              generated_at: str, generated_by: str,
              task_id: str, report_id: str) -> "m.Report":
    """组装完整报告：先算普通指标（供比率引用），再算比率指标。"""
    if window_start > window_end:
        raise ValidationError("观察期开始晚于结束")
    rows = build_rows(project_id, window_start, window_end,
                      metric_versions, points, rules)
    return finalize_report(project_id, window_start, window_end,
                           metric_versions, rules, rows, points,
                           generated_at, generated_by, task_id, report_id)


def finalize_report(project_id: str, window_start: str, window_end: str,
                    metric_versions: dict[str, "m.MetricVersion"],
                    rules: list["m.RuleVersion"],
                    rows: list[dict[str, Any]],
                    points: list["m.DataPoint"],
                    generated_at: str, generated_by: str,
                    task_id: str, report_id: str) -> "m.Report":

    decided = [r for r in rows if r["verdict"] in ("achieved", "not_achieved")]
    achieved = [r for r in decided if r["verdict"] == "achieved"]
    conclusion = {
        "metrics_total": len(rows),
        "metrics_decided": len(decided),
        "metrics_achieved": len(achieved),
        "metrics_indeterminate": sum(1 for r in rows if r["verdict"] == "indeterminate"),
        "all_achieved": len(decided) == len(rows) and len(rows) > 0
                        and all(r["verdict"] == "achieved" for r in rows),
        "overall": (
            "passed" if decided and all(r["verdict"] == "achieved" for r in decided)
            and not any(r["verdict"] == "indeterminate" for r in rows)
            else "conditional" if achieved else "not_passed"),
    }

    used_point_ids = {pid for r in rows for pid in r["points_used"]}
    by_id = {p.id: p for p in points}
    data_versions = [
        {"point_id": pid, "metric_code": by_id[pid].metric_code,
         "version_no": by_id[pid].version_no, "country": by_id[pid].country,
         "caliber": by_id[pid].caliber, "period_start": by_id[pid].period_start,
         "period_end": by_id[pid].period_end, "value": by_id[pid].value,
         "evidence": by_id[pid].evidence.to_dict()}
        for pid in sorted(used_point_ids) if pid in by_id]

    snapshot = {
        "project_id": project_id, "window": [window_start, window_end],
        "metrics": {code: mv.spec() for code, mv in sorted(metric_versions.items())},
        "rules": [r.spec() for r in sorted(rules, key=lambda r: (r.rule_id, r.version_no))],
        "rows": rows, "data_versions": data_versions,
    }
    return m.Report(
        id=report_id, project_id=project_id, window_start=window_start,
        window_end=window_end, generated_at=generated_at, generated_by=generated_by,
        task_id=task_id,
        metric_versions={code: mv.spec() for code, mv in sorted(metric_versions.items())},
        rule_versions=[r.spec() for r in sorted(rules, key=lambda r: (r.rule_id, r.version_no))],
        rows=rows, conclusion=conclusion, data_versions=data_versions,
        fingerprint=m.fingerprint(snapshot))


def diff_reports(old: "m.Report", new: "m.Report") -> dict[str, Any]:
    """两个版本报告的逐指标差异与所用数据版本差异。"""
    old_rows = {r["metric_code"]: r for r in old.rows}
    row_changes = []
    for r in new.rows:
        prev = old_rows.get(r["metric_code"])
        old_v = prev["value"] if prev else None
        if prev is None or old_v != r["value"] or prev["verdict"] != r["verdict"]:
            delta = None
            if old_v is not None and r["value"] is not None:
                delta = round(r["value"] - old_v, 10)
            row_changes.append({
                "metric_code": r["metric_code"],
                "old_value": old_v, "new_value": r["value"],
                "delta": delta,
                "old_verdict": prev["verdict"] if prev else None,
                "new_verdict": r["verdict"],
            })
    old_dv = {(d["metric_code"], d["country"], d["caliber"], d["period_start"],
               d["period_end"]): d["version_no"] for d in old.data_versions}
    new_dv = {(d["metric_code"], d["country"], d["caliber"], d["period_start"],
               d["period_end"]): d["version_no"] for d in new.data_versions}
    version_changes = []
    for k, vn in sorted(new_dv.items()):
        if old_dv.get(k) != vn:
            version_changes.append({"metric_code": k[0], "country": k[1], "caliber": k[2],
                                    "period": [k[3], k[4]],
                                    "old_version": old_dv.get(k), "new_version": vn})
    return {
        "old_report_id": old.id, "new_report_id": new.id,
        "old_fingerprint": old.fingerprint, "new_fingerprint": new.fingerprint,
        "row_changes": row_changes, "data_version_changes": version_changes,
        "changed": bool(row_changes or version_changes),
    }
