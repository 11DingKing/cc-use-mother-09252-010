"""领域计算引擎测试：缺失值策略、跨年度窗口、口径换算、比率、复算与差异。"""
from __future__ import annotations

import unittest

from service_09252_010.domain import engine, models as m


def metric(code, **kw):
    base = dict(code=code, version_no=1, name=code, category="general",
                kind=m.METRIC_KIND_MEASURE, unit="人", aggregation=m.AGG_SUM,
                standard_caliber="standard", missing_policy=m.MISSING_EXCLUDE)
    base.update(kw)
    return m.MetricVersion(**base)


def point(pid, code, country, caliber, ps, pe, value, vno=1):
    return m.DataPoint(id=pid, batch_id="b1", project_id="prj", metric_code=code,
                       version_no=vno, country=country, caliber=caliber,
                       period_start=ps, period_end=pe, value=value,
                       evidence=m.Evidence("registry", f"doc/{pid}"),
                       owner_org="org-a", recorded_at="2026-01-01T00:00:00Z")


class EngineTests(unittest.TestCase):
    def test_missing_exclude_zero_fail_policies(self):
        pts = [point("p1", "m_ex", "CN", "standard", "2026-01-01", "2026-01-31", None),
               point("p2", "m_ex", "DE", "standard", "2026-01-01", "2026-01-31", 10),
               point("p3", "m_z", "CN", "standard", "2026-01-01", "2026-01-31", None),
               point("p4", "m_f", "CN", "standard", "2026-01-01", "2026-01-31", None)]
        metrics = {
            "m_ex": metric("m_ex", missing_policy=m.MISSING_EXCLUDE, target=5),
            "m_z": metric("m_z", missing_policy=m.MISSING_ZERO, target=5),
            "m_f": metric("m_f", missing_policy=m.MISSING_FAIL),
        }
        rows = {r["metric_code"]: r for r in engine.build_rows(
            "prj", "2026-01-01", "2026-12-31", metrics, pts, [])}
        self.assertEqual(rows["m_ex"]["value"], 10)          # 缺失被排除
        self.assertEqual(rows["m_ex"]["verdict"], "achieved")
        self.assertEqual(rows["m_z"]["value"], 0)            # 缺失按 0
        self.assertEqual(rows["m_z"]["verdict"], "not_achieved")
        self.assertIsNone(rows["m_f"]["value"])              # fail -> 不可判定
        self.assertEqual(rows["m_f"]["verdict"], "indeterminate")

    def test_cross_year_window_uses_overlap(self):
        # 数据期间跨年度；观察期也跨年度，只有重叠的才纳入
        pts = [point("p1", "m", "CN", "standard", "2025-09-01", "2026-08-31", 30),
               point("p2", "m", "CN", "standard", "2024-01-01", "2024-12-31", 99),
               point("p3", "m", "CN", "standard", "2027-01-01", "2027-12-31", 7),
               point("p4", "m", "CN", "standard", "2026-06-01", "2026-12-31", 5)]
        rows = engine.build_rows("prj", "2025-01-01", "2026-12-31",
                                 {"m": metric("m")}, pts, [])
        self.assertEqual(rows[0]["value"], 35)             # p1 + p4
        self.assertEqual(rows[0]["points_used"], ["p1", "p4"])

    def test_caliber_conversion_with_country_specific_rule(self):
        rule_general = m.RuleVersion("r1", 1, "local", "standard", "*",
                                     {"type": "factor", "factor": 1.0},
                                     "2020-01-01")
        rule_de = m.RuleVersion("r1", 2, "local", "standard", "DE",
                                {"type": "factor", "factor": 0.5},
                                "2020-01-01")
        pts = [point("p1", "m", "DE", "local", "2026-01-01", "2026-01-31", 20),
               point("p2", "m", "CN", "local", "2026-01-01", "2026-01-31", 20)]
        rows = engine.build_rows("prj", "2026-01-01", "2026-12-31",
                                 {"m": metric("m")}, pts, [rule_general, rule_de])
        row = rows[0]
        # DE 20*0.5=10（国家专属），CN 20*1.0=20（通用）
        self.assertEqual(row["value"], 30)
        self.assertIn("r1@v2", row["rules_used"])
        self.assertIn("r1@v1", row["rules_used"])

    def test_missing_conversion_rule_makes_indeterminate(self):
        pts = [point("p1", "m", "DE", "local", "2026-01-01", "2026-01-31", 20)]
        rows = engine.build_rows("prj", "2026-01-01", "2026-12-31",
                                 {"m": metric("m")}, pts, [])
        self.assertIsNone(rows[0]["value"])
        self.assertEqual(rows[0]["verdict"], "indeterminate")
        self.assertIn("换算规则", rows[0]["note"])

    def test_ratio_metric_and_zero_denominator(self):
        metrics = {"n": metric("n"), "d": metric("d"),
                   "rate": metric("rate", kind=m.METRIC_KIND_RATIO,
                                  numerator_code="n", denominator_code="d",
                                  target=0.8, unit="%")}
        pts = [point("p1", "n", "CN", "standard", "2026-01-01", "2026-01-31", 9),
               point("p2", "d", "CN", "standard", "2026-01-01", "2026-01-31", 10)]
        rows = {r["metric_code"]: r for r in engine.build_rows(
            "prj", "2026-01-01", "2026-12-31", metrics, pts, [])}
        self.assertAlmostEqual(rows["rate"]["value"], 0.9)
        self.assertEqual(rows["rate"]["verdict"], "achieved")

        pts_bad = [point("p3", "n", "CN", "standard", "2026-01-01", "2026-01-31", 9),
                   point("p4", "d", "CN", "standard", "2026-01-01", "2026-01-31", 0)]
        rows_bad = {r["metric_code"]: r for r in engine.build_rows(
            "prj", "2026-01-01", "2026-12-31", metrics, pts_bad, [])}
        self.assertIsNone(rows_bad["rate"]["value"])
        self.assertEqual(rows_bad["rate"]["verdict"], "indeterminate")

    def test_late_data_picks_latest_version_only(self):
        pts = [point("p1", "m", "CN", "standard", "2026-01-01", "2026-01-31", 10, 1),
               point("p2", "m", "CN", "standard", "2026-01-01", "2026-01-31", 7, 2)]
        rows = engine.build_rows("prj", "2026-01-01", "2026-12-31",
                                 {"m": metric("m")}, pts, [])
        self.assertEqual(rows[0]["value"], 7)          # 只取 v2
        self.assertEqual(rows[0]["points_used"], ["p2"])

    def test_report_is_replayable_by_fingerprint(self):
        metrics = {"m": metric("m", target=5)}
        rules = [m.RuleVersion("r1", 1, "local", "standard", "DE",
                               {"type": "linear", "slope": 2.0, "intercept": 1.0},
                               "2020-01-01")]
        pts = [point("p1", "m", "DE", "local", "2026-01-01", "2026-01-31", 20)]
        r1 = engine.calculate("prj", "2026-01-01", "2026-12-31", metrics, pts,
                              rules, "2026-02-01T00:00:00Z", "u", "t1", "rpt-1")
        r2 = engine.calculate("prj", "2026-01-01", "2026-12-31", metrics, pts,
                              rules, "2026-03-01T00:00:00Z", "u2", "t2", "rpt-2")
        # 时间/操作者/报告 ID 不影响指纹：同输入必同指纹
        self.assertEqual(r1.fingerprint, r2.fingerprint)
        self.assertAlmostEqual(r1.rows[0]["value"], 41)  # 20*2+1

    def test_diff_reports_detects_value_and_version_change(self):
        metrics = {"m": metric("m")}
        pts1 = [point("p1", "m", "CN", "standard", "2026-01-01", "2026-01-31", 10, 1)]
        pts2 = [point("p1", "m", "CN", "standard", "2026-01-01", "2026-01-31", 10, 1),
                point("p2", "m", "CN", "standard", "2026-01-01", "2026-01-31", 15, 2)]
        r1 = engine.calculate("prj", "2026-01-01", "2026-12-31", metrics, pts1, [],
                              "t", "u", "t1", "A")
        r2 = engine.calculate("prj", "2026-01-01", "2026-12-31", metrics, pts2, [],
                              "t", "u", "t2", "B")
        d = engine.diff_reports(r1, r2)
        self.assertTrue(d["changed"])
        self.assertEqual(d["row_changes"][0]["old_value"], 10)
        self.assertEqual(d["row_changes"][0]["new_value"], 15)
        self.assertEqual(d["row_changes"][0]["delta"], 5)
        self.assertEqual(d["data_version_changes"][0]["old_version"], 1)
        self.assertEqual(d["data_version_changes"][0]["new_version"], 2)


if __name__ == "__main__":
    unittest.main()
