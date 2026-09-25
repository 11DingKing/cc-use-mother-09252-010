"""测试支撑：可控时钟与 WSGI 调用助手。"""
from __future__ import annotations

import io
import json
from typing import Any
from urllib.parse import urlencode

from service_09252_010.application.ports import Clock
from service_09252_010.bootstrap import build_container
from service_09252_010.interfaces.http import HttpApp


class FixedClock(Clock):
    def __init__(self, start: str = "2026-01-01T00:00:00Z") -> None:
        self.current = start
        self.ticks = 0

    def now(self) -> str:
        return self.current

    def advance(self, ts: str) -> None:
        self.current = ts
        self.ticks += 1


def make_app(snapshot_path: str | None = None):
    clock = FixedClock()
    container = build_container(snapshot_path=snapshot_path, clock=clock, seed=True)
    return container, Client(HttpApp(container)), clock


class Client:
    def __init__(self, app) -> None:
        self.app = app

    def call(self, method: str, path: str, token: str | None = None,
             body: Any = None, query: dict | None = None) -> tuple[int, Any, dict]:
        if query:
            path = path + "?" + urlencode(query)
        payload = b""
        headers = {}
        if body is not None:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["CONTENT_TYPE"] = "application/json"
        if token:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path.split("?", 1)[0],
            "QUERY_STRING": urlencode(query or {}),
            "CONTENT_LENGTH": str(len(payload)),
            "wsgi.input": io.BytesIO(payload),
            "wsgi.errors": io.StringIO(),
            **headers,
        }
        result: dict = {}

        def start_response(status, response_headers, exc_info=None):
            result["status"] = int(status.split()[0])
            result["headers"] = dict(response_headers)

        raw = b"".join(self.app(environ, start_response))
        ctype = result["headers"].get("Content-Type", "")
        if "json" in ctype:
            return result["status"], json.loads(raw.decode("utf-8"))
        return result["status"], raw.decode("utf-8")
