"""python -m service_09252_010 [--host 127.0.0.1] [--port 8080] [--snapshot PATH]"""
from __future__ import annotations

import argparse
from wsgiref.simple_server import make_server

from .bootstrap import build_container, default_snapshot_path
from .interfaces.http import HttpApp


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--snapshot", default=default_snapshot_path())
    args = parser.parse_args()

    container = build_container(snapshot_path=args.snapshot)
    app = HttpApp(container)
    print(f"国际职教合作成效核算服务启动: http://{args.host}:{args.port}")
    print(f"快照文件: {args.snapshot}")
    with make_server(args.host, args.port, app) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
