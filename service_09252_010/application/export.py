"""导出服务：JSON 复算包（含定义/规则/数据/证据快照与指纹）与 CSV 结论表。"""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from ..domain.errors import NotFoundError
from ..persistence.uow import Database


class ExportService:
    def export_json(self, db: Database, report_id: str) -> dict[str, Any]:
        report = self._get(db, report_id)
        return {
            "format": "project-effect-report",
            "version": 1,
            "report": report.to_dict(),
            "replay": {
                "fingerprint": report.fingerprint,
                "metric_versions": report.metric_versions,
                "rule_versions": report.rule_versions,
                "data_versions": report.data_versions,
                "note": "使用以上冻结版本即可独立复算 rows 与 conclusion",
            },
        }

    def export_csv(self, db: Database, report_id: str) -> str:
        report = self._get(db, report_id)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["# 报告", report.id, "项目", report.project_id,
                    "窗口", f"{report.window_start}~{report.window_end}",
                    "指纹", report.fingerprint])
        w.writerow(["指标代码", "指标版本", "类别", "指标名称", "单位",
                    "数值", "目标值", "判定", "所用数据点", "所用换算规则", "备注"])
        for r in report.rows:
            w.writerow([r["metric_code"], r["metric_version"], r["category"],
                        r["name"], r["unit"],
                        "" if r["value"] is None else r["value"],
                        "" if r["target"] is None else r["target"],
                        r["verdict"], ";".join(r["points_used"]),
                        ";".join(r["rules_used"]), r["note"]])
        w.writerow([])
        w.writerow(["# 结论", json.dumps(report.conclusion, ensure_ascii=False)])
        w.writerow(["# 数据版本与证据"])
        w.writerow(["数据点", "指标", "版本", "国家", "口径", "期间",
                    "数值", "证据来源", "证据定位", "证据SHA256"])
        for d in report.data_versions:
            ev = d["evidence"]
            w.writerow([d["point_id"], d["metric_code"], d["version_no"],
                        d["country"], d["caliber"],
                        f"{d['period_start']}~{d['period_end']}",
                        "" if d["value"] is None else d["value"],
                        ev["source"], ev["locator"], ev.get("sha256") or ""])
        return buf.getvalue()

    def _get(self, db: Database, report_id: str):
        report = db.reports.get(report_id)
        if report is None:
            raise NotFoundError(f"报告不存在: {report_id}")
        return report
