"""WSGI HTTP 接口边界。仅标准库，可被 wsgiref 或任意 WSGI 容器承载。"""
from __future__ import annotations

import json
import re
from typing import Any, Callable
from urllib.parse import parse_qs

from ..bootstrap import Container
from ..domain import models as m
from ..domain.errors import DomainError

Handler = Callable[..., Any]


class HttpApp:
    def __init__(self, container: Container) -> None:
        self.c = container
        self.routes: list[tuple[str, re.Pattern[str], str | None, Handler]] = []
        self._register()

    def route(self, method: str, pattern: str, scope: str | None = None) -> Callable:
        rx = re.compile("^" + pattern + "$")

        def deco(fn: Handler) -> Handler:
            self.routes.append((method, rx, scope, fn))
            return fn
        return deco

    # ---- WSGI 入口 --------------------------------------------------------

    def __call__(self, environ: dict, start_response) -> list[bytes]:
        path = environ.get("PATH_INFO", "/")
        method = environ.get("REQUEST_METHOD", "GET")
        try:
            body = self._read_body(environ)
            for meth, rx, scope, fn in self.routes:
                if meth != method:
                    continue
                match = rx.match(path)
                if not match:
                    continue
                principal = None
                if scope is not None:
                    with self.c.uow() as db:
                        principal = self.c.access.authenticate(
                            _bearer(environ))
                        # 外层门禁：持有该粒度即可；项目级粒度在处理器内
                        # 按资源所属项目二次校验（/projects/{id} 或资源反查）
                        self.c.access.require_scope(principal, scope)
                status, payload, headers = fn(self, body,
                                              parse_qs(environ.get("QUERY_STRING", "")),
                                              principal, match.groupdict(), environ)
                return self._respond(start_response, status, payload, headers)
            return self._respond(start_response, 404,
                                 {"error": "not_found", "path": path})
        except DomainError as e:
            return self._respond(start_response, e.status,
                                 {"error": e.code, "message": str(e)})
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            return self._respond(start_response, 400,
                                 {"error": "bad_request", "message": str(e)})

    def _read_body(self, environ: dict) -> dict[str, Any]:
        length = int(environ.get("CONTENT_LENGTH") or 0)
        if not length:
            return {}
        raw = environ["wsgi.input"].read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _respond(self, start_response, status: int, payload: Any,
                 headers: dict | None = None) -> list[bytes]:
        headers = dict(headers or {})
        if isinstance(payload, str):
            out = payload.encode("utf-8")
            headers.setdefault("Content-Type", "text/csv; charset=utf-8")
        else:
            out = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            headers.setdefault("Content-Type", "application/json; charset=utf-8")
        headers.setdefault("Cache-Control", "no-store")
        start_response(f"{status} {_STATUS.get(status, 'OK')}",
                       [(k, v) for k, v in headers.items()])
        return [out]

    # ---- 路由注册 ---------------------------------------------------------

    def _register(self) -> None:
        r = self.route

        @r("GET", r"/health", None)
        def health(app, body, qs, p, args, env):
            return 200, {"status": "ok"}, None

        @r("GET", r"/projects/(?P<project_id>[\w-]+)", m.SCOPE_READ)
        def get_project(app, body, qs, p, args, env):
            with app.c.uow() as db:
                app.c.access.require(p, m.SCOPE_READ, args["project_id"])
                proj = db.projects.get(args["project_id"])
                if proj is None:
                    from ..domain.errors import NotFoundError
                    raise NotFoundError("项目不存在")
                return 200, proj.to_dict(), None

        # 指标定义
        @r("POST", r"/metrics", m.SCOPE_ADMIN)
        def create_metric(app, body, qs, p, args, env):
            with app.c.uow() as db:
                return 201, app.c.catalog.register_metric(db, p, body), None

        @r("GET", r"/metrics", m.SCOPE_READ)
        def list_metrics(app, body, qs, p, args, env):
            with app.c.uow() as db:
                code = qs.get("code", [None])[0]
                return 200, app.c.catalog.list_metrics(db, code), None

        @r("GET", r"/metrics/(?P<code>[\w.-]+)", m.SCOPE_READ)
        def get_metric(app, body, qs, p, args, env):
            with app.c.uow() as db:
                vn = qs.get("version", [None])[0]
                mv = app.c.catalog.get_metric_version(
                    db, args["code"], int(vn) if vn else None)
                return 200, mv.__dict__, None

        # 换算规则
        @r("POST", r"/rules", m.SCOPE_ADMIN)
        def create_rule(app, body, qs, p, args, env):
            with app.c.uow() as db:
                return 201, app.c.catalog.create_rule_version(db, p, body), None

        @r("GET", r"/rules", m.SCOPE_READ)
        def list_rules(app, body, qs, p, args, env):
            with app.c.uow() as db:
                return 200, app.c.catalog.list_rules(db), None

        @r("POST", r"/rules/(?P<rule_id>[\w.-]+)/versions/(?P<vn>\d+)/approve",
          m.SCOPE_SIGNOFF)
        def approve(app, body, qs, p, args, env):
            with app.c.uow() as db:
                return 200, app.c.catalog.signoff_approve(
                    db, p, args["rule_id"], int(args["vn"]),
                    body.get("comment", "")), None

        @r("POST", r"/rules/(?P<rule_id>[\w.-]+)/versions/(?P<vn>\d+)/reject",
          m.SCOPE_SIGNOFF)
        def reject(app, body, qs, p, args, env):
            with app.c.uow() as db:
                return 200, app.c.catalog.signoff_reject(
                    db, p, args["rule_id"], int(args["vn"]),
                    body.get("comment", "")), None

        @r("GET", r"/rules/(?P<rule_id>[\w.-]+)/versions/(?P<vn>\d+)/signoff",
          m.SCOPE_READ)
        def signoff_status(app, body, qs, p, args, env):
            with app.c.uow() as db:
                return 200, app.c.catalog.signoff_status(
                    db, args["rule_id"], int(args["vn"])), None

        @r("POST", r"/rules/(?P<rule_id>[\w.-]+)/rollback", m.SCOPE_ADMIN)
        def rollback(app, body, qs, p, args, env):
            with app.c.uow() as db:
                to_v = int(body["to_version"])
                return 201, app.c.catalog.rollback_rule(
                    db, p, args["rule_id"], to_v), None

        # 数据导入
        @r("POST", r"/projects/(?P<project_id>[\w-]+)/batches", m.SCOPE_IMPORT)
        def submit_batch(app, body, qs, p, args, env):
            with app.c.uow() as db:
                app.c.access.require(p, m.SCOPE_IMPORT, args["project_id"])
                return 201, app.c.importer.submit_batch(
                    db, p, args["project_id"], body), None

        @r("GET", r"/projects/(?P<project_id>[\w-]+)/batches", m.SCOPE_READ)
        def list_batches(app, body, qs, p, args, env):
            with app.c.uow() as db:
                app.c.access.require(p, m.SCOPE_READ, args["project_id"])
                return 200, app.c.importer.list_batches(db, args["project_id"]), None

        @r("GET", r"/projects/(?P<project_id>[\w-]+)/points", m.SCOPE_READ)
        def list_points(app, body, qs, p, args, env):
            with app.c.uow() as db:
                app.c.access.require(p, m.SCOPE_READ, args["project_id"])
                mc = qs.get("metric", [None])[0]
                return 200, app.c.importer.list_points(
                    db, args["project_id"], mc), None

        # 计算任务
        @r("POST", r"/projects/(?P<project_id>[\w-]+)/tasks", m.SCOPE_CALC)
        def create_task(app, body, qs, p, args, env):
            with app.c.uow() as db:
                app.c.access.require(p, m.SCOPE_CALC, args["project_id"])
                return 201, app.c.calc.create_task(
                    db, p, args["project_id"], body), None

        @r("GET", r"/tasks/(?P<task_id>[\w-]+)", m.SCOPE_READ)
        def get_task(app, body, qs, p, args, env):
            with app.c.uow() as db:
                task = app.c.calc.get_task(db, args["task_id"])
                app.c.access.require(p, m.SCOPE_READ, task.project_id)
                return 200, task.to_dict(), None

        @r("POST", r"/tasks/(?P<task_id>[\w-]+)/run", m.SCOPE_CALC)
        def run_task(app, body, qs, p, args, env):
            fa = qs.get("fail_after", [None])[0]
            with app.c.uow() as db:
                task = app.c.calc.get_task(db, args["task_id"])
                app.c.access.require(p, m.SCOPE_CALC, task.project_id)
                return 200, app.c.calc.run_task(
                    db, args["task_id"], int(fa) if fa else None), None

        @r("POST", r"/tasks/(?P<task_id>[\w-]+)/resume", m.SCOPE_CALC)
        def resume_task(app, body, qs, p, args, env):
            with app.c.uow() as db:
                task = app.c.calc.get_task(db, args["task_id"])
                app.c.access.require(p, m.SCOPE_CALC, task.project_id)
                return 200, app.c.calc.run_task(
                    db, args["task_id"], resume=True), None

        # 报告
        @r("GET", r"/projects/(?P<project_id>[\w-]+)/reports", m.SCOPE_READ)
        def list_reports(app, body, qs, p, args, env):
            with app.c.uow() as db:
                app.c.access.require(p, m.SCOPE_READ, args["project_id"])
                return 200, app.c.calc.list_reports(
                    db, args["project_id"],
                    qs.get("window_start", [None])[0],
                    qs.get("window_end", [None])[0]), None

        @r("GET", r"/projects/(?P<project_id>[\w-]+)/reports/diff", m.SCOPE_READ)
        def report_diff(app, body, qs, p, args, env):
            with app.c.uow() as db:
                app.c.access.require(p, m.SCOPE_READ, args["project_id"])
                return 200, app.c.calc.report_diff(
                    db, args["project_id"],
                    qs["window_start"][0], qs["window_end"][0]), None

        @r("GET", r"/reports/(?P<report_id>[\w-]+)", m.SCOPE_READ)
        def get_report(app, body, qs, p, args, env):
            with app.c.uow() as db:
                report = app.c.calc.get_report(db, args["report_id"])
                app.c.access.require(p, m.SCOPE_READ, report.project_id)
                return 200, report.to_dict(), None

        # 复核
        @r("POST", r"/reports/(?P<report_id>[\w-]+)/reviews", m.SCOPE_REVIEW)
        def submit_review(app, body, qs, p, args, env):
            with app.c.uow() as db:
                report = app.c.calc.get_report(db, args["report_id"])
                app.c.access.require(p, m.SCOPE_REVIEW, report.project_id)
                return 201, app.c.review.submit(
                    db, p, args["report_id"], body), None

        @r("GET", r"/reports/(?P<report_id>[\w-]+)/reviews", m.SCOPE_READ)
        def review_status(app, body, qs, p, args, env):
            with app.c.uow() as db:
                return 200, app.c.review.status(db, args["report_id"]), None

        # 导出
        @r("GET", r"/reports/(?P<report_id>[\w-]+)/export", m.SCOPE_READ)
        def export_report(app, body, qs, p, args, env):
            fmt = qs.get("format", ["json"])[0]
            with app.c.uow() as db:
                report = app.c.calc.get_report(db, args["report_id"])
                app.c.access.require(p, m.SCOPE_READ, report.project_id)
                if fmt == "csv":
                    return 200, app.c.export.export_csv(
                        db, args["report_id"]), None
                return 200, app.c.export.export_json(
                    db, args["report_id"]), None


def _bearer(environ: dict) -> str | None:
    auth = environ.get("HTTP_AUTHORIZATION", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None


_STATUS = {200: "OK", 201: "Created", 400: "Bad Request", 401: "Unauthorized",
           403: "Forbidden", 404: "Not Found", 409: "Conflict",
           500: "Internal Server Error"}
