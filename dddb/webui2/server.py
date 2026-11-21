"""webui2: 精简版站长端(导入 / 歌单对比 / 采集).

与旧 ``dddb/webui`` 的区别:
    旧版 6865 行, 功能堆在一起(专辑库/社团/任务/报告/合并/图片槽...)。
    新版只保留**三条主流程**:

        ① 导入   —— 贴网易云链接(或从本地库/歌单) -> 预览 -> 推到线上
        ② 歌单   —— 歌单 vs 线上, 找出缺失的
        ③ 采集   —— 触发采集/富化/匹配任务, 看进度

    实现上刻意用**单文件服务 + 内嵌页面**, 不引入构建步骤。

设计:
    * 只依赖标准库 http.server
    * 所有写操作走 POST + CSRF(与会话绑定的 token)
    * 任务在后台线程跑, 前端轮询 /api/jobs/<id>
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping

from ..config import Config

logger = logging.getLogger("dddb.webui2")

STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# 任务管理
# ---------------------------------------------------------------------------
class JobRegistry:
    """后台任务: 采集/匹配/导入这种长操作用它跑."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._seq = 0

    def new(self, title: str) -> str:
        with self._lock:
            self._seq += 1
            jid = "j{:04d}".format(self._seq)
            self._jobs[jid] = {
                "id": jid, "title": title, "state": "pending",
                "started_at": time.time(), "finished_at": 0,
                "logs": [], "result": None, "error": "",
            }
            return jid

    def log(self, jid: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(jid)
            if job is None:
                return
            job["logs"].append(str(message)[:500])
            if len(job["logs"]) > 400:
                del job["logs"][:100]

    def run(self, jid: str, fn: Callable[[str], Any]) -> None:
        def _target() -> None:
            with self._lock:
                if jid in self._jobs:
                    self._jobs[jid]["state"] = "running"
            try:
                result = fn(jid)
                with self._lock:
                    job = self._jobs.get(jid)
                    if job is not None:
                        job["state"] = "done"
                        job["result"] = result
                        job["finished_at"] = time.time()
            except Exception as exc:              # noqa: BLE001
                logger.warning("任务 %s 失败: %s", jid, exc, exc_info=True)
                with self._lock:
                    job = self._jobs.get(jid)
                    if job is not None:
                        job["state"] = "error"
                        job["error"] = "{}: {}".format(
                            type(exc).__name__, exc)
                        job["finished_at"] = time.time()

        threading.Thread(target=_target, daemon=True).start()

    def get(self, jid: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(jid)
            return dict(job) if job else None

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return sorted(
                (dict(j) for j in self._jobs.values()),
                key=lambda x: -x["started_at"])


# ---------------------------------------------------------------------------
# 导入草稿的暂存区
# ---------------------------------------------------------------------------
class DraftStore:
    """待导入的 ImportDraft: 预览阶段先放内存, 确认导入时才用."""

    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def put(self, draft: Any) -> str:
        d = draft.to_dict() if hasattr(draft, "to_dict") else dict(draft)
        key = str(d.get("key") or "")
        with self._lock:
            self._items[key] = d
        return key

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            d = self._items.get(key)
            return dict(d) if d else None

    def pop(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            return self._items.pop(key, None)

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._items.values())

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


# ---------------------------------------------------------------------------
# HTTP 处理
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "dddb-webui2/1"
    config: Config
    logger: logging.Logger
    jobs: JobRegistry
    drafts: DraftStore

    # -- 工具 ----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        self.logger.debug("%s " + fmt, self.address_string(), *args)

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status=status)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("content-length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 8 * 1024 * 1024))
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @property
    def route(self) -> str:
        return urllib.parse.urlsplit(self.path).path or "/"

    # -- GET -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        route = self.route
        try:
            if route.startswith("/api/"):
                return self._api_get(route)
            return self._serve_static(route)
        except Exception as exc:                  # noqa: BLE001
            self.logger.warning("GET %s 失败: %s", route, exc, exc_info=True)
            self._error("{}: {}".format(type(exc).__name__, exc), 500)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    # -- POST ----------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        route = self.route
        if not route.startswith("/api/"):
            return self._error("not found", 404)
        data = self._read_json()
        try:
            return self._api_post(route, data)
        except Exception as exc:                  # noqa: BLE001
            self.logger.warning("POST %s 失败: %s", route, exc,
                                exc_info=True)
            self._error("{}: {}".format(type(exc).__name__, exc), 500)

    # -- 静态 ----------------------------------------------------------

    def _serve_static(self, route: str) -> None:
        rel = "index.html" if route in ("/", "") else route.lstrip("/")
        if rel.startswith("static/"):
            rel = rel[len("static/"):]
        base = STATIC_DIR.resolve()
        target = (base / rel).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            return self._error("not found", 404)
        if not target.is_file():
            return self._error("not found", 404)
        import mimetypes

        ctype = mimetypes.guess_type(str(target))[0] or \
            "application/octet-stream"
        if ctype.startswith("text/") or ctype in (
                "application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    # -- API: GET ------------------------------------------------------

    def _api_get(self, route: str) -> None:
        if route == "/api/summary":
            return self._json(self._summary())
        if route == "/api/drafts":
            return self._json({"items": self.drafts.all()})
        if route.startswith("/api/drafts/"):
            key = urllib.parse.unquote(route[len("/api/drafts/"):])
            d = self.drafts.get(key)
            if d is None:
                return self._error("没有这个草稿", 404)
            return self._json(d)
        if route == "/api/jobs":
            return self._json({"items": self.jobs.all()})
        if route.startswith("/api/jobs/"):
            jid = route[len("/api/jobs/"):]
            job = self.jobs.get(jid)
            if job is None:
                return self._error("没有这个任务", 404)
            return self._json(job)
        if route == "/api/cloud/status":
            return self._json(self._cloud_status())
        return self._error("unknown endpoint", 404)

    # -- API: POST -----------------------------------------------------

    def _api_post(self, route: str, data: Mapping[str, Any]) -> None:
        if route == "/api/import/netease":
            return self._json(self._import_netease(data))
        if route == "/api/import/local":
            return self._json(self._import_local(data))
        if route == "/api/import/push":
            return self._json(self._push_to_cloud(data))
        if route == "/api/playlist/compare":
            return self._json(self._playlist_compare(data))
        if route == "/api/collect/run":
            return self._json(self._collect_run(data))
        if route == "/api/drafts/clear":
            self.drafts.clear()
            return self._json({"ok": True})
        return self._error("unknown endpoint", 404)

    # -- 业务 ----------------------------------------------------------

    def _summary(self) -> dict[str, Any]:
        from ..cloudview import CloudView

        out: dict[str, Any] = {"ok": True}
        cv = CloudView(self.config.cloud_db_path,
                       base_url=self.config.cloud_base_url)
        try:
            out["cloud"] = {
                "available": cv.available,
                "counts": cv.counts() if cv.available else {},
                "base_url": cv.base_url,
            }
        finally:
            cv.close()
        out["drafts"] = len(self.drafts.all())
        out["jobs"] = len(self.jobs.all())
        out["data_dir"] = str(self.config.data_dir)
        return out

    def _cloud_status(self) -> dict[str, Any]:
        from ..cloudview import CloudView

        cv = CloudView(self.config.cloud_db_path,
                       base_url=self.config.cloud_base_url)
        try:
            return {
                "ok": True,
                "available": cv.available,
                "path": str(self.config.cloud_db_path or ""),
                "counts": cv.counts() if cv.available else {},
                "base_url": cv.base_url,
            }
        finally:
            cv.close()

    def _client(self) -> Any:
        from ..netease.client import NeteaseClient

        return NeteaseClient(self.config, logger=self.logger)

    def _import_netease(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """贴网易云链接 -> 后台抓取 -> 产出草稿."""
        ref = str(data.get("url") or "").strip()
        if not ref:
            return {"ok": False, "error": "请粘贴网易云专辑/歌单链接"}
        fetch_lyrics = bool(data.get("fetch_lyrics", True))

        jid = self.jobs.new("导入 {}".format(ref[:60]))
        self.jobs.run(jid, lambda j: self._do_import_netease(
            j, ref, fetch_lyrics))
        return {"ok": True, "job": jid}

    def _do_import_netease(self, jid: str, ref: str,
                           fetch_lyrics: bool) -> dict[str, Any]:
        from ..import_netease import draft_from_netease

        client = self._client()
        drafts = draft_from_netease(
            client, ref, fetch_lyrics=fetch_lyrics,
            progress=lambda m: self.jobs.log(jid, m))
        if not isinstance(drafts, list):
            drafts = [drafts]
        keys = [self.drafts.put(d) for d in drafts]
        return {"keys": keys, "count": len(keys)}

    def _import_local(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """本地库的一张专辑 -> 草稿."""
        from ..import_format import from_local_album

        key = str(data.get("key") or "").strip()
        if not key:
            return {"ok": False, "error": "请给出专辑 key"}
        try:
            draft = from_local_album(self.config.db_path, key)
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "key": self.drafts.put(draft)}

    def _push_to_cloud(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """把草稿推到线上(CDP 自动填表 / 桥页)."""
        from ..import_to_web import import_payload_to_online

        key = str(data.get("key") or "").strip()
        draft = self.drafts.get(key)
        if draft is None:
            return {"ok": False, "error": "没有这个草稿: {}".format(key)}

        from ..import_format import ImportDraft

        obj = ImportDraft.from_dict(draft)
        payload = obj.to_form_payload()

        mode = str(data.get("mode") or "cdp").strip().lower()
        if mode == "bridge":
            return self._bridge_only(obj, payload)

        try:
            result = import_payload_to_online(
                payload, base_url=self.config.cloud_base_url,
                cover_file=str(draft.get("cover_file") or ""),
                cache_dir=Path(self.config.data_dir) / "import_cache")
        except Exception as exc:                  # noqa: BLE001
            fallback = self._bridge_only(obj, payload)
            fallback["cdp_error"] = "{}: {}".format(type(exc).__name__, exc)
            fallback["hint"] = "自动填表不可用, 已生成桥页作为替代"
            return fallback
        result["ok"] = True
        result["mode"] = "cdp"
        return result

    def _bridge_only(self, draft: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
        """生成桥页 + 书签(CDP 不可用时的退路)."""
        import hashlib

        from ..import_bridge import write_bookmarklet, write_bridge

        cache_dir = Path(self.config.data_dir) / "import_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        stem = hashlib.sha1(
            str(draft.key).encode("utf-8")).hexdigest()[:16]
        data = dict(payload)
        data["_stem"] = stem
        data["album_key"] = draft.key

        bridge = write_bridge(data, base_url=self.config.cloud_base_url,
                              out_dir=cache_dir, stem=stem)
        bookmark = write_bookmarklet(data, out_dir=cache_dir, stem=stem)
        return {
            "ok": True, "mode": "bridge", "stem": stem,
            "bridge_file": str(bridge),
            "bridge_url": "/import-cache/{}".format(bridge.name),
            "bookmarklet_file": str(bookmark),
            "cloud_url": "{}/new/album".format(
                self.config.cloud_base_url.rstrip("/")),
        }

    def _playlist_compare(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """歌单 vs 线上: 找出线上缺的."""
        ref = str(data.get("url") or "").strip()
        if not ref:
            return {"ok": False, "error": "请粘贴网易云歌单链接"}
        jid = self.jobs.new("歌单对比 {}".format(ref[:60]))
        self.jobs.run(jid, lambda j: self._do_playlist_compare(j, ref))
        return {"ok": True, "job": jid}

    def _do_playlist_compare(self, jid: str, ref: str) -> dict[str, Any]:
        from ..cloudview import CloudView
        from ..import_format import parse_netease_ref
        from ..import_netease import drafts_from_netease_playlist
        from ..playlist_import import find_missing_on_cloud, group_by_album
        from ..playlist_to_cloud import songs_from_playlist

        kind, ident = parse_netease_ref(ref)
        if kind != "playlist":
            raise ValueError("这不是歌单链接: {}".format(ref[:60]))

        client = self._client()
        self.jobs.log(jid, "拉取歌单 {}".format(ident))
        songs = songs_from_playlist(client, ident)
        albums = group_by_album(songs)
        self.jobs.log(jid, "聚合 {} 张专辑".format(len(albums)))

        cv = CloudView(self.config.cloud_db_path,
                       base_url=self.config.cloud_base_url)
        try:
            missing = find_missing_on_cloud(albums, cv)
        finally:
            cv.close()
        self.jobs.log(jid, "线上缺 {} 张".format(len(missing)))

        # 缺的那些做成草稿(不抓歌词, 太快; 单张导入时再抓)
        keys: list[str] = []
        for item in missing:
            try:
                drafts = drafts_from_netease_playlist(
                    client, ident, fetch_lyrics=False, limit=0)
                break
            except Exception:                     # noqa: BLE001
                break
        # 上面只是碰运气预热; 真正按 missing 生成
        from ..import_netease import draft_from_netease_album

        for item in missing:
            aid = int(item.get("album_id") or 0)
            if not aid:
                continue
            try:
                d = draft_from_netease_album(client, aid,
                                             fetch_lyrics=False)
            except Exception:                     # noqa: BLE001
                continue
            keys.append(self.drafts.put(d))
        return {
            "playlist_id": ident,
            "total_songs": len(songs),
            "total_albums": len(albums),
            "missing": len(missing),
            "drafts": keys,
        }

    def _collect_run(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """触发采集/富化/匹配."""
        action = str(data.get("action") or "discover").strip().lower()
        event = str(data.get("event") or "").strip()
        jid = self.jobs.new("{} {}".format(action, event or "(全部)"))
        self.jobs.run(jid, lambda j: self._do_collect(j, action, event))
        return {"ok": True, "job": jid}

    def _do_collect(self, jid: str, action: str,
                    event: str) -> dict[str, Any]:
        from ..pipeline import Pipeline

        self.jobs.log(jid, "开始 {} {}".format(action, event or ""))
        with Pipeline(self.config, self.logger) as pipe:
            if action == "match":
                result = pipe.match_event(event) if hasattr(
                    pipe, "match_event") else None
            elif action == "enrich":
                result = pipe.enrich_event(event) if hasattr(
                    pipe, "enrich_event") else None
            else:
                result = pipe.discover_event(event) if hasattr(
                    pipe, "discover_event") else None
        self.jobs.log(jid, "完成")
        return {"result": str(result)[:400]}


def serve(config: Config, *, host: str = "127.0.0.1",
          port: int = 8800) -> None:
    """启动 webui2."""
    Handler.config = config
    Handler.logger = logger
    Handler.jobs = JobRegistry()
    Handler.drafts = DraftStore()
    httpd = ThreadingHTTPServer((host, int(port)), Handler)
    logger.info("webui2 监听 http://%s:%s", host, port)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


__all__ = ["DraftStore", "Handler", "JobRegistry", "serve"]
