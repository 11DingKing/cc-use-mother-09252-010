"""端到端 API 与应用服务测试。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest

from service_09252_010.domain import models as m
from service_09252_010.interfaces.http import HttpApp
from tests.support import Client, FixedClock, make_app

ADMIN, A, B, AUDIT, CALC = ("token-admin", "token-a", "token-b",
                            "token-audit", "token-calc")
PID = "prj-demo"
W0, W1 = "2025-09-01", "2026-08-31"


def enrollment_rows(v_de=100, v_cn=200, missing=False):
    rows = [
        {"metric_code": "enrollment", "country": "DE", "caliber": "local",
         "period_start": "2025-09-01", "period_end": "2026-08-31",
         "value": v_de, "evidence": {"source": "registry", "locator": "DE-REG-1"}},
        {"metric_code": "enrollment", "country": "CN", "caliber": "standard",
         "period_start": "2025-09-01", "period_end": "2026-08-31",
         "value": (None if missing else v_cn),
         "evidence": {"source": "registry", "locator": "CN-REG-1"}},
    ]
    return rows


class ApiTestBase(unittest.TestCase):
    def setUp(self):
        self.container, self.http, self.clock = make_app()
        self.c = Client(HttpApp(self.container))

    def create_catalog(self):
        # 指标：招生（sum）、毕业生数、就业率（比率）、师资培养（sum）
        for body in [
            {"code": "enrollment", "name": "招生人数", "category": "enrollment",
             "standard_caliber": "standard", "target": 200},
            {"code": "graduates", "name": "毕业生数", "category": "employment",
             "standard_caliber": "standard"},
            {"code": "employed", "name": "就业人数", "category": "employment",
             "standard_caliber": "standard"},
            {"code": "employment_rate", "name": "就业率", "category": "employment",
             "kind": "ratio", "numerator_code": "employed",
             "denominator_code": "graduates", "target": 0.8, "unit": "%"},
            {"code": "teachers_trained", "name": "师资培养人数",
             "category": "teacher", "standard_caliber": "standard", "target": 30},
        ]:
            st, _ = self.c.call("POST", "/metrics", ADMIN, body)
            self.assertEqual(st, 201, body)

    def create_pending_rule(self, factor=0.5, parties=("org-a", "org-b")):
        body = {"rule_id": "r-local", "source_caliber": "local",
                "target_caliber": "standard", "country": "DE",
                "transform": {"type": "factor", "factor": factor},
                "effective_date": "2020-01-01",
                "required_parties": list(parties)}
        st, resp = self.c.call("POST", "/rules", ADMIN, body)
        self.assertEqual(st, 201, resp)
        return resp["rule"]["version_no"]


class LateDataAndReportTests(ApiTestBase):
    def test_late_data_creates_new_version_and_diff(self):
        self.create_catalog()
        self.create_pending_rule()
        # 两会签方通过
        st, _ = self.c.call("POST", "/rules/r-local/versions/1/approve", A, {})
        self.assertEqual(st, 200)
        st, so = self.c.call("POST", "/rules/r-local/versions/1/approve", B, {})
        self.assertEqual(st, 200)
        self.assertEqual(so["state"], "approved")

        # 导入首批数据
        st, batch = self.c.call(
            "POST", f"/projects/{PID}/batches", A,
            {"idempotency_key": "batch-1", "rows": enrollment_rows()})
        self.assertEqual(st, 201)
        self.assertEqual(batch["late_versions"], 0)

        # 首次计算
        st, task = self.c.call(
            "POST", f"/projects/{PID}/tasks", CALC,
            {"idempotency_key": "calc-1", "window_start": W0, "window_end": W1})
        self.assertEqual(st, 201)
        st, task = self.c.call("POST", f"/tasks/{task['id']}/run", CALC)
        self.assertEqual(st, 200)
        self.assertEqual(task["status"], "completed")
        r1_id = task["report_id"]
        st, r1 = self.c.call("GET", f"/reports/{r1_id}", CALC)
        # DE 100*0.5 + CN 200 = 250
        row = next(r for r in r1["rows"] if r["metric_code"] == "enrollment")
        self.assertEqual(row["value"], 250)
        self.assertEqual(row["metric_version"], 1)
        fp1 = r1["fingerprint"]

        # 迟到数据（同一逻辑键 DE）：新版本，不覆盖
        st, batch2 = self.c.call(
            "POST", f"/projects/{PID}/batches", B,
            {"idempotency_key": "batch-2", "rows": [
                {"metric_code": "enrollment", "country": "DE", "caliber": "local",
                 "period_start": "2025-09-01", "period_end": "2026-08-31",
                 "value": 120, "evidence": {"source": "registry",
                                            "locator": "DE-REG-1-CORRECTED"}}]})
        self.assertEqual(st, 201)
        self.assertEqual(batch2["late_versions"], 1)
        st, points = self.c.call(
            "GET", f"/projects/{PID}/points", A, query={"metric": "enrollment"})
        self.assertEqual(len(points), 3)  # v1+v2(DE) 与 CN

        # 重新计算 -> 报告序列新版本
        st, task2 = self.c.call(
            "POST", f"/projects/{PID}/tasks", CALC,
            {"idempotency_key": "calc-2", "window_start": W0, "window_end": W1})
        st, task2 = self.c.call("POST", f"/tasks/{task2['id']}/run", CALC)
        self.assertEqual(st, 200)
        r2_id = task2["report_id"]
        self.assertNotEqual(r1_id, r2_id)
        st, reports = self.c.call(
            "GET", f"/projects/{PID}/reports", CALC,
            query={"window_start": W0, "window_end": W1})
        self.assertEqual(len(reports), 2)

        # 旧报告不变
        st, old = self.c.call("GET", f"/reports/{r1_id}", CALC)
        self.assertEqual(old["fingerprint"], fp1)
        row_old = next(r for r in old["rows"] if r["metric_code"] == "enrollment")
        self.assertEqual(row_old["value"], 250)
        # 新报告体现迟到数据：120*0.5+200=260
        st, r2 = self.c.call("GET", f"/reports/{r2_id}", CALC)
        row_new = next(r for r in r2["rows"] if r["metric_code"] == "enrollment")
        self.assertEqual(row_new["value"], 260)

        # 差异
        st, diff = self.c.call(
            "GET", f"/projects/{PID}/reports/diff", CALC,
            query={"window_start": W0, "window_end": W1})
        self.assertEqual(st, 200)
        change = next(c for c in diff["row_changes"]
                      if c["metric_code"] == "enrollment")
        self.assertEqual(change["delta"], 10)
        vc = diff["data_version_changes"][0]
        self.assertEqual((vc["old_version"], vc["new_version"]), (1, 2))

    def test_metric_definition_update_does_not_rewrite_old_report(self):
        self.create_catalog()
        self.create_pending_rule()
        for tok in (A, B):
            self.c.call("POST", "/rules/r-local/versions/1/approve", tok, {})
        self.c.call("POST", f"/projects/{PID}/batches", A,
                    {"idempotency_key": "b1", "rows": enrollment_rows()})
        st, task = self.c.call(
            "POST", f"/projects/{PID}/tasks", CALC,
            {"idempotency_key": "k1", "window_start": W0, "window_end": W1})
        st, task = self.c.call("POST", f"/tasks/{task['id']}/run", CALC)
        st, r1 = self.c.call("GET", f"/reports/{task['report_id']}", CALC)
        self.assertEqual(r1["metric_versions"]["enrollment"]["version_no"], 1)
        fp_before = r1["fingerprint"]

        # 更新指标定义（目标值、口径变化 -> 新版本 v2）
        st, mv = self.c.call("POST", "/metrics", ADMIN, {
            "code": "enrollment", "name": "招生人数(修订)", "category": "enrollment",
            "standard_caliber": "standard", "target": 999})
        self.assertEqual(st, 201)
        self.assertEqual(mv["version_no"], 2)
        st, old = self.c.call("GET", f"/reports/{r1['id']}", CALC)
        self.assertEqual(old["fingerprint"], fp_before)
        self.assertEqual(old["metric_versions"]["enrollment"]["target"], 200)
        self.assertEqual(old["metric_versions"]["enrollment"]["version_no"], 1)

    def test_export_payload_is_self_contained(self):
        self.create_catalog()
        self.create_pending_rule()
        for tok in (A, B):
            self.c.call("POST", "/rules/r-local/versions/1/approve", tok, {})
        self.c.call("POST", f"/projects/{PID}/batches", A,
                    {"idempotency_key": "b1", "rows": enrollment_rows()})
        st, task = self.c.call(
            "POST", f"/projects/{PID}/tasks", CALC,
            {"idempotency_key": "k1", "window_start": W0, "window_end": W1})
        st, task = self.c.call("POST", f"/tasks/{task['id']}/run", CALC)
        rid = task["report_id"]
        st, pkg = self.c.call("GET", f"/reports/{rid}/export", CALC)
        self.assertEqual(st, 200)
        self.assertIn("replay", pkg)
        self.assertTrue(pkg["replay"]["rule_versions"])
        self.assertTrue(pkg["replay"]["data_versions"][0]["evidence"])
        st, csv_text = self.c.call(
            "GET", f"/reports/{rid}/export", CALC, query={"format": "csv"})
        self.assertEqual(st, 200)
        self.assertIn("招生人数", csv_text)


class ConcurrentSignoffTests(ApiTestBase):
    def test_concurrent_approvals_activate_once(self):
        self.create_catalog()
        self.create_pending_rule()
        results = {}
        barrier = threading.Barrier(2)

        def approve(token, key):
            barrier.wait()
            app = Client(HttpApp(self.container))
            st, body = app.call(
                "POST", "/rules/r-local/versions/1/approve", token,
                {"comment": key})
            results[key] = (st, body)

        t1 = threading.Thread(target=approve, args=(A, "a"))
        t2 = threading.Thread(target=approve, args=(B, "b"))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertTrue(all(st == 200 for st, _ in results.values()))
        st, so = self.c.call("GET", "/rules/r-local/versions/1/signoff", ADMIN)
        self.assertEqual(so["state"], "approved")
        self.assertEqual(set(so["approvals"].keys()), {"org-a", "org-b"})
        st, rules = self.c.call("GET", "/rules", ADMIN)
        self.assertEqual(rules["r-local"][0]["status"], "active")

    def test_duplicate_and_non_party_approval(self):
        self.create_catalog()
        self.create_pending_rule()
        # 非必备方（复核中心，持 review/read 粒度，无 signoff）-> 403
        st, err = self.c.call(
            "POST", "/rules/r-local/versions/1/approve", AUDIT, {})
        self.assertEqual(st, 403)
        # 同机构重复同意幂等
        st, first = self.c.call(
            "POST", "/rules/r-local/versions/1/approve", A, {"comment": "1"})
        self.assertEqual(st, 200)
        st, second = self.c.call(
            "POST", "/rules/r-local/versions/1/approve", A, {"comment": "2"})
        self.assertEqual(st, 200)
        self.assertEqual(len(second["approvals"]), 1)
        self.assertEqual(second["approvals"]["org-a"]["comment"], "1")

    def test_reject_blocks_activation(self):
        self.create_catalog()
        self.create_pending_rule()
        st, so = self.c.call(
            "POST", "/rules/r-local/versions/1/reject", B, {"comment": "口径存疑"})
        self.assertEqual(st, 200)
        self.assertEqual(so["state"], "rejected")
        st, err = self.c.call(
            "POST", "/rules/r-local/versions/1/approve", A, {})
        self.assertEqual(st, 409)
        st, rules = self.c.call("GET", "/rules", ADMIN)
        self.assertEqual(rules["r-local"][0]["status"], "rejected")


class IdempotencyAndRecoveryTests(ApiTestBase):
    def test_task_and_batch_idempotency(self):
        self.create_catalog()
        self.create_pending_rule()
        for tok in (A, B):
            self.c.call("POST", "/rules/r-local/versions/1/approve", tok, {})
        body = {"idempotency_key": "dup-batch", "rows": enrollment_rows()}
        st, b1 = self.c.call("POST", f"/projects/{PID}/batches", A, body)
        st, b2 = self.c.call("POST", f"/projects/{PID}/batches", A, body)
        self.assertEqual(b1["id"], b2["id"])
        self.assertTrue(b2["replayed"])  # 第二次：幂等命中，返回原批次视图

        tbody = {"idempotency_key": "dup-task", "window_start": W0, "window_end": W1}
        st, t1 = self.c.call("POST", f"/projects/{PID}/tasks", CALC, tbody)
        st, t2 = self.c.call("POST", f"/projects/{PID}/tasks", CALC, tbody)
        self.assertEqual(t1["id"], t2["id"])
        self.c.call("POST", f"/tasks/{t1['id']}/run", CALC)
        # 已完成任务重复执行直接返回，不产生第二份报告
        st, t3 = self.c.call("POST", f"/tasks/{t1['id']}/run", CALC)
        self.assertEqual(t3["status"], "completed")
        st, reports = self.c.call(
            "GET", f"/projects/{PID}/reports", CALC,
            query={"window_start": W0, "window_end": W1})
        self.assertEqual(len(reports), 1)

    def test_checkpoint_resume(self):
        self.create_catalog()
        self.create_pending_rule()
        for tok in (A, B):
            self.c.call("POST", "/rules/r-local/versions/1/approve", tok, {})
        self.c.call("POST", f"/projects/{PID}/batches", A,
                    {"idempotency_key": "b1", "rows": enrollment_rows()})
        st, task = self.c.call(
            "POST", f"/projects/{PID}/tasks", CALC,
            {"idempotency_key": "k1", "window_start": W0, "window_end": W1})
        tid = task["id"]
        # 直接调服务层注入中断：完成 2 个指标后崩溃
        with self.container.uow() as db:
            with self.assertRaises(RuntimeError):
                self.container.calc.run_task(db, tid, fail_after=2)
        st, failed = self.c.call("GET", f"/tasks/{tid}", CALC)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(len(failed["checkpoint"]), 2)
        # 恢复执行：跳过检查点，完成全部指标
        st, done = self.c.call("POST", f"/tasks/{tid}/resume", CALC)
        self.assertEqual(st, 200)
        self.assertEqual(done["status"], "completed")
        st, report = self.c.call("GET", f"/reports/{done['report_id']}", CALC)
        codes = {r["metric_code"] for r in report["rows"]}
        self.assertEqual(codes,
                         {"enrollment", "graduates", "employed",
                          "employment_rate", "teachers_trained"})

    def test_concurrent_run_is_rejected(self):
        self.create_catalog()
        st, task = self.c.call(
            "POST", f"/projects/{PID}/tasks", CALC,
            {"idempotency_key": "k1", "window_start": W0, "window_end": W1})
        # 手动置为 running，模拟另一执行器占用
        with self.container.uow() as db:
            db.tasks[task["id"]].status = m.TASK_RUNNING
        st, err = self.c.call("POST", f"/tasks/{task['id']}/run", CALC)
        self.assertEqual(st, 409)


class RuleRollbackTests(ApiTestBase):
    def test_rollback_creates_new_active_version_without_history_rewrite(self):
        self.create_catalog()
        # v1：factor 0.5，无需会签即生效
        st, _ = self.c.call("POST", "/rules", ADMIN, {
            "rule_id": "r-local", "source_caliber": "local",
            "target_caliber": "standard", "country": "DE",
            "transform": {"type": "factor", "factor": 0.5},
            "effective_date": "2020-01-01", "required_parties": []})
        self.assertEqual(st, 201)
        # v2：factor 0.9（同样直接生效）
        st, v2 = self.c.call("POST", "/rules", ADMIN, {
            "rule_id": "r-local", "source_caliber": "local",
            "target_caliber": "standard", "country": "DE",
            "transform": {"type": "factor", "factor": 0.9},
            "effective_date": "2021-01-01", "required_parties": []})
        self.assertEqual(v2["rule"]["version_no"], 2)
        # 回滚到 v1 -> v3，factor 恢复 0.5
        st, v3 = self.c.call("POST", "/rules/r-local/rollback", ADMIN,
                             {"to_version": 1})
        self.assertEqual(st, 201)
        self.assertEqual(v3["version_no"], 3)
        self.assertEqual(v3["transform"]["factor"], 0.5)
        self.assertEqual(v3["status"], "active")
        self.assertIn("回滚自 v1", v3["note"])
        # 历史版本未被改写
        st, rules = self.c.call("GET", "/rules", ADMIN)
        factors = {v["version_no"]: v["transform"]["factor"]
                   for v in rules["r-local"]}
        self.assertEqual(factors, {1: 0.5, 2: 0.9, 3: 0.5})

    def test_rollback_changes_new_report_only(self):
        self.create_catalog()
        # v1 factor 0.5，v2 factor 0.9（当前生效）
        self.c.call("POST", "/rules", ADMIN, {
            "rule_id": "r-local", "source_caliber": "local",
            "target_caliber": "standard", "country": "DE",
            "transform": {"type": "factor", "factor": 0.5},
            "effective_date": "2020-01-01", "required_parties": []})
        self.c.call("POST", "/rules", ADMIN, {
            "rule_id": "r-local", "source_caliber": "local",
            "target_caliber": "standard", "country": "DE",
            "transform": {"type": "factor", "factor": 0.9},
            "effective_date": "2021-01-01", "required_parties": []})
        self.c.call("POST", f"/projects/{PID}/batches", A,
                    {"idempotency_key": "b1", "rows": enrollment_rows()})
        st, t1 = self.c.call(
            "POST", f"/projects/{PID}/tasks", CALC,
            {"idempotency_key": "k1", "window_start": W0, "window_end": W1})
        st, t1 = self.c.call("POST", f"/tasks/{t1['id']}/run", CALC)
        st, rep_before = self.c.call("GET", f"/reports/{t1['report_id']}", CALC)
        v_before = next(r for r in rep_before["rows"]
                        if r["metric_code"] == "enrollment")["value"]
        self.assertEqual(v_before, 290)  # 100*0.9 + 200
        fp_before = rep_before["fingerprint"]

        # 回滚到 factor 0.5 后重算
        st, _ = self.c.call("POST", "/rules/r-local/rollback", ADMIN,
                            {"to_version": 1})
        self.assertEqual(st, 201)
        st, t2 = self.c.call(
            "POST", f"/projects/{PID}/tasks", CALC,
            {"idempotency_key": "k2", "window_start": W0, "window_end": W1})
        st, t2 = self.c.call("POST", f"/tasks/{t2['id']}/run", CALC)
        st, rep_after = self.c.call("GET", f"/reports/{t2['report_id']}", CALC)
        v_after = next(r for r in rep_after["rows"]
                       if r["metric_code"] == "enrollment")["value"]
        self.assertEqual(v_after, 250)  # 100*0.5 + 200
        # 旧报告指纹与冻结规则版本均不变
        st, old = self.c.call("GET", f"/reports/{t1['report_id']}", CALC)
        self.assertEqual(old["fingerprint"], fp_before)
        self.assertEqual(
            old["rule_versions"][0]["transform"]["factor"], 0.9)

    def test_rollback_of_never_effective_version_rejected(self):
        self.create_catalog()
        vn = self.create_pending_rule(parties=("org-a", "org-b"))
        # 尚未会签（pending），不可回滚
        st, err = self.c.call("POST", "/rules/r-local/rollback", ADMIN,
                              {"to_version": vn})
        self.assertEqual(st, 400)
        self.assertIn("从未生效", err["message"])


class MissingValueApiTests(ApiTestBase):
    def test_missing_fail_makes_report_indeterminate(self):
        # enrollment 使用 fail 策略
        st, _ = self.c.call("POST", "/metrics", ADMIN, {
            "code": "enrollment", "name": "招生人数", "category": "enrollment",
            "missing_policy": "fail", "target": 100})
        self.assertEqual(st, 201)
        st, _ = self.c.call("POST", f"/projects/{PID}/batches", A,
                            {"idempotency_key": "b1",
                             "rows": enrollment_rows(missing=True)})
        self.assertEqual(st, 201)
        st, task = self.c.call(
            "POST", f"/projects/{PID}/tasks", CALC,
            {"idempotency_key": "k1", "window_start": W0, "window_end": W1})
        st, task = self.c.call("POST", f"/tasks/{task['id']}/run", CALC)
        self.assertEqual(task["status"], "completed")  # 缺数据不等于任务失败
        st, report = self.c.call("GET", f"/reports/{task['report_id']}", CALC)
        row = next(r for r in report["rows"] if r["metric_code"] == "enrollment")
        self.assertEqual(row["verdict"], "indeterminate")
        self.assertEqual(report["conclusion"]["overall"], "not_passed")


class ReviewTests(ApiTestBase):
    def _report(self):
        self.create_catalog()
        self.create_pending_rule()
        for tok in (A, B):
            self.c.call("POST", "/rules/r-local/versions/1/approve", tok, {})
        self.c.call("POST", f"/projects/{PID}/batches", A,
                    {"idempotency_key": "b1", "rows": enrollment_rows()})
        st, task = self.c.call(
            "POST", f"/projects/{PID}/tasks", CALC,
            {"idempotency_key": "k1", "window_start": W0, "window_end": W1})
        st, task = self.c.call("POST", f"/tasks/{task['id']}/run", CALC)
        return task["report_id"]

    def test_review_flow(self):
        rid = self._report()
        st, rec = self.c.call("POST", f"/reports/{rid}/reviews", AUDIT,
                              {"state": "approved", "comment": "可复算"})
        self.assertEqual(st, 201)
        st, status = self.c.call("GET", f"/reports/{rid}/reviews", AUDIT)
        self.assertEqual(status["overall"], "approved")
        # 已通过不可改判
        st, err = self.c.call("POST", f"/reports/{rid}/reviews", AUDIT,
                              {"state": "rejected"})
        self.assertEqual(st, 409)

    def test_import_org_cannot_review(self):
        rid = self._report()
        st, err = self.c.call("POST", f"/reports/{rid}/reviews", A,
                              {"state": "approved"})
        self.assertEqual(st, 403)


class AuthorizationTests(ApiTestBase):
    def test_granular_scopes(self):
        # 无令牌
        st, _ = self.c.call("GET", f"/projects/{PID}")
        self.assertEqual(st, 401)
        # 复核机构无导入权限
        st, _ = self.c.call("POST", f"/projects/{PID}/batches", AUDIT,
                            {"rows": []})
        self.assertEqual(st, 403)
        # 计算主体无会签权限
        st, _ = self.c.call(
            "POST", "/rules/x/versions/1/approve", CALC, {})
        self.assertEqual(st, 403)
        # 导入机构的项目粒度：未授权项目被拒
        st, _ = self.c.call("POST", "/projects/prj-other/batches", A,
                            {"rows": []})
        self.assertEqual(st, 403)
        # 管理员可以登记指标，普通机构不行
        st, _ = self.c.call("POST", "/metrics", A, {"code": "x"})
        self.assertEqual(st, 403)
        st, _ = self.c.call("GET", f"/projects/{PID}", A)
        self.assertEqual(st, 200)


class SnapshotRecoveryTests(unittest.TestCase):
    def test_state_and_checkpoint_survive_restart(self):
        tmpdir = tempfile.mkdtemp()
        snap = os.path.join(tmpdir, "nested", "snapshot.json")

        container, http, clock = make_app(snapshot_path=snap)
        c = Client(HttpApp(container))
        # 建指标与规则（直接 HTTP）
        c.call("POST", "/metrics", ADMIN,
               {"code": "enrollment", "category": "enrollment", "target": 1})
        c.call("POST", f"/projects/{PID}/batches", A,
               {"idempotency_key": "b1", "rows": enrollment_rows()})
        st, task = c.call("POST", f"/projects/{PID}/tasks", CALC,
                          {"idempotency_key": "k1", "window_start": W0,
                           "window_end": W1})
        tid = task["id"]
        # 中断在检查点上
        with container.uow() as db:
            try:
                container.calc.run_task(db, tid, fail_after=1)
            except RuntimeError:
                pass
        self.assertTrue(os.path.exists(snap))

        # 新进程：从快照恢复
        from service_09252_010.bootstrap import build_container
        from service_09252_010.interfaces.http import HttpApp as HA
        new_clock = FixedClock()
        c2_db = build_container(snapshot_path=snap, clock=new_clock, seed=False)
        c2 = Client(HA(c2_db))
        st, failed = c2.call("GET", f"/tasks/{tid}", CALC)
        self.assertEqual(st, 200)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(len(failed["checkpoint"]), 1)
        st, done = c2.call("POST", f"/tasks/{tid}/resume", CALC)
        self.assertEqual(done["status"], "completed")
        # 幂等索引也持久化：重复键返回原任务
        st, again = c2.call("POST", f"/projects/{PID}/tasks", CALC,
                            {"idempotency_key": "k1", "window_start": W0,
                             "window_end": W1})
        self.assertEqual(again["id"], tid)


if __name__ == "__main__":
    unittest.main()
