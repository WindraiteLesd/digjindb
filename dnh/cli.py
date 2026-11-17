"""命令行入口.

用法::

    python -m dnh --help
    python -m dnh init
    python -m dnh doctor
    python -m dnh events list --year 2026
    python -m dnh discover --event M3-2026秋
    python -m dnh run --event M3-2026秋 --auto
    python -m dnh serve
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sqlite3
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, Sequence

from . import __version__, events as events_mod, report as report_mod
from .config import DEFAULT_CONFIG_RELPATH, Config, write_example_config
from .errors import DnhError, NeteaseApiError
from .pipeline import Pipeline

LOG = logging.getLogger("dnh")

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


# --------------------------------------------------------------------------
# 输出工具
# --------------------------------------------------------------------------


def _setup_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _setup_logging(level: str, *, quiet: bool = False) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root = logging.getLogger("dnh")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.ERROR if quiet else _LEVELS.get(level.lower(), logging.INFO))
    logging.getLogger("dnh").propagate = False


def out(message: str = "") -> None:
    print(message)


def out_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024:
            return f"{num:.1f}{unit}"
        num /= 1024
    return f"{num:.1f}TB"


# --------------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dnh",
        description="同人音乐自动检索 + 网易云匹配 + 标记管理 (doujindigdb)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  dnh init\n"
            "  dnh doctor\n"
            "  dnh events list --year 2026\n"
            "  dnh discover --event M3-2026秋 --sources doujinstyle,x,bangumi\n"
            "  dnh run --event M3-2026秋\n"
        ),
    )
    parser.add_argument("--config", "-c", help="配置文件路径 (默认 config/config.toml)")
    parser.add_argument("--root", help="工作根目录 (默认当前目录)")
    parser.add_argument("--log-level", default=None, choices=sorted(_LEVELS), help="日志级别")
    parser.add_argument("--quiet", "-q", action="store_true", help="只输出错误")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--version", action="version", version=f"doujindigdb {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    # init
    p_init = sub.add_parser("init", help="初始化配置与目录")
    p_init.add_argument("--force", action="store_true", help="覆盖已存在的配置文件")

    # doctor
    sub.add_parser("doctor", help="环境自检(代理/数据源/网易云连通性)")

    # bilibili: 搜 B 站搬运的试听视频, 给专辑补链接
    p_bili = sub.add_parser("bilibili", help="搜索 B 站搬运的试听视频并补进专辑链接")
    p_bili.add_argument("--event", default="", help="只处理某个展会")
    p_bili.add_argument("--limit", type=int, default=0, help="最多处理多少张(0=不限)")
    p_bili.add_argument("--budget", type=int, default=40,
                        help="本轮最多查几张(默认 40, 接口有风控别开太大)")
    p_bili.add_argument("--dry-run", action="store_true", help="只打印不写库")

    # covers: 检查被多张专辑共用的封面(基本是抓错了)
    p_covers = sub.add_parser("covers", help="检查被多张专辑共用的封面(基本是抓错了)")
    p_covers.add_argument("--event", default="", help="只查某个展会")
    p_covers.add_argument("--min-count", type=int, default=2, help="共用几张起算(默认 2)")
    p_covers.add_argument("--clear", action="store_true",
                          help="清掉这些封面(之后 enrich 会重新找)")
    p_covers.add_argument("--from-netease", action="store_true",
                          help="给已匹配到网易云但没封面的专辑补封面(用其专辑图)")
    p_covers.add_argument("--from-shop", action="store_true",
                          help="从通贩页面(BOOTH/Melonbooks…)取商品图当封面")
    p_covers.add_argument("--suspect", action="store_true",
                          help="列出封面与社团主页同域的可疑封面(历史 bug 的产物)")
    p_covers.add_argument("--broken", action="store_true",
                          help="检查加载不出的封面(本地文件丢失 / 上游返回非图片 / 4xx)")
    p_covers.add_argument("--budget", type=int, default=80,
                          help="--broken 最多联网检查多少张(默认 80, 避免跑太久)")
    p_covers.add_argument("--cache-remote", action="store_true",
                          help="把外链封面(如网易云 p*.music.126.net)下载缓存到 data/covers/")
    p_covers.add_argument("--limit", type=int, default=0,
                          help="--cache-remote 本次最多下载几张(默认 0 = 不限)")
    p_covers.add_argument("--dry-run", action="store_true",
                          help="--cache-remote 只统计待缓存数量, 不下载不写库")

    # events
    p_events = sub.add_parser("events", help="展会与话题标签")
    ev_sub = p_events.add_subparsers(dest="events_command", metavar="<子命令>")
    p_ev_list = ev_sub.add_parser("list", help="列出展会")
    p_ev_list.add_argument("--year", type=int, default=0)
    p_ev_list.add_argument("--kinds", default="m3,comiket", help="逗号分隔: m3,comiket")
    p_ev_tags = ev_sub.add_parser("tags", help="打印某个展会的全部 X 话题标签")
    p_ev_tags.add_argument("event", help="如 M3-2026秋 / C107")
    p_ev_tags.add_argument("--format", default=None, choices=["text", "hash", "json"],
                           help="输出格式; 省略时跟随全局 --json")

    # sources
    p_src = sub.add_parser("sources", help="数据源")
    src_sub = p_src.add_subparsers(dest="sources_command", metavar="<子命令>")
    src_sub.add_parser("list", help="列出数据源与可用状态")

    # discover
    p_disc = sub.add_parser("discover", help="采集专辑线索")
    _add_event_args(p_disc)
    p_disc.add_argument("--sources", help="逗号分隔的数据源, 默认全部启用的")
    p_disc.add_argument("--limit", type=int, default=80, help="每个源每个事件的上限")
    p_disc.add_argument("--query", default="", help="额外按关键词检索")
    p_disc.add_argument("--covers", action="store_true", help="同时下载封面")

    # enrich
    p_enrich = sub.add_parser("enrich", help="用其它站点补齐曲目表/封面/社团")
    p_enrich.add_argument("--event", default="")
    p_enrich.add_argument("--limit", type=int, default=150)
    p_enrich.add_argument("--sources", help="参与富化的源(默认除 x 外全部)")
    p_enrich.add_argument("--no-covers", action="store_true")
    p_enrich.add_argument("--bilibili", action="store_true",
                          help="顺带搜 B 站搬运的试听视频(有风控, 默认关)")

    # match
    p_match = sub.add_parser("match", help="在网易云检索并打分")
    p_match.add_argument("--event", default="")
    p_match.add_argument("--tag", default="")
    p_match.add_argument("--limit", type=int, default=300)
    p_match.add_argument("--min-score", type=float, default=None)
    p_match.add_argument("--force", action="store_true", help="忽略已有匹配重新匹配")
    p_match.add_argument("--show", type=int, default=30, help="显示前 N 条结果")

    # run
    p_run = sub.add_parser("run", help="一站式: 采集 → 富化 → 匹配")
    _add_event_args(p_run)
    p_run.add_argument("--sources")
    p_run.add_argument("--limit", type=int, default=80)
    p_run.add_argument("--query", default="")
    p_run.add_argument("--no-enrich", action="store_true")
    p_run.add_argument("--no-match", action="store_true")
    p_run.add_argument("--no-covers", action="store_true")

    # playlist
    p_pl = sub.add_parser("playlist", help="用网易云歌单链接反查本地库(只读)")
    pl_sub = p_pl.add_subparsers(dest="playlist_command", metavar="<子命令>")
    p_pl_cmp = pl_sub.add_parser("compare", help="把歌单曲目和本地数据库对照")
    p_pl_cmp.add_argument("--url", required=True,
                          help="网易云歌单链接或歌单 id, 例如 https://music.163.com/#/playlist?id=123456")
    p_pl_cmp.add_argument("--event", default="", help="只对照该展会下的本地专辑")
    p_pl_cmp.add_argument("--tag", default="")
    p_pl_cmp.add_argument("--limit", type=int, default=None, help="本地专辑取样上限")
    p_pl_cmp.add_argument("--show", type=int, default=25, help="每类最多列几条")
    p_pl_cmp.add_argument("--mark", default="", help="把命中的本地专辑打上该标记(如「已加入歌单」)")
    p_pl_cmp.add_argument("--match-filter", default="matched",
                        choices=["matched", "unmatched", "all"],
                        help="参与对照的专辑范围(默认只对照已匹配网易云的)")
    p_pl_cmp.add_argument("--no-terminal", action="store_true")

    # report
    p_rep = sub.add_parser("report", help="生成 Markdown / HTML / JSON 报告")
    p_rep.add_argument("--event", default="")
    p_rep.add_argument("--tag", default="")
    p_rep.add_argument("--group-by", default="event", choices=sorted(report_mod.GROUP_LABELS))
    p_rep.add_argument("--format", default="md,html,json")
    p_rep.add_argument("--title", default="")
    p_rep.add_argument("--limit", type=int, default=None)
    p_rep.add_argument("--open", action="store_true", help="生成后打开 HTML")

    # robots: 抓取合规检查(只读, 不发实际采集请求)
    p_robots = sub.add_parser(
        "robots", help="查看某个 URL / 各数据源的 robots.txt 判定(只读检查)")
    p_robots.add_argument("url", nargs="?", default="", help="要检查的 URL, 如 https://bgm.tv/subject/1")
    p_robots.add_argument("--audit", action="store_true",
                          help="汇总各数据源的 host + 最小请求间隔 + robots 状态")
    p_robots.add_argument("--refresh", action="store_true", help="忽略缓存重新取 robots.txt")
    p_robots.add_argument("--offline", action="store_true",
                          help="不联网, 只用已有缓存(取不到就标 unknown)")

    # db
    p_db = sub.add_parser("db", help="数据库操作")
    db_sub = p_db.add_subparsers(dest="db_command", metavar="<子命令>")
    db_sub.add_parser("stats", help="统计信息")
    p_db_backup = db_sub.add_parser(
        "backup", help="用 sqlite3 的 .backup API 安全备份(WAL 下直接复制文件不安全)")
    p_db_backup.add_argument("--out", default="",
                             help="输出文件(默认 data/backups/dnh-<时间戳>.sqlite3)")
    p_db_backup.add_argument("--force", action="store_true", help="允许覆盖已存在的文件")
    p_db_albums = db_sub.add_parser("albums", help="列出专辑")
    p_db_albums.add_argument("--event", default="")
    p_db_albums.add_argument("--tag", default="")
    p_db_albums.add_argument("--query", default="")
    p_db_albums.add_argument("--limit", type=int, default=50)

    p_netease = sub.add_parser("netease", help="网易云检索(只读, 帮手动匹配找 id)")
    netease_sub = p_netease.add_subparsers(dest="netease_command", metavar="<子命令>")
    p_netease_search = netease_sub.add_parser(
        "search", help="按关键词搜网易云专辑, 打印 id 供 dnh db match --netease 使用")
    p_netease_search.add_argument("keywords", nargs="+", help="关键词(标题/社团)")
    p_netease_search.add_argument("--limit", type=int, default=10, help="最多几条(默认 10)")

    p_db_reset = db_sub.add_parser("reset", help="清空数据")
    p_db_reset.add_argument("--yes", action="store_true")
    p_db_reset.add_argument("--keep-matches", action="store_true")

    p_db_desc = db_sub.add_parser(
        "desc", help="给简介为空的专辑补简介(**只取条目简介, 不取收藏短评**)")
    p_db_desc.add_argument("--source", default="bangumi", help="目前支持 bangumi")
    p_db_desc.add_argument("--event", default="")
    p_db_desc.add_argument("--limit", type=int, default=0)
    p_db_desc.add_argument("--dry-run", action="store_true")
    p_db_desc.add_argument("--overwrite", action="store_true", help="连已有简介一起覆盖")

    p_db_reparse = db_sub.add_parser(
        "reparse", help="用已存的源 ID 重新抓页面并**覆盖**元数据(清洗解析 bug 造成的脏数据)")
    p_db_reparse.add_argument("--source", default="vgmdb", help="目前支持 vgmdb")
    p_db_reparse.add_argument("--event", default="", help="只处理该展会")
    p_db_reparse.add_argument("--limit", type=int, default=0, help="最多处理多少张(0=全部)")
    p_db_reparse.add_argument("--dry-run", action="store_true", help="只列出要处理的专辑")

    p_db_prune = db_sub.add_parser("prune", help="清理发行日与所标展会不符的专辑")
    p_db_prune.add_argument("--drop", action="store_true", help="直接删除; 不加则只摘掉展会标签")
    p_db_prune.add_argument("--dry-run", action="store_true", help="只看会动哪些, 不实际改")
    p_db_prune.add_argument("--before-days", type=int, default=None, help="展会前多少天内算匹配")
    p_db_prune.add_argument("--after-days", type=int, default=None, help="展会后多少天内算匹配")

    p_db_delete = db_sub.add_parser("delete", help="按条件删除专辑")
    p_db_delete.add_argument("--key", action="append", default=[], help="专辑 key(可重复)")
    p_db_delete.add_argument("--event", default="")
    p_db_delete.add_argument("--tag", default="")
    p_db_delete.add_argument("--source", default="")
    p_db_delete.add_argument("--query", default="")
    p_db_delete.add_argument("--mark", default="")
    p_db_delete.add_argument("--unmarked", action="store_true", help="只删没有任何标记的")
    p_db_delete.add_argument("--unmatched", action="store_true", help="只删没有网易云匹配的")
    p_db_delete.add_argument("--dry-run", action="store_true")
    p_db_delete.add_argument("--yes", action="store_true")

    p_db_unmatch = db_sub.add_parser(
        "unmatch",
        help="取消网易云匹配并**记住不再自动匹配**(删匹配记录 + 写人工拒绝, 不动专辑)")
    p_db_unmatch.add_argument("keys", nargs="+", help="专辑 key(可多个)")
    p_db_unmatch.add_argument("--reason", default="cli", help="记录一下拒绝原因(便于事后核对)")
    p_db_rematch = db_sub.add_parser(
        "rematch", help="解除上面的「不再自动匹配」(之后 dnh match 会重新考虑这些碟)")
    p_db_rematch.add_argument("keys", nargs="*", help="专辑 key(可多个); 配合 --list 时可省略")
    p_db_rematch.add_argument("--list", action="store_true",
                              help="只列出当前所有被人工取消的专辑")
    p_db_match = db_sub.add_parser(
        "match", help="手动把专辑挂到指定网易云条目(权威, 自动匹配之后不会覆盖)")
    p_db_match.add_argument("key", nargs="*", help="专辑 key; 配合 --list-manual 时可省略")
    p_db_match.add_argument("--netease", "-n", default="",
                            help="网易云专辑 id 或链接(直接粘贴浏览器地址即可)")
    p_db_match.add_argument("--clear-manual", "--auto", dest="clear_manual",
                            action="store_true",
                            help="撤销手动标记, 改回由算法决定(只删 manual 那条, 不写拒绝)")
    p_db_match.add_argument("--list-manual", action="store_true", help="列出所有手动匹配")
    p_db_mark = db_sub.add_parser("mark", help="给专辑打标记")
    p_db_mark.add_argument("key", nargs="*", help="专辑 key(可多个); 配合 --list 时可省略")
    p_db_mark.add_argument("--mark", default="已加入歌单", help="标记名, 默认「已加入歌单」")
    p_db_mark.add_argument("--note", default="")
    p_db_mark.add_argument("--remove", action="store_true", help="改为移除该标记")
    p_db_mark.add_argument("--list", action="store_true", help="列出所有标记及数量")

    p_db_ids = db_sub.add_parser(
        "ids", help="给专辑/社团/艺人/展会/标签分配实体 id(幂等), 并重建关联表")
    p_db_ids.add_argument("--build", action="store_true", help="执行分配/重建")
    p_db_strip = db_sub.add_parser(
        "strip-source-links", help="从专辑外链里剔除 VGMDB/BGM.TV 等来源数据库链接")
    p_db_strip.add_argument("--apply", action="store_true", help="真的写回(否则只统计)")
    p_db_export = db_sub.add_parser(
        "export-payload", help="导出专辑完整 payload 为 JSON(云端->本地同步用)")
    p_db_export.add_argument("--since", type=float, default=0.0,
                             help="只导出这个时间戳之后更新的专辑")
    p_db_export.add_argument("--out", default="", help="输出文件(省略则打到 stdout)")
    p_db_import = db_sub.add_parser(
        "import-payload", help="从 JSON 导入专辑完整 payload(云端同步用)")
    p_db_import.add_argument("--file", required=True, help="JSON 文件: {key: payload串}")
    p_db_import.add_argument("--apply", action="store_true", help="真的写入(否则只演练)")
    p_db_cleantags = db_sub.add_parser(
        "clean-tags", help="清掉噪声与无检索意义的标签(展会名/年份/虚拟歌姬名/格式词)")
    p_db_cleantags.add_argument("--dry-run", action="store_true",
                               help="只统计会删多少, 不写库")
    p_db_dedupe = db_sub.add_parser("dedupe", help="找出并合并疑似重复的专辑")
    p_db_dedupe.add_argument("--threshold", type=float, default=0.94,
                             help="标题相似度阈值(0~1, 默认 0.94; 调高更保守)")
    p_db_dedupe.add_argument("--limit", type=int, default=200, help="最多处理多少组")
    p_db_dedupe.add_argument("--apply", action="store_true",
                             help="真的合并(默认只演练); 每组保留可信度最高的那条")

    p_cache = sub.add_parser("cache", help="HTTP 缓存管理")
    cache_sub = p_cache.add_subparsers(dest="cache_command", metavar="<子命令>")
    cache_sub.add_parser("stats", help="缓存统计")
    p_cache_clear = cache_sub.add_parser("clear", help="清空缓存")
    p_cache_clear.add_argument("--url", help="只清理匹配该子串的 URL")
    p_cache_clear.add_argument("--vacuum", action="store_true", help="只清理 30 天前的")

    # serve
    p_public = sub.add_parser(
        "public", help="启动**面向公众的只读站点**(浏览/搜索/详情/看板, 无写操作)")
    p_public.add_argument("--host", default="127.0.0.1")
    p_public.add_argument("--port", type=int, default=8940)
    p_public.add_argument("--users-db", default=None,
                          help="用户库路径(默认 data/public_users.sqlite3)")
    p_public.add_argument("--sessions-db", default=None,
                          help="会话库路径(默认取 [public] sessions_db, 即 data/sessions.sqlite3)")
    p_public.add_argument("--no-session-persist", action="store_true",
                          help="会话只放内存(重启掉线; 仅临时调试用)")
    p_public.add_argument("--base-url", default="",
                          help="站点对外地址(写进 robots/sitemap), 如 https://dnh.example.com")

    p_serve = sub.add_parser("serve", help="启动本地 Web 面板")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8787)
    p_serve.add_argument("--open", action="store_true", help="启动后打开浏览器")

    return parser


def _add_event_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--event", "-e", default="", help="展会, 如 M3-2026秋 / C107 / 2026 / 空=近期")
    parser.add_argument("--year", type=int, default=0)


# --------------------------------------------------------------------------
# 命令实现
# --------------------------------------------------------------------------


def _load_config(args: argparse.Namespace) -> Config:
    root = Path(args.root).resolve() if getattr(args, "root", None) else Path.cwd()
    try:
        cfg = Config.load(getattr(args, "config", None), root=root)
    except DnhError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        raise SystemExit(2)
    cfg.ensure_dirs()
    if getattr(args, "log_level", None):
        cfg.general.log_level = args.log_level
    return cfg



def _netease_status(client) -> dict:
    """只读地看一眼网易云接口能不能用(不再有登录/收藏功能)."""
    try:
        payload = client.login_status()
    except Exception as exc:
        return {"ok": False, "mode": client.mode, "reason": f"{type(exc).__name__}: {exc}"}
    code = payload.get("code")
    profile = payload.get("profile") or {}
    if profile.get("userId"):
        return {"ok": True, "mode": client.mode, "uid": int(profile["userId"]),
                "nickname": profile.get("nickname") or "", "reason": ""}
    return {"ok": True, "mode": client.mode, "uid": 0, "nickname": "",
            "reason": f"接口可用(未登录, code={code}); 检索匹配不需要登录"}


def cmd_init(cfg: Config, args: argparse.Namespace) -> int:
    cfg.ensure_dirs()
    path = cfg.root / DEFAULT_CONFIG_RELPATH
    if path.exists() and not args.force:
        out(f"配置文件已存在: {path} (用 --force 覆盖)")
    else:
        write_example_config(path)
        out(f"已生成配置文件: {path}")
    env_example = cfg.root / "config" / ".env.example"
    env_example.write_text(
        "# 复制为 config/.env 或 .env 后填写; 也可以直接写进 config/config.toml\n"
        "DNH_NETEASE_COOKIE=\n"
        "DNH_NETEASE_PHONE=\n"
        "DNH_NETEASE_PASSWORD=\n"
        "DNH_X_BEARER_TOKEN=\n"
        "DNH_PROXY_URL=http://127.0.0.1:7897\n"
        "DNH_PROXY_MODE=hosts\n"
        "DNH_SOURCE_DISCOGS_TOKEN=\n",
        encoding="utf-8",
    )
    out(f"已生成环境变量模板: {env_example}")
    for d in (cfg.data_dir, cfg.cache_dir, cfg.data_dir / "covers", cfg.reports_dir):
        out(f"目录: {d}")
    out("")
    out("下一步:")
    out("  1) dnh doctor               # 检查代理 / 数据源 / 网易云连通性")
    out("  2) dnh events list          # 看看最近有哪些展会")
    out("  3) dnh discover --event M3-2026秋")
    return 0


def cmd_doctor(cfg: Config, args: argparse.Namespace) -> int:
    from .httpclient import HttpClient

    report: dict[str, Any] = {"version": __version__, "python": sys.version.split()[0], "root": str(cfg.root)}
    checks: list[tuple[str, bool, str]] = []

    checks.append(("Python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0]))
    checks.append(("配置文件", cfg.config_file is not None, str(cfg.config_file or "未找到(使用默认值)")))

    # 目录可写
    try:
        probe = cfg.data_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(("数据目录可写", True, str(cfg.data_dir)))
    except OSError as exc:
        checks.append(("数据目录可写", False, str(exc)))

    # 代理
    proxy = cfg.proxy
    if proxy.enabled and proxy.url and proxy.mode != "off":
        from urllib.parse import urlsplit

        parts = urlsplit(proxy.url)
        host, port = parts.hostname or "127.0.0.1", parts.port or 7897
        reachable = False
        try:
            with socket.create_connection((host, port), timeout=2.5):
                reachable = True
        except OSError as exc:
            checks.append(("代理可达", False, f"{host}:{port} 连接失败: {exc}"))
        if reachable:
            checks.append(("代理可达", True, f"{host}:{port} ({len(proxy.hosts)} 个站点走代理)"))
    else:
        checks.append(("代理", True, "已禁用"))
    report["proxy"] = {"enabled": proxy.enabled, "url": proxy.url, "mode": proxy.mode}

    # 数据源
    with Pipeline(cfg, LOG) as pipe:
        src_rows = pipe.describe_sources()
        for row in src_rows:
            status = "OK" if row["available"] else "不可用"
            if not row["enabled"]:
                status += " (未启用)"
            checks.append(
                (f"数据源 {row['name']}", row["available"] and row["enabled"], f"{status} {row['reason']}".strip())
            )
        report["sources"] = src_rows

        # 网易云(只读: 这里只探测接口能不能连通 / 有没有登录态)
        client = pipe.netease
        info = _netease_status(client)
        checks.append(
            (
                "网易云登录",
                info["ok"],
                f"uid={info['uid']} {info.get('nickname','')}".strip() if info["ok"] else info["reason"],
            )
        )
        checks.append(("网易云传输", True, client.mode))
        report["netease"] = {"mode": client.mode, **info}

        # X 后端已移除
        xsrc = None
        report["db"] = pipe.stats()

    if args.json:
        out_json({"checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks], **report})
        return 0

    out(f"doujindigdb {__version__} 环境自检")
    out("=" * 62)
    width = max(len(n) for n, _, _ in checks)
    bad = 0
    for name, ok, detail in checks:
        mark = "✓" if ok else "✗"
        if not ok:
            bad += 1
        out(f" {mark} {name.ljust(width)}  {detail}")
    out("=" * 62)
    counts = report["db"]
    out(
        f"数据库: 专辑 {counts['albums']} / 来源映射 {counts['album_sources']} / "
        f"标记 {counts.get('album_marks', 0)} / 网易云匹配 {counts['netease_matches']}"
    )
    if counts.get("events"):
        out("已入库展会: " + ", ".join(f"{name}({n})" for name, _kind, n in counts["events"]))
    out(f"未通过项: {bad}")
    if bad:
        out("提示: 代理不通时可用 `DNH_PROXY_MODE=off` 或修改 config/config.toml 的 [proxy]。")
    return 0


def cmd_events(cfg: Config, args: argparse.Namespace) -> int:
    if args.events_command in (None, "list"):
        year = getattr(args, "year", 0) or cfg.events.year or events_mod.default_year()
        kinds = tuple(k.strip() for k in getattr(args, "kinds", "m3,comiket").split(",") if k.strip())
        evs = events_mod.events_for_year(year, kinds=kinds)
        try:
            recent = events_mod.recent_events(kinds=kinds)
        except Exception:
            recent = []
        if args.json:
            out_json({"year": year, "events": [e.to_dict() for e in evs], "recent": [e.name for e in recent]})
            return 0
        out(f"{year} 年展会 (近期: {', '.join(e.name for e in recent) or '无'})")
        for ev in evs:
            out(f"  {ev.name:<16} {ev.start_date} ~ {ev.end_date}  标签 {len(ev.topic_tags)} 个")
            out(f"      {', '.join(ev.topic_tags[:10])}{' ...' if len(ev.topic_tags) > 10 else ''}")
        return 0

    if args.events_command == "tags":
        evs = events_mod.resolve(args.event, year=cfg.events.year, kinds=cfg.events.kinds)
        if not evs:
            print(f"无法识别事件: {args.event}", file=sys.stderr)
            return 2
        ev = evs[0]
        fmt = args.format or ("json" if args.json else "text")
        if fmt == "json":
            out_json(ev.to_dict())
        elif fmt == "hash":
            out(" ".join(ev.topic_tags))
        else:
            out(f"{ev.name}  ({ev.start_date} ~ {ev.end_date})")
            for tag in ev.topic_tags:
                out("  " + tag)
        return 0
    return 2


def cmd_sources(cfg: Config, args: argparse.Namespace) -> int:
    with Pipeline(cfg, LOG) as pipe:
        rows = pipe.describe_sources()
    if args.json:
        out_json(rows)
        return 0
    out(f"{'名称':<14}{'启用':<6}{'可用':<6}说明")
    out("-" * 78)
    for row in rows:
        out(
            f"{row['name']:<14}{'是' if row['enabled'] else '否':<6}"
            f"{'是' if row['available'] else '否':<6}{row['reason'] or row['label']}"
        )
    return 0


def cmd_discover(cfg: Config, args: argparse.Namespace) -> int:
    sources = _split(args.sources)
    with Pipeline(cfg, LOG) as pipe:
        pipe.on_event = _progress
        evs = events_mod.resolve(args.event, year=args.year or cfg.events.year, kinds=cfg.events.kinds)
        if args.query and not evs:
            evs = []
        stats = pipe.discover(evs, sources=sources, limit=args.limit, query=args.query,
                              download_covers=args.covers)
    if args.json:
        out_json(stats.to_dict())
    else:
        out(stats.summary())
    return 0


def cmd_enrich(cfg: Config, args: argparse.Namespace) -> int:
    sources = _split(args.sources)
    with Pipeline(cfg, LOG) as pipe:
        albums = pipe.store.list_albums(event=args.event or None, limit=args.limit)
        improved, stats = pipe.enrich(albums, sources=sources, download_covers=not args.no_covers)
        bili: list = []
        if args.bilibili or bool(getattr(cfg.sources, "bilibili_audition", False)):
            bili, _ = pipe.fill_bilibili_links(
                event=args.event or "",
                budget=int(getattr(cfg.sources, "bilibili_budget", 40) or 40))
    if args.json:
        out_json(stats.to_dict())
    else:
        out(f"富化完成: 更新 {len(improved)} / {len(albums)} 张, 下载封面 {stats.covers_downloaded} 张")
        if bili:
            out(f"B站补试听链接: {len(bili)} 张(用 `dnh bilibili` 可继续补)")
        for album in improved[:25]:
            out(f"  {album.display_title}  ({album.circle})  曲目 {len(album.tracks)}  封面 {'有' if album.cover_url else '无'}")
    return 0


def cmd_match(cfg: Config, args: argparse.Namespace) -> int:
    from .netease.matcher import summarize_matches

    with Pipeline(cfg, LOG) as pipe:
        # ``--limit 0`` = 无上限(一直跑到手动 Ctrl+C 停)
        fetch = args.limit if args.limit and args.limit > 0 else 1_000_000
        albums = pipe.store.list_albums(event=args.event or None, tag=args.tag or None,
                                        limit=fetch)
        results = pipe.match(albums, min_score=args.min_score, force=args.force)
        # 「已拉黑候选」单独归类(候选级) —— 这些碟**仍然**在参与匹配, 只是那几条不参选
        blocked_note = pipe.matcher.blocked_summary()
        # 「已手动匹配」的也单独归类: 算法跳过了它们(手动指定优先)
        manual_note = pipe.matcher.manual_summary()
    if args.json:
        out_json([m.to_dict() for m in results])
    else:
        out(summarize_matches(results, limit=args.show))
        if blocked_note:
            out(blocked_note + "(这些碟仍会尝试其它候选; 要解禁用 dnh db rematch <key>)")
        if manual_note:
            out(manual_note + "(手动指定优先; 要改回自动用 dnh db match <key> --clear-manual)")
    return 0


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    sources = _split(args.sources)
    with Pipeline(cfg, LOG) as pipe:
        pipe.on_event = _progress
        stats = pipe.run(
            args.event,
            sources=sources,
            limit=args.limit,
            query=args.query,
            do_match=not args.no_match,
            enrich=not args.no_enrich,
            download_covers=not args.no_covers,
        )
    if args.json:
        out_json(stats.to_dict())
    else:
        out(stats.summary())
    return 0


def cmd_playlist(cfg: Config, args: argparse.Namespace) -> int:
    """网易云歌单 ↔ 本地库 对照(只读; 可选把命中项打标记)."""
    from .playlist import compare_playlist, parse_playlist_ref

    if getattr(args, "playlist_command", None) != "compare":
        print("用法: dnh playlist compare --url <网易云歌单链接>", file=sys.stderr)
        return 2

    playlist_id = parse_playlist_ref(args.url)
    if not playlist_id:
        print(f"无法从 {args.url!r} 里解析出歌单 id", file=sys.stderr)
        return 2

    with Pipeline(cfg, LOG) as pipe:
        if args.no_terminal:
            pass
        else:
            out(f"读取歌单 {playlist_id} ...")
        meta, songs = pipe.netease.playlist_tracks(playlist_id)
        name = str(meta.get("name") or "")
        if not songs:
            print("歌单是空的, 或者这个歌单不可见(私密歌单需要登录态 Cookie)", file=sys.stderr)
            return 1

        albums = pipe.store.list_albums(
            event=args.event or None,
            tag=args.tag or None,
            limit=args.limit or 5000,
            order="circle",
        )
        # 默认只对照**已匹配网易云**的专辑: 没上线的碟加不进歌单, 列出来只是噪音
        from .playlist import filter_by_match, normalize_match_filter

        match_filter = normalize_match_filter(getattr(args, "match_filter", ""))
        albums = filter_by_match(
            albums, lambda a: pipe.store.get_match(a.key) is not None, match_filter)
        result = compare_playlist(songs, albums, playlist_id=playlist_id,
                                  playlist_name=name,
                                  track_count=int(meta.get("trackCount") or len(songs)))

        if args.mark:
            keys = result.matched_album_keys
            for key in keys:
                pipe.store.add_mark(key, args.mark)
            if args.json:
                pass
            else:
                out(f"已给 {len(keys)} 张命中的专辑打上标记「{args.mark}」")

    if args.json:
        out_json(result.to_dict())
        return 0

    out("")
    out(result.summary(limit=args.show))
    if not args.mark:
        out("")
        out("提示: 加 --mark 已加入歌单 可以顺手把命中的专辑标记掉, "
            "之后在 Web 面板「专辑库」用标记过滤。")
    return 0


def cmd_report(cfg: Config, args: argparse.Namespace) -> int:
    from .db import Store

    store = Store(cfg.db_path)
    try:
        formats = [f.strip() for f in args.format.split(",") if f.strip()]
        paths = report_mod.write_reports(
            cfg, store,
            event=args.event, tag=args.tag, group_by=args.group_by,
            formats=formats, title=args.title, limit=args.limit,
        )
    finally:
        store.close()
    if args.json:
        out_json({k: str(v) for k, v in paths.items()})
        return 0
    if not paths:
        out("未生成任何报告(检查 --format)")
        return 0
    for fmt, path in paths.items():
        out(f"{fmt:<12} {path}")
    target = paths.get("html_latest") or paths.get("html")
    if args.open and target:
        try:
            webbrowser.open(Path(target).as_uri())
        except Exception as exc:
            LOG.debug("打开浏览器失败: %s", exc)
    return 0


def cmd_bilibili(cfg: Config, args: argparse.Namespace) -> int:
    """搜 B 站搬运的试听视频, 给专辑补 ``urls["bilibili"]``.

    有风控 + 误配风险, 所以: 每张碟最多 2 个检索词、整轮有预算、
    且只处理**还没有 bilibili 链接**的专辑(重复跑安全)。
    """
    from .pipeline import Pipeline

    with Pipeline(cfg, None) as pipe:
        albums = pipe.store.list_albums(event=args.event or "", limit=args.limit or 100000)
        if args.dry_run:
            from . import bilibili as bb

            hits = 0
            for album in albums[: args.budget or len(albums)]:
                if (album.urls or {}).get("bilibili"):
                    continue
                for query in bb.search_queries(album):
                    try:
                        payload = pipe.http.get_json(
                            bb.search_url(query), use_cache=True, retries=0,
                            timeout=20, headers=bb.REQUEST_HEADERS)
                    except Exception as exc:
                        print(f"   ! {query}: {type(exc).__name__}")
                        continue
                    url = bb.pick_audition_video(
                        album, bb.parse_search_payload(payload))
                    if url:
                        hits += 1
                        print(f"   ✓ {album.display_title[:34]:<36} {url}")
                        break
            print(f"\n(dry-run) 预计可补 {hits} 张")
            return 0

        found, _ = pipe.fill_bilibili_links(
            event=args.event or "", limit=args.limit or 0, budget=args.budget)
    print(f"补到 {len(found)} 张 B 站试听链接。")
    for album in found[:20]:
        print(f"   {album.display_title[:36]:<38} {album.urls.get('bilibili', '')}")
    return 0


def _covers_broken(cfg: Config, args: argparse.Namespace) -> int:
    """列出(并可清理)加载不出的封面.

    三类"加载不出":

    * ``cover_local`` 指向的文件已经不存在了(手动删过 / 换了机器);
    * ``cover_url`` 上游 4xx/5xx(链接失效、防盗链);
    * ``cover_url`` 上游用 **200 + HTML** 返回风控页 —— 浏览器当图片解析必然失败
      (Tumblr 的拦截页就是这样, 还会被封面缓存当成图片存下来)。

    联网检查受 ``--budget`` 限制; 加 ``--clear`` 会把这些封面清掉,
    之后 ``dnh enrich`` / ``dnh covers --from-shop`` 可以重新补。
    """
    from pathlib import Path

    from . import coverimage
    from .pipeline import Pipeline

    budget = max(0, int(getattr(args, "budget", 80) or 0))
    broken: list[tuple[Any, str]] = []
    stale_local: list[Any] = []
    purged_cache = 0
    with Pipeline(cfg, None) as pipe:
        # 顺手清掉"被当成图片缓存下来的 HTML 错误页"
        cache_dir = cfg.data_dir / "covers" / "http"
        if cache_dir.is_dir():
            for path in cache_dir.glob("*.img"):
                try:
                    head = path.read_bytes()[:65536]
                except OSError:
                    continue
                if coverimage.image_size(head) is None:
                    try:
                        path.unlink()
                        purged_cache += 1
                    except OSError:
                        pass

        albums = pipe.store.all_albums()
        if getattr(args, "event", ""):
            albums = [album for album in albums if album.event == args.event]
        checked = 0
        for album in albums:
            local = (album.cover_local or "").strip()
            if local:
                path = Path(local)
                problem = ""
                if not path.is_file():
                    problem = "本地文件不存在"
                else:
                    try:
                        head = path.read_bytes()[:65536]
                    except OSError:
                        head = b""
                    if coverimage.image_size(head) is None:
                        problem = "本地文件不是图片"
                if problem:
                    # 远程地址还在的话, 界面会自动回退到它 —— 只清本地路径即可,
                    # 别把还能用的远程封面一起清掉。
                    if (album.cover_url or "").strip():
                        stale_local.append(album)
                    else:
                        broken.append((album, problem))
                continue
            url = (album.cover_url or "").strip()
            if not url or checked >= budget:
                continue
            checked += 1
            try:
                resp = pipe.http.get(url, use_cache=False, retries=0, timeout=8,
                                     raise_for_status=False)
            except Exception as exc:  # 超时 / DNS / TLS
                broken.append((album, f"请求失败: {type(exc).__name__}"))
                continue
            if not getattr(resp, "ok", False):
                status = int(getattr(resp, "status", 0) or 0)
                # 连接失败时客户端给的是 status=0 的空响应, 不是异常
                broken.append((album, f"HTTP {status}" if status else "请求失败"))
            elif coverimage.image_size((getattr(resp, "body", b"") or b"")[:65536]) is None:
                broken.append((album, "返回的不是图片"))

        if purged_cache:
            print(f"清掉 {purged_cache} 个不是图片的封面缓存")
        print(f"检查 {checked} 个远程封面 + 全部本地封面, 发现 {len(broken)} 张加载不出。")
        if stale_local:
            print(f"另有 {len(stale_local)} 张本地文件丢失, 但远程地址还在(界面会自动回退)。")
        for album, why in broken[:40]:
            print(f"   {album.display_title[:34]:<36} {why:<20} "
                  f"{(album.cover_url or album.cover_local)[:56]}")
        if len(broken) > 40:
            print(f"   ... 其余 {len(broken) - 40} 张省略")
        if not broken and not stale_local:
            return 0
        if getattr(args, "clear", False):
            result = pipe.store.clear_album_covers([album.key for album, _why in broken])
            fixed = pipe.store.clear_local_covers([album.key for album in stale_local])
            extra = f"; 另有 {fixed} 张只清了失效的本地路径(保留远程地址)" if fixed else ""
            print(f"已清掉: 专辑 {result['cleared']} 张, 文件 {result['files']} 个{extra}。"
                  f"之后可用 enrich / covers --from-shop 重新补。")
        else:
            print("(只列出; 加 --clear 清掉它们)")
    return 0


def _covers_cache_remote(cfg: Config, args: argparse.Namespace) -> int:
    """``dnh covers --cache-remote``: 把**外链封面**下载缓存到本地.

    现状是网易云匹配后 ``cover_url`` 指向 ``p1.music.126.net`` 这种外链, 图不落盘 ——
    对方图床一失效/防盗链, 界面就没图了。这里用 HttpClient(超时/节流/robots 都在)
    逐个下载, 校验确实是图片后写到 ``data/covers/<key>.img`` 并补 ``cover_local``,
    **保留 ``cover_url``**。

    幂等: 已有 ``cover_local`` 的直接跳过; ``--dry-run`` 只统计不联网。
    """
    from .covercache import (REASON_LABELS, STATUS_CACHED, STATUS_FAILED,
                             cache_remote_covers)
    from .db import Store
    from .httpclient import HttpClient

    limit = max(0, int(getattr(args, "limit", 0) or 0))
    dry_run = bool(getattr(args, "dry_run", False))
    store = Store(cfg.db_path)
    try:
        http = None
        if not dry_run:
            #: 走 HttpClient(与其它命令一致: 超时/节流/robots 都在)。
            #: 故意不挂 HTTP 磁盘缓存 —— 图片没必要再存一份, 万一缓存进
            #: "200 + HTML 风控页" 反而更糟(见 covers --broken 里的清理逻辑)。
            http = HttpClient(cfg, logger=LOG)
        stats = cache_remote_covers(store, http=http, limit=limit or None, dry_run=dry_run)
    finally:
        store.close()

    out(stats.summary())
    if dry_run:
        out(f"（--dry-run：不下载不写库; 待缓存 {stats.pending} 张）")
    counts = stats.reason_counts()
    if counts:
        detail = " · ".join(f"{REASON_LABELS.get(r, r)} {n}" for r, n in counts.items())
        out(f"失败原因: {detail}")
    cached = stats.by_status(STATUS_CACHED)
    for item in cached[:5]:
        note = f"  {item.detail}" if item.detail else ""
        out(f"   已缓存 {item.path.name if item.path else item.key}"
            f"  {item.width}x{item.height}  {item.size} B{note}")
    if len(cached) > 5:
        out(f"   ... 其余 {len(cached) - 5} 张省略")
    for item in stats.by_status(STATUS_FAILED)[:10]:
        out(f"   [失败] {item.key[:44]:<46} {item.reason_label:<12} {item.detail[:60]}")
    if stats.failed > 10:
        out(f"   ... 其余 {stats.failed - 10} 条失败省略(用 --log-level debug 看日志)")
    if stats.deferred:
        out(f"（--limit {limit or 0} 生效：本次只处理 {stats.attempted} 张, 还剩 {stats.deferred} 张）")
    if stats.cached:
        out("封面已落盘到 data/covers/（cover_url 保留不变）")
    return 0


def cmd_covers(cfg: Config, args: argparse.Namespace) -> int:
    """检查**被多张专辑共用**的封面 —— 基本可以断定是抓错了。

    最常见的误抓是艺人主页图(SoundCloud 的 artwork)、站点通用图、平台图标。
    实测踩到过两张不同的碟拿到同一张 sndcdn 作品图。
    """
    from .db import Store

    if getattr(args, "cache_remote", False):
        return _covers_cache_remote(cfg, args)

    if getattr(args, "broken", False):
        return _covers_broken(cfg, args)

    if getattr(args, "from_shop", False):
        from .pipeline import Pipeline as _Pipe

        with _Pipe(cfg, None) as pipe:
            filled, _ = pipe.fill_covers_from_shops(
                event=args.event or "",
                budget=int(getattr(cfg.sources, "shop_cover_budget", 200) or 200))
        print(f"从通贩页面补到 {len(filled)} 张封面。")
        for album in filled[:20]:
            print(f"   {album.display_title[:36]:<38} {(album.cover_url or '')[:56]}")
        return 0

    if getattr(args, "from_netease", False):
        from .pipeline import Pipeline

        with Pipeline(cfg, None) as pipe:
            filled, _ = pipe.fill_covers_from_netease(event=args.event)
        print(f"从网易云补到 {len(filled)} 张封面。")
        for album in filled[:20]:
            print(f"   {album.display_title[:40]:<42} {(album.cover_url or '')[:50]}")
        return 0

    if getattr(args, "suspect", False):
        store = Store(cfg.db_path)
        try:
            items = store.suspicious_special_covers(event=args.event)
            if not items:
                print("没有发现可疑封面。")
                return 0
            print(f"发现 {len(items)} 张可疑封面(与社团主页同域):\n")
            for item in items:
                print(f"  {item['circle'][:16]:<18} {item['title'][:30]:<32}")
                print(f"      封面 {item['cover_url'][:78]}")
                print(f"      主页 {item['page'][:78]}")
            if getattr(args, "clear", False):
                cleared = 0
                for item in items:
                    cleared += store.clear_cover(item["cover_url"])
                print(f"\n已清掉 {cleared} 张。")
            else:
                print("\n加 --clear 可清掉(建议先扫一眼清单)。")
        finally:
            store.close()
        return 0

    store = Store(cfg.db_path)
    try:
        groups = store.duplicate_covers(min_count=args.min_count, event=args.event)
        if not groups:
            print("没有发现被共用的封面。")
            return 0

        total = sum(g["count"] for g in groups)
        print(f"发现 {len(groups)} 组共用封面, 涉及 {total} 张专辑:\n")
        for index, group in enumerate(groups, start=1):
            print(f"[{index}] {group['count']} 张共用: {group['cover_url']}")
            for album in group["albums"]:
                print(f"      {album['circle'][:18]:<20} {album['title'][:34]:<36} "
                      f"({album['event']})")
            print()

        if args.clear:
            cleared = 0
            for group in groups:
                cleared += store.clear_cover(group["cover_url"], event=args.event)
            print(f"已清掉 {cleared} 张专辑的封面(下次 enrich 会重新找)。")
        else:
            print("加 --clear 可把这些封面清掉(之后 enrich 会重新找)。")
    finally:
        store.close()
    return 0


def _cmd_db_desc(cfg: Config, args: argparse.Namespace, store) -> int:
    """补简介.

    **只认条目的 summary**: bgm.tv 的 ``row["comment"]`` 是收藏者短评(吐槽),
    曾经被当成简介抓进来过(实测全库清了 25 条, 包括 InPhant 那种)。
    所以这里走 ``fetch_album`` 拿到的 ``description``, 它来自 API 的 summary 字段。
    """
    from .pipeline import Pipeline

    source = str(getattr(args, "source", "bangumi") or "bangumi")
    event = str(getattr(args, "event", "") or "")
    limit = int(getattr(args, "limit", 0) or 0)
    overwrite = bool(getattr(args, "overwrite", False))

    albums = store.list_albums(event=event or None, limit=None)
    targets = [a for a in albums
               if str(a.source_ids.get(source) or "").strip().isdigit()
               and (overwrite or not (a.description or "").strip())]
    if limit:
        targets = targets[:limit]
    out(f"来源 {source} · 需要补简介 {len(targets)} 张")
    if getattr(args, "dry_run", False):
        for album in targets[:20]:
            out(f"  {album.display_title[:44]:<46} id={album.source_ids.get(source)}")
        return 0

    filled = 0
    with Pipeline(cfg, None) as pipe:
        src = next((s for s in pipe.sources if getattr(s, "name", "") == source), None)
        if src is None:
            out(f"没有启用的数据源 {source}")
            return 2
        for index, album in enumerate(targets, start=1):
            try:
                fresh = src.fetch_album(str(album.source_ids[source]))
            except Exception as exc:                    # noqa: BLE001
                out(f"  [{index}/{len(targets)}] 抓取失败: {exc}")
                continue
            text = (getattr(fresh, "description", "") or "").strip() if fresh else ""
            if not text:
                continue
            saved = store.get_album(album.key)
            if saved is None or saved.edited:
                continue
            saved.description = text
            store._write_album(saved, __import__("time").time())
            filled += 1
            if index <= 3 or filled % 10 == 0:
                out(f"  [{index}/{len(targets)}] {saved.display_title[:34]:<36} {text[:30]!r}")
    out(f"完成: 补上 {filled} 张")
    return 0


def _cmd_db_reparse(cfg: Config, args: argparse.Namespace, store) -> int:
    """用库里存的源 ID 重新抓页面, 覆盖被解析 bug 弄脏的元数据.

    为什么需要: 解析器修好了, 但**已经入库的数据不会自己变好**。重采整轮代价太大,
    而这些专辑的源 ID 都还在 ``source_ids`` 里, 所以按 id 重抓一页即可就地修正。
    """
    from .pipeline import Pipeline

    source = str(getattr(args, "source", "vgmdb") or "vgmdb")
    event = str(getattr(args, "event", "") or "")
    limit = int(getattr(args, "limit", 0) or 0)
    dry_run = bool(getattr(args, "dry_run", False))

    albums = store.list_albums(event=event or None, limit=None)
    targets = [a for a in albums
               if str(a.source_ids.get(source) or "").strip()
               and str(a.source_ids.get(source)).strip().isdigit()]
    if limit:
        targets = targets[:limit]
    out(f"来源 {source} · 可重解析 {len(targets)} 张")
    if dry_run:
        for album in targets[:20]:
            out(f"  {album.display_title[:42]:<44} id={album.source_ids.get(source)}"
                f" circle={album.circle[:20]!r}")
        return 0

    ok = skipped = failed = 0
    with Pipeline(cfg, None) as pipe:
        src = next((s for s in pipe.sources if getattr(s, "name", "") == source), None)
        if src is None:
            out(f"没有启用的数据源 {source}")
            return 2
        for index, album in enumerate(targets, start=1):
            try:
                fresh = src.fetch_album(str(album.source_ids[source]))
            except Exception as exc:              # noqa: BLE001
                failed += 1
                out(f"  [{index}/{len(targets)}] {album.display_title[:30]} 抓取失败: {exc}")
                continue
            if fresh is None:
                failed += 1
                continue
            changed, error = store.replace_from_fresh(album.key, fresh)
            if changed:
                ok += 1
                if index % 10 == 0 or index <= 3:
                    out(f"  [{index}/{len(targets)}] {fresh.title[:34]:<36} "
                        f"circle={fresh.circle[:24]!r}")
            else:
                skipped += 1
                if error:
                    out(f"  [{index}/{len(targets)}] 跳过: {error}")
    out(f"完成: 更新 {ok} · 跳过 {skipped} · 失败 {failed}")
    return 0


def _db_match_manual(cfg: Config, args: argparse.Namespace, store) -> int:
    """``dnh db match`` —— 手动匹配 / 撤销手动 / 列出手动.

    三种用法(与面板抽屉里那三个按钮一一对应):

    * ``dnh db match <key> --netease <id|链接>`` —— 手动挂匹配(**权威**, 之后自动匹配
      不会覆盖它; 顺带清掉这张碟之前的"人工取消")
    * ``dnh db match <key> --clear-manual``      —— 撤销手动标记, 改回算法决定
      (只删那条 ``manual`` 记录, **不**写人工拒绝 —— 与 ``db unmatch`` 是两件事)
    * ``dnh db match --list-manual``             —— 列出所有手动匹配
    """
    from .netease.links import describe_ref_problem, parse_album_ref

    if getattr(args, "list_manual", False):
        rows = store.manual_matches()
        if args.json:
            out_json([m.to_dict() for m in rows])
            return 0
        if not rows:
            out("没有手动匹配的专辑 ✓")
            return 0
        out(f"手动匹配的专辑: {len(rows)} 张")
        for m in rows:
            out(f"  {m.album_key}")
            out(f"      {m.album_title[:40]} -> {m.target_name} "
                f"(album_id={m.target_album_id}, 艺人 {m.artist_score:.2f}, "
                f"曲目重合 {m.track_score:.2f})")
        return 0

    keys = [str(k).strip() for k in (getattr(args, "key", None) or []) if str(k).strip()]
    if not keys:
        out("需要一个专辑 key(或加 --list-manual 查看已有的手动匹配)")
        return 2

    if getattr(args, "clear_manual", False):
        cleared = 0
        for key in keys:
            removed = int(store.clear_manual_match(key) or 0)
            cleared += removed
            if not removed:
                out(f"  {key}: 当前不是手动匹配(或压根没有匹配), 未改动")
        if args.json:
            out_json({"cleared": cleared, "keys": keys})
        else:
            out(f"已撤销 {cleared} 张的手动标记 -> 下次 dnh match 由算法重新判断")
        return 0

    ref = str(getattr(args, "netease", "") or "").strip()
    if not ref:
        out('需要 --netease <专辑 id 或链接>; 也可以先用 `dnh netease search "关键词"` 找 id')
        return 2
    album_id = parse_album_ref(ref)
    if not album_id:
        out(f"解析不了网易云专辑 id: {describe_ref_problem(ref)}")
        return 2

    results = []
    with Pipeline(cfg, LOG) as pipe:
        for key in keys:
            album = pipe.store.get_album(key)
            if album is None:
                out(f"  {key}: 专辑不存在(key 对不上)")
                continue
            try:
                view = pipe.netease.album_detail_view(album_id)
            except Exception as exc:
                out(f"  {key}: 网易云取不到专辑 {album_id}: {exc}")
                continue
            if not view:
                out(f"  {key}: 网易云上没有这个专辑(id={album_id}) —— 确认链接/ id 有没有错")
                continue
            was_blocked = bool(pipe.store.is_blocked(key, int(view.get("album_id") or album_id)))
            result = pipe.matcher.manual_match(
                album,
                album_id=int(view.get("album_id") or album_id),
                name=str(view.get("name") or ""),
                artists=[str(a) for a in (view.get("artists") or [])],
                track_titles=[str(t.get("title") or "") for t in (view.get("tracks") or [])],
            )
            # 顺手解掉**这一条**的黑名单(互斥规则在 Store 里只解这一对, 别的拉黑照旧)
            pipe.store.save_match(result)
            results.append(result)
            out(f"  {album.display_title} -> {result.target_name} "
                f"(album_id={result.target_album_id}, 艺人 {result.artist_score:.2f}, "
                f"曲目重合 {result.track_score:.2f})")
            if was_blocked:
                out("      (这条候选之前被你拉黑过, 现在一并解禁 —— 你既然手动挂上了它)")
        if args.json:
            out_json({"manual": [m.to_dict() for m in results]})
        else:
            out(f"手动匹配 {len(results)} 张 —— 已标记 method=manual, "
                f"之后的 dnh match 不会覆盖它(要撤销: dnh db match <key> --clear-manual)")
    return 0


def cmd_netease(cfg: Config, args: argparse.Namespace) -> int:
    """``dnh netease search`` —— **只读**检索, 帮用户找到要手动挂的专辑 id."""
    if getattr(args, "netease_command", "") != "search":
        out('用法: dnh netease search "关键词" [--limit n]')
        return 2
    keyword = " ".join(getattr(args, "keywords", None) or []).strip()
    if not keyword:
        out('需要关键词, 例如: dnh netease search "Celestine Stargazer"')
        return 2
    limit = max(1, min(int(getattr(args, "limit", 10) or 10), 30))
    with Pipeline(cfg, LOG) as pipe:
        try:
            items = pipe.netease.search_albums_for_picker(keyword, limit=limit)
        except Exception as exc:
            out(f"检索失败: {exc}")
            return 1
    if args.json:
        out_json({"query": keyword, "items": items})
        return 0
    if not items:
        out("没有搜到专辑 —— 换个关键词试试(去掉社团名, 或改用罗马字/原名)")
        return 0
    out(f"网易云专辑候选({len(items)} 条):")
    for item in items:
        out(f"  {item['album_id']:>12}  {item['name'][:46]}")
        out(f"               艺人={', '.join(item['artists'])[:44]}  "
            f"发行={item['release_date'] or '?'}  曲目={item['track_count']}")
        out(f"               {item['url']}")
    out('选定后: dnh db match <专辑 key> --netease <上面的 id 或链接>')
    return 0


def cmd_db(cfg: Config, args: argparse.Namespace) -> int:
    from .db import Store

    if args.db_command == "backup":
        return _cmd_db_backup(cfg, args)

    store = Store(cfg.db_path)
    try:
        if args.db_command in (None, "stats"):
            data = store.counts()
            data["events"] = store.album_events()
            data["top_tags"] = store.album_tags()[:20]
            if args.json:
                out_json(data)
                return 0
            out("数据库统计")
            out("-" * 40)
            for key in ("albums", "album_sources", "album_tags", "album_marks", "netease_matches"):
                out(f"  {key:<20} {data[key]}")
            if data["events"]:
                out("  展会分布:")
                for name, kind, n in data["events"]:
                    out(f"    {name:<18} {kind:<10} {n}")
            if data["top_tags"]:
                out("  热门标签: " + ", ".join(f"{t}({n})" for t, n in data["top_tags"][:12]))
            return 0

        if args.db_command == "desc":
            return _cmd_db_desc(cfg, args, store)

        if args.db_command == "reparse":
            return _cmd_db_reparse(cfg, args, store)

        if args.db_command == "albums":
            albums = store.list_albums(
                event=args.event or None, tag=args.tag or None, query=args.query or None, limit=args.limit
            )
            if args.json:
                out_json([a.to_dict() for a in albums])
                return 0
            if not albums:
                out("(没有专辑。先跑 `dnh discover`)")
            for album in albums:
                match = store.get_match(album.key)
                mark = f"✓{match.score:.2f}" if match else "  --  "
                out(f"  {mark} [{album.event or '--':<12}] {album.circle[:20]:<20} {album.display_title[:46]:<46} 曲目 {len(album.tracks):>3}")
            return 0

        if args.db_command == "reset":
            if not args.yes:
                print("需要 --yes 确认", file=sys.stderr)
                return 2
            store.reset(keep_matches=args.keep_matches)
            out("已清空")
            return 0

        if args.db_command == "prune":
            before = args.before_days if args.before_days is not None else cfg.events.release_window_before_days
            after = args.after_days if args.after_days is not None else cfg.events.release_window_after_days
            result = store.prune_events(before_days=before, after_days=after,
                                        drop=args.drop, dry_run=args.dry_run)
            if args.json:
                out_json(result)
                return 0
            out(f"检查 {result['checked']} 张专辑, 发行日与展会不符的有 {result['mismatched']} 张")
            out(f"窗口: 展会前 {before} 天 ~ 展会后 {after} 天")
            for s in result["samples"][:20]:
                out(f"  [{s['event']:<14}] {s['release_date'] or '--':<12} {s['title'][:44]:<44} "
                    f"来源={','.join(s['sources'])}")
            if result["mismatched"] > 20:
                out(f"  ... 其余 {result['mismatched'] - 20} 张省略")
            if args.dry_run:
                out("(演练模式, 未改动。去掉 --dry-run 生效)")
            elif args.drop:
                out(f"已删除 {result['changed']} 张")
            else:
                out(f"已摘掉 {result['changed']} 张的展会标签(专辑仍保留, 可用 db delete 彻底删)")
            return 0

        if args.db_command == "unmatch":
            removed = 0
            blocked = 0
            failed: list[str] = []
            for key in args.keys:
                # 先把"当时匹配到的那条"读出来 —— 它的 album_id 与**分量**都要一起写进
                # 拉黑记录(每一次人工取消 = 一条带标签的负样本)。
                old = store.get_match(str(key))
                removed += int(store.delete_match(str(key)) or 0)
                ok = store.block_match(
                    str(key),
                    int(getattr(old, "target_album_id", 0) if old else 0),
                    target_name=getattr(old, "target_name", "") if old else "",
                    reason=str(getattr(args, "reason", "") or "cli"),
                    match=old,
                )
                if ok:
                    blocked += 1
                else:
                    failed.append(str(key))
            if args.json:
                out_json({"removed": removed, "blocked": blocked,
                          "keys": list(args.keys), "failed": failed})
            else:
                out(f"已取消 {removed} 条匹配记录, 并**拉黑 {blocked} 条候选**"
                    f"(粒度是「这张碟 × 那个 album_id」, 该碟仍会尝试其它候选)")
                if failed:
                    out(f"  有 {len(failed)} 个 key 没有形成有效拉黑(那条匹配缺 album_id): "
                        f"{', '.join(failed[:3])}")
                out("  要解禁用: dnh db rematch <key>")
            return 0

        if args.db_command == "rematch":
            if getattr(args, "list", False):
                rows = store.list_blocks()
                leftovers = store.legacy_rejection_rows()
                if args.json:
                    out_json({"blocks": rows, "legacy": leftovers})
                elif not rows and not leftovers:
                    out("没有被拉黑的候选 ✓")
                else:
                    out(f"被拉黑的候选: {len(rows)} 条(粒度 = 本地专辑 × 网易云 album_id)")
                    for row in rows:
                        out(f"  {row['album_key']}\n"
                            f"      拉黑 album_id={int(row['target_album_id'])}"
                            f"  {row['target_name'] or '(无名字)'}"
                            f"  score={float(row['score'] or 0):.3f}"
                            f"  method={row['method'] or '-'}")
                    if leftovers:
                        out(f"⚠ 另有 {len(leftovers)} 条**老格式**记录无法自动迁移"
                            f"(缺 album_id), 仍留在 netease_rejections:")
                        for row in leftovers[:5]:
                            out(f"      {row['album_key']}  {row.get('target_name') or ''}")
                return 0
            if not args.keys:
                out("需要至少一个专辑 key(或加 --list 查看当前被拉黑的候选)")
                return 2
            cleared = 0
            not_blocked: list[str] = []
            for key in args.keys:
                if store.clear_blocks(str(key)):
                    cleared += 1
                else:
                    not_blocked.append(str(key))
            if args.json:
                out_json({"cleared": cleared, "keys": list(args.keys),
                          "not_blocked": not_blocked})
            else:
                out(f"已清空 {cleared} 张专辑的黑名单(它们会重新参与**所有**候选的匹配)")
                if not_blocked:
                    out(f"  (其中 {len(not_blocked)} 个 key 本来就没有拉黑过任何候选)")
            return 0

        if args.db_command == "match":
            return _db_match_manual(cfg, args, store)
        if args.db_command == "ids":
            if not args.build:
                out("加 --build 才会执行分配")
                return 0
            counts = store.build_entity_ids()
            out("已分配/刷新实体 id: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
            _cloud_sync_after_write(Path(args.root or "."))
            return 0

        if args.db_command == "strip-source-links":
            from dnh.db import strip_source_links
            affected, removed = strip_source_links(
                store, apply=bool(args.apply))
            out(f"{'已清理' if args.apply else '可清理'} {removed} 条来源链接 "
                f"(涉及 {affected} 张专辑)")
            if args.apply:
                _cloud_sync_after_write(Path(args.root or "."))
            return 0

        if args.db_command == "export-payload":
            import json as _json
            from pathlib import Path as _Path
            rows = store.query(
                "SELECT key, payload FROM albums WHERE updated_at > ? ORDER BY updated_at ASC",
                (float(args.since or 0.0),))
            payloads = {str(row["key"]): str(row["payload"]) for row in rows}
            blob = _json.dumps(payloads, ensure_ascii=False)
            if args.out:
                _Path(args.out).write_text(blob, encoding="utf-8")
                out(f"已导出 {len(payloads)} 张 -> {args.out}")
            else:
                out(blob)
            return 0

        if args.db_command == "import-payload":
            import json as _json
            from pathlib import Path as _Path
            from dnh.models import Album as _Album
            payloads = _json.loads(_Path(args.file).read_text(encoding="utf-8"))
            total = ok_count = 0
            for key, payload in payloads.items():
                total += 1
                try:
                    album = _Album.from_json(payload)
                except (ValueError, TypeError) as exc:
                    out(f"  跳过 {key}: {exc}")
                    continue
                if not args.apply:
                    ok_count += 1
                    continue
                store._write_album(album, time.time())
                ok_count += 1
            out(f"{'已导入' if args.apply else '可导入'} {ok_count}/{total} 张专辑")
            if args.apply:
                _cloud_sync_after_write(Path(args.root or "."))
            return 0

        if args.db_command == "clean-tags":
            stats = store.clean_noise_tags(dry_run=bool(getattr(args, "dry_run", False)))
            if getattr(args, "json", False):
                out_json({"dry_run": bool(getattr(args, "dry_run", False)), **stats})
                return 0
            suffix = "（演练模式，未写库；确认后去掉 --dry-run）" if stats and getattr(args, "dry_run", False) else ""
            out(f"处理 {stats.get('albums', 0)} 张专辑 · "
                f"删掉 {stats.get('removed', 0)} 条 / {stats.get('kinds', 0)} 种标签 {suffix}".rstrip())
            return 0

        if args.db_command == "dedupe":
            groups = store.duplicate_groups(threshold=args.threshold, limit=args.limit)
            extra = sum(group["count"] - 1 for group in groups)
            if args.json and not args.apply:
                out_json({"groups": groups, "duplicates": extra, "dry_run": True})
                return 0
            if not groups:
                out("没有发现重复专辑 ✓")
                return 0
            out(f"发现 {len(groups)} 组疑似重复, 共 {extra} 条多余记录(阈值 {args.threshold})")
            for group in groups:
                out(f"  [{group['count']} 条] {group['title'][:44]}   社团={group['circle'][:20]}")
                for key, sources in zip(group["keys"], group["sources"]):
                    flag = "*" if key == group["keep"] else " "
                    out(f"     {flag} {key[:66]:<66} {','.join(sources)}")
            if not args.apply:
                out("(演练模式, 未改动。确认后加 --apply 真合并, 每组保留 * 那条)")
                return 0
            merged = 0
            for group in groups:
                keep_key = group["keep"]
                for key in group["keys"]:
                    if key != keep_key and store.merge_albums(keep_key, key):
                        merged += 1
            out(f"已合并 {merged} 条重复记录, 剩下 {len(groups)} 条; 用 `dnh db stats` 看总数")
            if args.json:
                out_json({"merged": merged, "groups": len(groups)})
            return 0

        if args.db_command == "delete":
            keys = list(args.key or [])
            filtered = any([args.event, args.tag, args.source, args.query, args.mark,
                            args.unmarked, args.unmatched])
            if not keys and not filtered:
                print("需要 --key 或至少一个筛选条件(否则会误删全库)", file=sys.stderr)
                return 2
            if not args.dry_run and not args.yes:
                print("需要 --yes 确认(或用 --dry-run 先看)", file=sys.stderr)
                return 2
            deleted = list(keys)
            if filtered:
                deleted += store.delete_albums_where(
                    event=args.event or None, tag=args.tag or None, source=args.source or None,
                    query=args.query or None, mark=args.mark or None,
                    only_unmarked=args.unmarked, only_unmatched=args.unmatched,
                    dry_run=True,
                )
            deleted = list(dict.fromkeys(deleted))
            if not args.dry_run:
                store.delete_albums(deleted)
            if args.json:
                out_json({"count": len(deleted), "keys": deleted, "dry_run": args.dry_run})
                return 0
            out(f"{'将删除' if args.dry_run else '已删除'} {len(deleted)} 张专辑")
            for key in deleted[:25]:
                album = store.get_album(key)
                out(f"  {key}")
            if len(deleted) > 25:
                out(f"  ... 其余 {len(deleted) - 25} 张省略")
            return 0

        if args.db_command == "mark":
            if args.list or not args.key:
                rows = store.mark_counts()
                if args.json:
                    out_json([{"mark": m, "count": n} for m, n in rows])
                    return 0
                if not rows:
                    out("(还没有任何标记)")
                for m, n in rows:
                    out(f"  {m:<20} {n}")
                if not args.key:
                    return 0
            count = 0
            for key in args.key:
                if store.get_album(key) is None:
                    print(f"跳过不存在的 key: {key}", file=sys.stderr)
                    continue
                if args.remove:
                    store.remove_mark(key, args.mark)
                else:
                    store.add_mark(key, args.mark, args.note)
                count += 1
            verb = "移除" if args.remove else "打上"
            out(f"已为 {count} 张专辑{verb}标记「{args.mark}」")
            return 0
    finally:
        store.close()
    return 2


def cmd_cache(cfg: Config, args: argparse.Namespace) -> int:
    from .cache import HttpCache

    cache = HttpCache(cfg.cache_dir / "http_cache.sqlite3")
    try:
        if args.cache_command in (None, "stats"):
            stats = cache.stats()
            if args.json:
                out_json(stats)
                return 0
            out(f"缓存条目: {stats['entries']}  体积: {_human_size(stats['bytes'])}")
            if stats["newest"]:
                out(f"  最早: {time.strftime('%Y-%m-%d %H:%M', time.localtime(stats['oldest']))}")
                out(f"  最新: {time.strftime('%Y-%m-%d %H:%M', time.localtime(stats['newest']))}")
            return 0
        if args.cache_command == "clear":
            if args.vacuum:
                n = cache.vacuum_old(30)
                out(f"已清理 30 天前的 {n} 条缓存")
            else:
                n = cache.invalidate(args.url)
                out(f"已清理 {n} 条缓存")
            return 0
    finally:
        cache.close()
    return 2


def cmd_public(cfg: Config, args: argparse.Namespace) -> int:
    """启动面向公众的只读站点.

    围绕专辑数据库提供浏览/搜索/详情/筛选/看板; 登录后**普通用户只读、
    认证编辑者可编辑**。它是**独立站点**: 与任何后台管理面板没有跳转与文案关联,
    也不暴露数据是怎么采集来的。
    """
    from .public.site import build_site, make_server

    if getattr(args, "no_session_persist", False):
        cfg.public.sessions_db = ""
    site = build_site(
        cfg,
        users_db=getattr(args, "users_db", None),
        sessions_db=getattr(args, "sessions_db", None),
        base_url=getattr(args, "base_url", "") or None,
    )
    httpd = make_server(site, host=args.host, port=args.port)
    host, port = httpd.server_address[0], httpd.server_address[1]
    out(f"用户端站点已启动: http://{host}:{port}/")
    out("（浏览免登录; 登录后普通用户只读、认证编辑者可编辑）")
    out(f"  会话: {'落盘 ' + str(site.sessions.store.path) if site.sessions.persistent else '内存(重启会掉线)'}")
    out(f"  限速: {'开启' if site.limiter.enabled else '关闭'}"
        f" · robots.txt / sitemap.xml 已就绪 · 爬取合规: 见 `dnh robots --audit`")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        out("已停止")
    finally:
        httpd.server_close()
        site.close()
    return 0


def bind_warning(host: str) -> str:
    """站长端绑定非本地地址时的醒目警告(空串 = 不必警告).

    站长端带着 VGMdb 的 ``cf_clearance``、能直接写专辑库, 而且**没有**公开端那套
    限速 / robots / 反代加固 —— 它只应该出现在 SSH 隧道里。
    """
    text = str(host or "").strip().lower()
    if text in ("", "127.0.0.1", "localhost", "::1"):
        return ""
    return (
        f"警告: 站长端绑定了 {host} —— 这不是本地地址!\n"
        "  站长端带着 cf_clearance、能直接写专辑库, 且没有公开端的限速/反代加固。\n"
        "  正确用法是 SSH 隧道:\n"
        "    ssh -L 8787:127.0.0.1:8787 <user>@<vps>   然后本机访问 http://127.0.0.1:8787/\n"
        "  要对外提供浏览服务请用 `dnh public`(公开端), 不要把站长端暴露到公网。"
    )


# --------------------------------------------------------------------------
# robots 合规检查(只读)
# --------------------------------------------------------------------------


_STATUS_TEXT = {
    0: "取不到(网络/代理故障)",
    200: "可读",
    401: "不可用 -> 不限制",
    403: "不可用 -> 不限制",
    404: "不存在 -> 不限制",
    410: "已删除 -> 不限制",
    429: "限流 -> 暂假设禁止",
    500: "服务端错误 -> 暂假设禁止",
    503: "服务不可用 -> 暂假设禁止",
}


def _robots_status_text(status: int) -> str:
    return _STATUS_TEXT.get(int(status), f"HTTP {status}")


def _host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return ""


def cmd_robots(cfg: Config, args: argparse.Namespace) -> int:
    """``dnh robots <url>`` / ``dnh robots --audit`` —— 只读, 不发实际采集请求."""
    from .httpclient import HttpClient

    client = HttpClient(cfg, logger=LOG)
    try:
        if args.audit or not args.url:
            return _robots_audit(cfg, client, args)
        url = str(args.url)
        if "://" not in url:
            url = "https://" + url
        report = client.robots_report(
            url, refresh=bool(args.refresh), allow_fetch=not bool(args.offline)
        )
        if args.json:
            out_json(report)
            return 0
        out(f"URL: {report['url']}")
        out(f"host: {report['host']}   路径: {report['path']}")
        out(f"robots.txt: {report['robots_url'] or '(无)'}")
        out(f"robots 状态: "
            f"{'豁免(未检查)' if report['exempt'] else _robots_status_text(int(report['http_status']))}"
            f"  HTTP {report['http_status']}")
        out(f"判定: {'允许' if report['allowed'] else '不允许'}"
            f"{'  (命中 ' + report['rule'] + ')' if report['rule'] else ''}")
        if report["exempt"]:
            out(f"豁免: 是 —— {report['exempt_reason']}")
        else:
            out("豁免: 否")
        out(f"Crawl-delay: {report['crawl_delay']:.1f}s"
            f"   实际最小间隔: {report['min_interval']:.1f}s"
            f"   (rate_limit={float(cfg.http.rate_limit):.1f}s, UA={client.identity_user_agent()})")
        out(f"缓存: {'内存/落盘命中' if report['cached'] else '本次现取'}"
            f"   说明: {report['reason']}")
        if report["rules"]:
            out("适用规则:")
            for rule in report["rules"]:
                out(f"    {'Allow' if rule['allow'] else 'Disallow'}: {rule['pattern']}")
        return 0
    finally:
        client.close()


def _robots_audit(cfg: Config, client: Any, args: argparse.Namespace) -> int:
    """各数据源: host + 最小请求间隔 + robots 状态(一眼看清我们的请求频率与许可)."""
    from .sources import describe, make_context

    rows: list[dict[str, Any]] = []
    try:
        ctx = make_context(cfg, client, logger=LOG)
        entries = describe(ctx)
    except Exception as exc:  # 数据源注册表坏了也不该让检查命令失败
        LOG.debug("数据源概览失败: %s", exc)
        entries = []

    for entry in entries:
        homepage = str(entry.get("homepage") or "")
        host = _host_of(homepage)
        row: dict[str, Any] = {
            "source": entry.get("name") or "",
            "label": entry.get("label") or "",
            "enabled": bool(entry.get("enabled")),
            "host": host,
            "homepage": homepage,
            "rate_limit": float(cfg.http.rate_limit or 0.0),
        }
        if not host:
            row.update({"robots": "unknown(源未声明 homepage)", "allowed": None,
                        "crawl_delay": 0.0, "min_interval": float(cfg.http.rate_limit or 0.0),
                        "exempt": False})
            rows.append(row)
            continue
        report = client.robots_report(
            homepage if "://" in homepage else f"https://{host}/",
            refresh=bool(args.refresh),
            allow_fetch=not bool(args.offline),
        )
        row.update({
            "host": report["host"],
            "robots": (
                "豁免(不检查)" if report["exempt"]
                else ("离线且无缓存" if not report["fetched"]
                      else _robots_status_text(int(report["http_status"])))
            ),
            "allowed": bool(report["allowed"]),
            "crawl_delay": float(report["crawl_delay"]),
            "min_interval": float(report["min_interval"]),
            "exempt": bool(report["exempt"]),
            "exempt_reason": report["exempt_reason"],
            "rules": len(report["rules"]),
        })
        rows.append(row)

    if args.json:
        out_json(rows)
        return 0

    if not rows:
        out("(没有可检查的数据源)")
        return 0
    out(f"抓取节流汇总  rate_limit={float(cfg.http.rate_limit):.1f}s  "
        f"respect_robots={bool(cfg.http.respect_robots)}")
    out("-" * 88)
    out(f"{'数据源':<14}{'启用':<6}{'host':<22}{'最小间隔':<10}{'Crawl-delay':<12}{'robots 状态':<22}许可")
    for row in rows:
        allowed = row.get("allowed")
        verdict = "—" if allowed is None else ("允许" if allowed else "不允许")
        if row.get("exempt"):
            verdict += "(豁免)"
        host = row.get("host") or "-"
        out(
            f"{row['source']:<14}{'是' if row['enabled'] else '否':<6}"
            f"{host[:20]:<22}{row['min_interval']:>6.1f}s   "
            f"{row['crawl_delay']:>6.1f}s     {row['robots'][:20]:<22}{verdict}"
        )
    out("-" * 88)
    out("提示: 豁免的 host 每次请求都会在日志里打 warning; "
        "想彻底合规请清空 [http] robots_override_hosts 并把各源的 respect_robots 设为 true。")


# --------------------------------------------------------------------------
# 数据库备份(WAL 安全)
# --------------------------------------------------------------------------


def backup_database(src: Path | str, out_path: Path | str, *, force: bool = False) -> dict[str, Any]:
    """用 sqlite3 的 ``Connection.backup`` 在线备份.

    为什么不是直接复制文件: 库跑在 WAL 模式下, 主文件里可能缺少还没 checkpoint
    的事务, 单独复制 ``dnh.sqlite3`` 会得到一个**看似正常实则撕裂**的库
    (旧的 ``-wal`` 也带不走)。``backup`` API 由 SQLite 保证一致性快照。
    """
    source = Path(src)
    target = Path(out_path)
    if not source.is_file():
        raise DnhError(f"数据库不存在: {source}")
    if target.exists():
        if not force:
            raise DnhError(f"目标文件已存在: {target} (加 --force 覆盖, 或换 --out)")
        # 覆盖时先把旧文件删掉: 它可能根本不是 SQLite 库(残留的半个文件),
        # 直接往里 backup 会得到 "file is not a database"。
        try:
            target.unlink()
        except OSError as exc:
            raise DnhError(f"删不掉已存在的目标文件 {target}: {exc}") from exc
    target.parent.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    src_conn = sqlite3.connect(str(source), timeout=30.0)
    dst_conn = sqlite3.connect(str(target), timeout=30.0)
    try:
        src_conn.backup(dst_conn)
        dst_conn.commit()
        integrity = "unknown"
        try:
            row = dst_conn.execute("PRAGMA integrity_check").fetchone()
            integrity = str(row[0]) if row else "unknown"
        except sqlite3.Error:
            pass
        albums = -1
        try:
            row = dst_conn.execute("SELECT COUNT(*) FROM albums").fetchone()
            albums = int(row[0]) if row else -1
        except sqlite3.Error:
            albums = -1
    finally:
        dst_conn.close()
        src_conn.close()
    elapsed = time.monotonic() - started
    return {
        "source": str(source),
        "out": str(target),
        "bytes": target.stat().st_size if target.exists() else 0,
        "seconds": round(elapsed, 3),
        "albums": albums,
        "integrity": integrity,
    }


def _cmd_db_backup(cfg: Config, args: argparse.Namespace) -> int:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_path = Path(args.out) if args.out else (cfg.data_dir / "backups" / f"dnh-{stamp}.sqlite3")
    info = backup_database(cfg.db_path, out_path, force=bool(getattr(args, "force", False)))
    if args.json:
        out_json(info)
        return 0
    out(f"已备份: {info['out']}")
    out(f"  来源: {info['source']}")
    out(f"  大小: {_human_size(info['bytes'])}   耗时: {info['seconds']:.2f}s")
    if info["albums"] >= 0:
        out(f"  专辑数: {info['albums']}   integrity_check: {info['integrity']}")
    out("  恢复: 停掉服务后把备份文件复制成 data/dnh.sqlite3(WAL 下同时删掉 -wal/-shm)")
    return 0


def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    from .webui.server import serve

    warning = bind_warning(args.host)
    if warning:
        out(warning)
        out("")
    url = f"http://{args.host}:{args.port}/"
    out("启动本地面板: " + url)
    out("(Ctrl+C 退出)")
    if args.open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    serve(cfg, host=args.host, port=args.port, logger=LOG)
    return 0


def _progress(kind: str, **data: Any) -> None:
    if kind == "source_start":
        LOG.debug("采集 %s @ %s", data.get("source"), data.get("event"))


def _split(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

_HANDLERS = {
    "init": cmd_init,
    "doctor": cmd_doctor,
    "covers": cmd_covers,
    "bilibili": cmd_bilibili,
    "events": cmd_events,
    "sources": cmd_sources,
    "discover": cmd_discover,
    "enrich": cmd_enrich,
    "match": cmd_match,
    "run": cmd_run,
    "playlist": cmd_playlist,
    "report": cmd_report,
    "db": cmd_db,
    "netease": cmd_netease,
    "cache": cmd_cache,
    "robots": cmd_robots,
    "public": cmd_public,
    "serve": cmd_serve,
}


# 全局选项: 出现在子命令之后时会被搬到子命令之前, 这样两种写法都能用:
#   dnh --log-level debug discover ...  ==  dnh discover --log-level debug ...
_GLOBAL_VALUE_FLAGS = ("--config", "-c", "--root", "--log-level")
_GLOBAL_BOOL_FLAGS = ("--quiet", "-q", "--json")


def normalize_argv(argv: Sequence[str]) -> list[str]:
    """把子命令之后的全局选项搬到最前面, 让 argparse 能识别."""
    head: list[str] = []
    rest: list[str] = []
    items = list(argv)
    i = 0
    while i < len(items):
        arg = items[i]
        if arg in _GLOBAL_VALUE_FLAGS:
            if i + 1 < len(items):
                head.extend([arg, items[i + 1]])
                i += 2
                continue
            rest.append(arg)
            i += 1
            continue
        if any(arg.startswith(flag + "=") for flag in _GLOBAL_VALUE_FLAGS if flag.startswith("--")):
            head.append(arg)
            i += 1
            continue
        if arg in _GLOBAL_BOOL_FLAGS:
            head.append(arg)
            i += 1
            continue
        rest.append(arg)
        i += 1
    return head + rest



def _cloud_sync_after_write(root: Path, logger: Any = None) -> bool:
    """CLI 写完库后把改动推到云端(后台执行, 不阻塞命令).

    开关: ``data/.cloud_sync_enabled`` 存在才生效; 与站长端保存时用的是同一个开关。
    """
    import subprocess
    import sys as _sys
    import time

    try:
        root = Path(root)
        flag = root / "data" / ".cloud_sync_enabled"
        if not flag.exists():
            return False
        script = root / "tools" / "sync_cloud.py"
        if not script.is_file():
            return False
        stamp_file = root / "data" / ".cli_sync_last"
        try:
            last = float(stamp_file.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            last = 0.0
        now = time.time()
        if now - last < 30.0:                  # 30 秒内不重复触发
            return False
        stamp_file.write_text(str(now), encoding="utf-8")
        flags = 0
        if _sys.platform == "win32":
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([_sys.executable, str(script)], cwd=str(root),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=flags, close_fds=True)
        if logger is not None:
            logger.info("已在后台触发云端同步")
        return True
    except Exception:                          # noqa: BLE001 - 同步失败不该影响主命令
        return False



def main(argv: Sequence[str] | None = None) -> int:
    _setup_console()
    raw = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(normalize_argv(raw))
    if not args.command:
        parser.print_help()
        return 0

    cfg = _load_config(args)
    _setup_logging(args.log_level or cfg.general.log_level, quiet=args.quiet)
    handler = _HANDLERS.get(args.command)
    if handler is None:
        parser.print_help()
        return 2
    try:
        return int(handler(cfg, args) or 0)
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except (DnhError, NeteaseApiError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
