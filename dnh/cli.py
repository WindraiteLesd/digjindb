"""``dnh`` 命令行入口(公开站点版).

本仓库是 DDDB 的**线上云端部分**: 对外只读站点 + 编辑端。
采集/入库/站长端面板与部署脚本不在本仓库内。

用法::

    python -m dnh public --root . --host 127.0.0.1 --port 8940

    # 常用参数
    --users-db        用户库路径(默认 <root>/data/public_users.sqlite3)
    --sessions-db     会话库路径(默认 <root>/data/sessions.sqlite3)
    --no-session-persist  会话只放内存(重启掉线, 仅调试)
    --base-url        站点对外地址(写进 robots.txt / sitemap.xml)
    --db              专辑库路径(默认 <root>/data/dnh.sqlite3)
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__


def out(text: str) -> None:
    """打印一行(容忍 Windows 控制台编码)."""
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((text + "\n").encode("utf-8", "replace"))
    sys.stdout.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dnh",
        description="DDDB 公开站点(线上云端部分)",
    )
    parser.add_argument("--version", action="version",
                        version="dnh {}".format(__version__))
    sub = parser.add_subparsers(dest="command")

    p_public = sub.add_parser(
        "public", help="启动面向公众的站点(浏览/搜索/详情/看板)")
    p_public.add_argument("--host", default="127.0.0.1")
    p_public.add_argument("--port", type=int, default=8940)
    p_public.add_argument("--root", default=None,
                          help="站点根目录(含 dnh/ 与 data/)")
    p_public.add_argument("--db", default=None,
                          help="专辑库路径(默认 <root>/data/dnh.sqlite3)")
    p_public.add_argument("--users-db", default=None,
                          help="用户库路径(默认 <root>/data/public_users.sqlite3)")
    p_public.add_argument("--sessions-db", default=None,
                          help="会话库路径(默认 <root>/data/sessions.sqlite3)")
    p_public.add_argument("--no-session-persist", action="store_true",
                          help="会话只放内存(重启掉线; 仅临时调试用)")
    p_public.add_argument("--base-url", default="",
                          help="站点对外地址(写进 robots/sitemap), 如 https://example.com")
    p_public.add_argument("--covers-dir", default=None,
                          help="封面目录(默认 <root>/data/covers)")
    return parser


def cmd_public(args: argparse.Namespace) -> int:
    """启动公开站点."""
    from .public.site import build_site, make_server

    root = Path(args.root).resolve() if args.root else Path.cwd()
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)

    db_path = Path(args.db) if args.db else data / "dnh.sqlite3"
    users_db = Path(args.users_db) if args.users_db else data / "public_users.sqlite3"
    sessions_db = ("" if args.no_session_persist
                   else (Path(args.sessions_db) if args.sessions_db
                         else data / "sessions.sqlite3"))
    covers_dir = Path(args.covers_dir) if args.covers_dir else data / "covers"

    site = build_site(
        None,
        db_path=db_path,
        root=root,
        covers_dir=covers_dir,
        users_db=users_db,
        sessions_db=sessions_db or None,
        base_url=args.base_url or None,
    )
    httpd = make_server(site, host=args.host, port=args.port)
    host, port = httpd.server_address[0], httpd.server_address[1]
    out("公开站点已启动: http://{}:{}/".format(host, port))
    out("  专辑库: {}".format(db_path))
    out("  用户库: {}".format(users_db))
    out("  会话:   {}".format(
        str(site.sessions.store.path) if site.sessions.persistent
        else "内存(重启会掉线)"))
    out("  限速:   {}".format("开启" if site.limiter.enabled else "关闭"))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        out("已停止")
    finally:
        httpd.server_close()
        site.close()
    return 0


def main(argv: "list[str] | None" = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    if args.command == "public":
        return cmd_public(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
