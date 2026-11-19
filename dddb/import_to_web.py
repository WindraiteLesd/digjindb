"""站长端「一键导入线上」: 导出字段到缓存 + 打开浏览器自动填表.

用户要求:
    "站长端导出所有字段至缓存并自动打开新建专辑页面, 然后直接从缓存中
     填入对应的表单内容, 包括封面, 不动线上的任何代码不做接口,
     可以做成本地自动化脚本工具"

设计:
    ① 从本地库读出一张专辑的**全部**字段(含曲目/词条/外链/封面)
    ② 写成一份 JSON 缓存(``data/import_cache/<key>.json``), 封面单独
       落到 ``data/import_cache/<key>.cover.<ext>``
    ③ 用 Chrome/Edge 的**远程调试**(CDP)驱动浏览器:
         - 打开线上新建页
         - 用 JS 把缓存里的值填进表单(含 file input 用 DOM.setFileInputFiles)
         - **不自动提交** —— 留给人工核对后点保存
    ④ 不修改线上任何代码, 不调任何线上接口

依赖: 只用标准库 + websocket 手写(CDP 的 ws 协议很简单)。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

#: 缓存目录
CACHE_DIR_NAME = "import_cache"


# ---------------------------------------------------------------------------
# ① 从本地库导出字段
# ---------------------------------------------------------------------------

def export_album(db_path: Path, album_key: str,
                 cache_dir: Path, *,
                 base_url: str = "") -> dict[str, Any]:
    """把一张专辑的全部字段导出成缓存文件(含封面路径).

    返回缓存 dict; 同时写出 ``<hash>.json`` 与 ``<hash>.cover.<ext>``。
    """
    import sqlite3

    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError("本地库不存在: {}".format(db_path))

    conn = sqlite3.connect("file:{}?mode=ro".format(db_path), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT key, payload FROM albums WHERE key = ?",
                           (album_key,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise KeyError("本地库里没有这张专辑: {}".format(album_key))

    try:
        payload = json.loads(row["payload"] or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    data = _payload_to_form(payload, album_key)

    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    stem = hashlib.sha1(album_key.encode("utf-8")).hexdigest()[:16]

    # 封面: 复制到缓存目录(浏览器需要一个真实文件路径)
    cover_path = _resolve_cover(payload, db_path, album_key)
    cover_out = ""
    if cover_path.startswith("url:"):
        # 远程封面: 下载到缓存
        dst = cache_dir / "{}.cover.jpg".format(stem)
        cover_out = _download_cover(cover_path[4:], dst)
    elif cover_path and Path(cover_path).is_file():
        ext = Path(cover_path).suffix or ".jpg"
        dst = cache_dir / "{}.cover{}".format(stem, ext)
        dst.write_bytes(Path(cover_path).read_bytes())
        cover_out = str(dst)
    if cover_out:
        data["cover_file"] = cover_out

    data["album_key"] = album_key
    data["exported_at"] = time.time()
    json_path = cache_dir / "{}.json".format(stem)
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    data["_cache_json"] = str(json_path)
    data["_stem"] = stem

    # 同时产出**桥页 + 书签** —— 浏览器自动化在受限环境不可用时的正路
    try:
        from .import_bridge import write_bookmarklet, write_bridge

        base = str(base_url or "https://www.digjindb.top")
        data["_bridge_html"] = str(write_bridge(
            data, base_url=base, out_dir=cache_dir, stem=stem))
        data["_bookmarklet"] = str(write_bookmarklet(
            data, out_dir=cache_dir, stem=stem))
    except Exception:                       # noqa: BLE001 - 桥页非必需
        data["_bridge_html"] = ""
        data["_bookmarklet"] = ""
    return data


def _cover_slug(album_key: str) -> str:
    """专辑 key -> covers/ 里的文件名主干(与站点一致)."""
    import re

    slug = str(album_key or "").lower()
    slug = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", slug)
    return slug.strip("-")[:120]


def _resolve_cover(payload: dict[str, Any], db_path: Path,
                   album_key: str = "") -> str:
    """找封面文件. 依次尝试: cover_local -> covers/ 里按 key 找 -> 下载 URL."""
    # ① cover_local
    raw = str(payload.get("cover_local") or "").strip()
    if raw:
        p = Path(raw)
        if p.is_absolute() and p.is_file():
            return str(p)
        for base in (db_path.parent, db_path.parent.parent):
            cand = base / raw
            if cand.is_file():
                return str(cand)

    # ② covers/ 下按 key 推导(站点就是这样存的: <slug>.img / .jpg ...)
    covers = db_path.parent / "covers"
    if covers.is_dir() and album_key:
        slug = _cover_slug(album_key)
        for ext in (".img", ".jpg", ".jpeg", ".png", ".webp", ".gif"):
            cand = covers / (slug + ext)
            if cand.is_file():
                return str(cand)
        # 再宽松点: 找以 slug 开头的文件
        for f in covers.glob(slug + "*"):
            if f.is_file():
                return str(f)

    # ③ 远程 URL(交给调用方下载)
    url = str(payload.get("cover_url") or "").strip()
    if url.startswith(("http://", "https://")):
        return "url:" + url
    return ""


def _download_cover(url: str, dest: Path) -> str:
    """下载封面到 dest, 失败返回空串(不阻断导入)."""
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 dddb-import"})
        with urllib.request.urlopen(req, timeout=20) as r:
            blob = r.read()
        if not blob or len(blob) > 8 * 1024 * 1024:
            return ""
        dest.write_bytes(blob)
        return str(dest)
    except (urllib.error.URLError, OSError, ValueError):
        return ""


def _payload_to_form(payload: dict[str, Any],
                     album_key: str) -> dict[str, Any]:
    """payload -> 线上新建页的表单字段."""
    def _text(v: Any) -> str:
        return str(v or "").strip()

    artists = payload.get("artists")
    artists_text = "、".join(str(a) for a in artists) if isinstance(
        artists, list) else _text(artists)

    tags = payload.get("tags")
    tags_text = "、".join(str(t) for t in tags) if isinstance(tags, list) \
        else _text(tags)

    genres = payload.get("genres")
    genres_text = "、".join(str(g) for g in genres) if isinstance(genres, list) \
        else _text(genres)

    # 词条: credits -> [{role, name}]
    credits = []
    for c in (payload.get("credits") or []):
        if not isinstance(c, dict) or not c.get("name"):
            continue
        credits.append({
            "role": _text(c.get("role")),
            "name": _text(c.get("name")),
        })

    # 外链: urls 是 {域名: URL} 结构
    urls = []
    raw_urls = payload.get("urls")
    if isinstance(raw_urls, dict):
        for label, url in raw_urls.items():
            u = _text(url)
            if u:
                urls.append({"key": _text(label) or "链接", "value": u})
    elif isinstance(raw_urls, list):
        for item in raw_urls:
            if isinstance(item, dict):
                u = _text(item.get("href") or item.get("url"))
                if u:
                    urls.append({
                        "key": _text(item.get("text") or item.get("label"))
                        or "链接",
                        "value": u,
                    })

    # 曲目
    tracks = []
    for t in (payload.get("tracks") or []):
        if not isinstance(t, dict):
            continue
        ta = t.get("artists")
        tracks.append({
            "disc": str(t.get("disc") or 1),
            "index": str(t.get("index") or (len(tracks) + 1)),
            "title": _text(t.get("title")),
            "subtitle": _text(t.get("subtitle")),
            "title_zh": _text(t.get("title_zh")),
            "artists": "、".join(str(a) for a in ta)
            if isinstance(ta, list) else _text(ta),
            "lang": _text(t.get("lang")),
            "genre": _text(t.get("genre")),
            "duration": _fmt_duration(t.get("duration")),
        })

    return {
        "title_original": _text(payload.get("title_original")),
        "title": _text(payload.get("title")),
        "title_romaji": _text(payload.get("title_romaji")),
        "circle": _text(payload.get("circle")),
        "company": _text(payload.get("company")),
        "event": _text(payload.get("event")),
        "release_date": _text(payload.get("release_date")),
        "catalog_no": _text(payload.get("catalog_no")),
        "price": _text(payload.get("price")),
        "hall": _text(payload.get("hall")),
        "booth": _text(payload.get("booth")),
        "tags": tags_text,
        "genres": genres_text,
        "description": _text(payload.get("description")),
        "artists": artists_text,
        "credits": credits,
        "urls": urls,
        "tracks": tracks,
    }


def _fmt_duration(seconds: Any) -> str:
    try:
        n = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return "{}:{:02d}".format(n // 60, n % 60)


# ---------------------------------------------------------------------------
# ② 极简 CDP 客户端(标准库 websocket)
# ---------------------------------------------------------------------------

class _WS:
    """最小的 WebSocket 客户端(够 CDP 用)。"""

    def __init__(self, url: str, timeout: float = 20.0) -> None:
        # ws://127.0.0.1:9222/devtools/page/XXXX
        if not url.startswith("ws://"):
            raise ValueError("只支持 ws:// : {}".format(url))
        rest = url[5:]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection(
            (host, int(port or 80)), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET /{} HTTP/1.1\r\n"
            "Host: {}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: {}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).format(path, hostport, key)
        self.sock.sendall(req.encode())
        # 读掉握手响应
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += self.sock.recv(4096)
        self._id = 0

    def send(self, obj: dict[str, Any]) -> None:
        payload = json.dumps(obj).encode()
        header = bytearray()
        header.append(0x81)                      # FIN + text
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self) -> dict[str, Any]:
        while True:
            b1, b2 = self._read(2)
            opcode = b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            data = self._read(n)
            if opcode == 0x8:                    # close
                raise ConnectionError("websocket closed")
            if opcode in (0x9, 0xA):             # ping/pong
                continue
            try:
                return json.loads(data.decode("utf-8", "replace"))
            except ValueError:
                continue

    def _read(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("socket closed")
            buf += chunk
        return buf

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class CDP:
    """Chrome DevTools Protocol 客户端."""

    def __init__(self, port: int = 9222) -> None:
        self.port = port
        self.ws: _WS | None = None
        self._id = 0

    def _http(self, path: str) -> Any:
        url = "http://127.0.0.1:{}{}".format(self.port, path)
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def version(self) -> dict[str, Any]:
        return self._http("/json/version")

    def pages(self) -> list[dict[str, Any]]:
        try:
            return self._http("/json/list")
        except OSError:
            return []

    def connect_page(self) -> None:
        """连到第一个 page target."""
        for t in self.pages():
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                self.ws = _WS(t["webSocketDebuggerUrl"])
                return
        raise RuntimeError("没有可用的 page target")

    def call(self, method: str, **params: Any) -> dict[str, Any]:
        if self.ws is None:
            raise RuntimeError("未连接")
        self._id += 1
        mid = self._id
        self.ws.send({"id": mid, "method": method, "params": params})
        deadline = time.time() + 60
        while time.time() < deadline:
            msg = self.ws.recv()
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("CDP {} 失败: {}".format(
                        method, msg["error"]))
                return msg.get("result") or {}
        raise TimeoutError("CDP {} 超时".format(method))

    def eval_js(self, expression: str, await_promise: bool = False) -> Any:
        res = self.call("Runtime.evaluate", expression=expression,
                        returnByValue=True, awaitPromise=await_promise)
        return (res.get("result") or {}).get("value")

    def close(self) -> None:
        if self.ws is not None:
            self.ws.close()
            self.ws = None


# ---------------------------------------------------------------------------
# ③ 启动浏览器 + 填表
# ---------------------------------------------------------------------------

def find_browser() -> str:
    """找可用的 Chromium 系浏览器."""
    cands = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for c in cands:
        if Path(c).is_file():
            return c
    raise FileNotFoundError("没找到 Chrome / Edge")


def launch_browser(url: str, port: int = 9222,
                   profile_dir: Path | None = None) -> subprocess.Popen:
    """启动浏览器(带远程调试), 返回进程."""
    exe = find_browser()
    if profile_dir is None:
        # **不要放用户主目录**: 某些环境(沙箱)不允许在那里建目录。
        # 用系统临时目录, 一次性的调试 profile。
        import tempfile

        profile_dir = Path(tempfile.gettempdir()) / "dddb-cdp-profile"
    profile_dir = Path(profile_dir)
    try:
        profile_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        import tempfile

        profile_dir = Path(tempfile.mkdtemp(prefix="dddb-cdp-"))
    args = [
        exe,
        "--remote-debugging-port={}".format(port),
        "--user-data-dir={}".format(profile_dir),
        "--no-first-run",
        "--no-default-browser-check",
        url,
    ]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def wait_for_cdp(port: int = 9222, timeout: float = 25.0) -> CDP:
    """等浏览器起来并连上."""
    cdp = CDP(port)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            cdp.version()
            cdp.connect_page()
            return cdp
        except (OSError, RuntimeError, ValueError):
            time.sleep(0.5)
    raise TimeoutError("浏览器调试端口没起来(port={})".format(port))


def ensure_logged_in(cdp: CDP, base_url: str, username: str,
                     password: str, timeout: float = 25.0) -> bool:
    """确保浏览器处于登录态(已登录就直接返回 True).

    只操作本地浏览器, 不碰服务端 —— 相当于替你点了一次登录框。
    """
    base = base_url.rstrip("/")

    def _at_login() -> bool:
        path = cdp.eval_js("location.pathname") or ""
        return str(path).startswith("/login")

    def _wait_ready() -> None:
        for _ in range(40):
            if cdp.eval_js("document.readyState") == "complete":
                return
            time.sleep(0.4)

    cdp.call("Page.enable")
    cdp.eval_js("location.href = '{}/login'".format(base))
    time.sleep(1.5)
    _wait_ready()
    if not _at_login():
        # 可能已登录被重定向走
        return True

    js = """
    (function(u, p){
      var ue = document.querySelector('[name="username"]');
      var pe = document.querySelector('[name="password"]');
      var f = document.querySelector('form');
      if (!ue || !pe || !f) return 'no-form';
      ue.value = u; pe.value = p;
      ue.dispatchEvent(new Event('input', {bubbles: true}));
      pe.dispatchEvent(new Event('input', {bubbles: true}));
      f.submit();
      return 'submitted';
    })(%s, %s)
    """ % (json.dumps(username), json.dumps(password))
    state = cdp.eval_js(js)
    if state != "submitted":
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.6)
        path = str(cdp.eval_js("location.pathname") or "")
        if path and not path.startswith("/login"):
            return True
    return False


def open_new_album(cdp: CDP, base_url: str, timeout: float = 30.0) -> bool:
    """跳到线上的新建专辑页, 等表单渲染出来."""
    url = "{}/new/album".format(base_url.rstrip("/"))
    cdp.eval_js("location.href = {}".format(json.dumps(url)))
    time.sleep(1.5)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cdp.eval_js(
                "!!document.querySelector('[name=\"title_original\"]')"):
            return True
        time.sleep(0.5)
    return False


#: 填表的 JS —— 不提交, 只填
FILL_JS = r"""
(function(payload) {
  var filled = [];
  var missing = [];

  function setValue(el, value) {
    if (!el || value === null || value === undefined) return false;
    el.value = String(value);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
  }

  // 单值字段
  var singles = ['title_original', 'title', 'title_romaji', 'circle',
                 'company', 'event', 'release_date', 'catalog_no',
                 'price', 'hall', 'booth', 'tags', 'genres',
                 'description', 'artists'];
  singles.forEach(function(name) {
    var v = payload[name];
    if (!v) return;
    var el = document.querySelector('[name="' + name + '"]');
    if (setValue(el, v)) filled.push(name); else missing.push(name);
  });

  // 词条(credit_role / credit_name 成对)
  if (payload.credits && payload.credits.length) {
    var roles = document.querySelectorAll('[name="credit_role"]');
    var names = document.querySelectorAll('[name="credit_name"]');
    var n = Math.min(payload.credits.length, roles.length, names.length);
    for (var i = 0; i < n; i++) {
      setValue(roles[i], payload.credits[i].role);
      setValue(names[i], payload.credits[i].name);
    }
    filled.push('credits(' + n + ')');
  }

  // 外链
  if (payload.urls && payload.urls.length) {
    var ks = document.querySelectorAll('[name="url_key"]');
    var vs = document.querySelectorAll('[name="url_value"]');
    var m = Math.min(payload.urls.length, ks.length, vs.length);
    for (var j = 0; j < m; j++) {
      setValue(ks[j], payload.urls[j].key);
      setValue(vs[j], payload.urls[j].value);
    }
    filled.push('urls(' + m + ')');
  }

  // 曲目
  if (payload.tracks && payload.tracks.length) {
    var secs = {disc: 'track_disc', index: 'track_index',
                title: 'track_title', subtitle: 'track_subtitle',
                title_zh: 'track_title_zh', artists: 'track_artists',
                lang: 'track_lang', genre: 'track_genre',
                duration: 'track_duration'};
    Object.keys(secs).forEach(function(field) {
      var els = document.querySelectorAll('[name="' + secs[field] + '"]');
      var cnt = Math.min(payload.tracks.length, els.length);
      for (var k = 0; k < cnt; k++) {
        setValue(els[k], payload.tracks[k][field]);
      }
    });
    var tn = document.querySelectorAll('[name="track_title"]').length;
    filled.push('tracks(' + Math.min(payload.tracks.length, tn) + ')');
  }

  return JSON.stringify({filled: filled, missing: missing,
                         title: document.title,
                         url: location.href});
})(PAYLOAD_PLACEHOLDER)
"""


def fill_page(cdp: CDP, data: dict[str, Any]) -> dict[str, Any]:
    """把缓存里的值填进当前页(不提交)."""
    payload = {k: v for k, v in data.items() if not k.startswith("_")}
    js = FILL_JS.replace("PAYLOAD_PLACEHOLDER",
                         json.dumps(payload, ensure_ascii=False))
    raw = cdp.eval_js(js)
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {"raw": raw}


def upload_cover(cdp: CDP, cover_path: str) -> bool:
    """给 <input type=file name=cover> 设置文件."""
    if not cover_path or not Path(cover_path).is_file():
        return False
    doc = cdp.call("DOM.getDocument", depth=1)
    root = doc.get("root", {}).get("nodeId")
    if not root:
        return False
    node = cdp.call("DOM.querySelector",
                    nodeId=root, selector='input[type=file][name="cover"]')
    nid = node.get("nodeId")
    if not nid:
        return False
    cdp.call("DOM.setFileInputFiles", files=[str(cover_path)], nodeId=nid)
    return True


def import_to_online(db_path: Path, album_key: str, *,
                     base_url: str = "https://www.digjindb.top",
                     cache_dir: Path | None = None,
                     port: int = 9222,
                     auto_fill: bool = True,
                     keep_open: bool = True) -> dict[str, Any]:
    """一键: 导出缓存 -> 打开线上新建页 -> 自动填表(含封面).

    不修改线上代码, 不调线上接口, 也不自动提交。
    """
    cache_dir = Path(cache_dir or (Path(db_path).parent / CACHE_DIR_NAME))
    data = export_album(Path(db_path), album_key, cache_dir)
    new_url = "{}/new/album".format(base_url.rstrip("/"))

    proc = launch_browser(new_url, port=port)
    result: dict[str, Any] = {
        "album_key": album_key,
        "cache": data["_cache_json"],
        "cover": data.get("cover_file") or "",
        "url": new_url,
        "browser_pid": getattr(proc, "pid", 0),
    }
    if not auto_fill:
        result["note"] = "浏览器已打开, 未自动填表"
        return result

    cdp = wait_for_cdp(port)
    try:
        # 等页面加载
        cdp.call("Page.enable")
        for _ in range(40):
            state = cdp.eval_js("document.readyState")
            if state == "complete":
                break
            time.sleep(0.5)
        # 表单可能还没渲染出来, 再等一下
        for _ in range(20):
            has = cdp.eval_js(
                "!!document.querySelector('[name=\"title_original\"]')")
            if has:
                break
            time.sleep(0.5)

        result["filled"] = fill_page(cdp, data)
        if data.get("cover_file"):
            try:
                result["cover_uploaded"] = upload_cover(
                    cdp, data["cover_file"])
            except RuntimeError as exc:
                result["cover_uploaded"] = False
                result["cover_error"] = str(exc)
        if not keep_open:
            cdp.close()
    finally:
        if not keep_open:
            try:
                proc.terminate()
            except OSError:
                pass
    return result


__all__ = [
    "CDP",
    "CACHE_DIR_NAME",
    "export_album",
    "fill_page",
    "ensure_logged_in",
    "find_browser",
    "import_to_online",
    "open_new_album",
    "launch_browser",
    "upload_cover",
    "wait_for_cdp",
]
