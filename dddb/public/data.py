"""用户端的**只读**查询层.

职责: 把 :class:`dddb.db.Store` 里的数据整理成模板直接可用的"视图字典"。

为什么不用 :meth:`Store.list_albums` 而要自己写 SQL?

* 用户端需要几个额外的筛选(按社团精确匹配、按"信息缺口"筛选、
  按曲目数排序);
* 未登录 / 普通用户的读路径必须**完全只读**: 这里只用
  ``Store.query`` / ``Store.query_one`` 两个低层只读方法, 不调用任何写方法;
* 建表列可能随版本变化, 所以启动时先 ``PRAGMA table_info`` 探测一次, 只拼
  真正存在的列 —— 缺列时降级而不是抛 500。

**不暴露资料来源**: 视图字典里没有 ``sources`` / ``trust`` 这类字段, 也没有
按来源筛选 —— 用户端不该显示资料是怎么收集来的。

所有对外方法返回的都是普通 ``dict`` / ``list``, 不含 ``Album`` 对象, 于是模板层
可以脱离数据库单测。
"""

from __future__ import annotations

import urllib.parse

import json
import logging
import re
import sqlite3
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import quote, unquote, urlsplit

from .templates import (
    NEEDS_LABELS,
    event_kind_label,
    role_label,
    safe_url,
    sort_role,
)
from ..tagrules import is_useless_tag
from .eventseries import display_name, parse_event_name

logger = logging.getLogger("dddb.public")

#: 排序方式(公开端只暴露对访客有意义的几种)
SORTS: tuple[tuple[str, str], ...] = (
    ("updated", "最近更新"),
    ("date", "发售日(新 → 旧)"),
    ("title", "标题"),
    ("circle", "社团"),
    ("tracks", "曲目数(多 → 少)"),
    ("event", "展会"),
)
SORT_KEYS = tuple(key for key, _ in SORTS)
DEFAULT_SORT = "updated"

#: 每页条数候选
PER_PAGE_CHOICES = (12, 24, 48, 96)
DEFAULT_PER = 24

#: 评论区分页(比专辑列表小: 评论是正文, 一页 10 条读着正好)
COMMENT_PER_PAGE = 10
COMMENT_PER_PAGE_CHOICES = (5, 10, 20, 50)

#: 编辑者队列的"缺口"种类(顺序 = 从最常见的缺口开始)
NEEDS_KINDS: tuple[tuple[str, str], ...] = (
    ("no_cover", NEEDS_LABELS["no_cover"]),
    ("no_tracks", NEEDS_LABELS["no_tracks"]),
    ("no_desc", NEEDS_LABELS["no_desc"]),
    ("no_circle", NEEDS_LABELS["no_circle"]),
    ("no_date", NEEDS_LABELS["no_date"]),
    ("no_catalog", NEEDS_LABELS["no_catalog"]),
    ("no_credits", NEEDS_LABELS["no_credits"]),
    ("no_source_url", NEEDS_LABELS["no_source_url"]),
)
NEEDS_KEYS = tuple(key for key, _ in NEEDS_KINDS)

#: **用户端不保留任何来源键表** —— 外链只按 ``urls`` 的原始顺序渲染。
#: 带来源站名的展示顺序表放在管理面板: ``dddb/webui/linkorder.py``。
def _url_sort_key(key: str) -> int:
    """外链排序键: **恒定返回同一个值, 即保持用户填写的顺序**.

    早先按 ``special/official/site`` 分档把"特设页/官网"提到最前 —— 但编辑页
    是按库里的顺序展示的, 两边不一致, 用户改完外链再回详情页会觉得
    "顺序被搞乱了"(实测 261 张专辑受影响)。外链顺序是用户自己排的,
    站点不该替他重排。

    保留这个函数是因为 ``/link/<key>/<n>`` 的序号依赖同一套排序 ——
    恒等排序保证"页面第 n 个按钮"与"库里的第 n 条"永远一致。
    """
    return 0


# --------------------------------------------------------------------------
# 外链安全校验(**没有域名黑名单**: 任何站点的链接都允许)
# --------------------------------------------------------------------------

#: 一律拒绝的协议 —— 这是 XSS 防护, 不是站点政策
_BLOCKED_LINK_SCHEMES: tuple[str, ...] = (
    "javascript:", "jscript:", "livescript:", "vbscript:",
    "data:", "file:", "blob:", "about:",
)


#: URL 里要抹掉的空白/零宽字符 —— ``java\tscript:`` / ``某站\u200b.net`` 这类绕过手法
_URL_NOISE_RE = re.compile(r"[\s\u200b-\u200f\u2028\u2029\ufeff]+")


def _clean_url_text(url: Any) -> str:
    """URL -> 去空白/零宽字符后的文本(非字符串也先转成字符串)。"""
    if url is None:
        return ""
    text = url if isinstance(url, str) else str(url)
    return _URL_NOISE_RE.sub("", text).strip()


def _url_scheme_host(url: Any) -> tuple[str, str]:
    """``-> (scheme, host)``; 解析不出来时给空串.

    host 经过 NFKC + percent 解码 + 小写 + 去尾点: 于是全角 ``ｖｇｍｄｂ``、
    百分号编码 (``%2E``)、大写、尾点等写法都归一成同一个 host。
    """
    text = _clean_url_text(url)
    if not text:
        return "", ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return "", ""
    scheme = str(parts.scheme or "").lower()
    try:
        raw_host = parts.hostname or ""
    except ValueError:  # 畸形 IPv6 字面量之类
        return scheme, ""
    host = unicodedata.normalize("NFKC", unquote(raw_host)).strip().lower().rstrip(".")
    return scheme, host


def blocked_link_host(url: Any) -> str:
    """**不再有域名黑名单** —— 保留函数只为兼容旧调用方, 恒返回空串."""
    return ""


def outbound_link_reject_reason(url: Any) -> str:
    """``""`` = 允许对外跳转; 非空 = 拒绝原因.

    **只做安全校验, 不做站点政策**:
    * 只放行 ``http`` / ``https``; ``javascript:`` / ``data:`` / 相对路径一律拒
      (那是 XSS, 不是政策);
    * 含反斜杠的拒(浏览器把反斜杠当斜杠, 能骗过 host 解析);
    * 解析不出 host 的拒。

    **不再有域名黑名单**: 任何数据站点的链接都允许显示与外跳。
    """
    text = _clean_url_text(url)
    if not text:
        return "链接为空"
    lowered = text.lower()
    for scheme in _BLOCKED_LINK_SCHEMES:
        if lowered.startswith(scheme):
            return f"禁止的协议 {scheme}"
    if "\\" in text:
        return "URL 含有反斜杠(可能是 host 伪装)"
    scheme, host = _url_scheme_host(text)
    if scheme not in ("http", "https"):
        return f"只允许 http/https 链接(实际: {scheme or '无协议'})"
    if not host:
        return "无法解析出主机名"
    return ""


def outbound_link_allowed(url: Any) -> bool:
    """公开端是否允许把这个 URL 当成外部链接跳出去."""
    return not outbound_link_reject_reason(url)


def _outbound_named_rows(urls: Mapping[str, Any]) -> list[tuple[str, str]]:
    """``urls`` 映射 -> ``[(名称, url)]``, 排序与 :func:`_outbound_rows` 完全一致.

    详情页显示链接名(用户自己填的标签), ``/link/<key>/<n>`` 仍然按下标取
    URL —— 两边必须用同一个排序, 否则点击第 n 个会跳到别的链接。
    """
    rows = [(str(key), _text(value)) for key, value in urls.items() if _text(value)]
    rows.sort(key=lambda kv: _url_sort_key(kv[0]))
    return rows


def _outbound_rows(urls: Mapping[str, Any]) -> list[str]:
    """``urls`` 映射 -> 稳定排序后的 URL 列表(下标 + 1 = ``/link/`` 的序号).

    详情页与 ``/link/<key>/<n>`` 共用它 —— 保证"页面上第 n 个外部链接"与
    "``/link/<key>/n``"永远指同一条记录(以前两边各过滤一次, 序号会错位)。
    """
    rows = [(str(key), _text(value)) for key, value in urls.items() if _text(value)]
    rows.sort(key=lambda kv: _url_sort_key(kv[0]))
    return [url for _, url in rows]

try:  # pragma: no cover - 兜底分支在正常安装下不会走到
    from ..textutil import norm_key as _norm_key
except Exception:  # pragma: no cover
    import re as _re
    import unicodedata as _ud

    def _norm_key(text: str, *, romaji: bool = True, drop_noise: bool = True) -> str:
        raw = _ud.normalize("NFKC", str(text or "")).lower()
        return _re.sub(r"[^0-9a-z\u3040-\u30ff\u4e00-\u9fff]+", " ", raw).strip()


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _album_href(key: Any) -> str:
    """专辑详情路径(URL 编码)."""
    return "/album/" + urllib.parse.quote(str(key or ""), safe="")


def _norm_date(value: Any) -> str:
    """把日期补成同宽 ``YYYY-MM-DD``, 便于字符串比较.

    ``2026-9-20`` -> ``2026-09-20``; ``2026`` -> ``2026-01-01``;
    认不出来的原样返回(排序时靠字符串兜底, 至少不会崩)。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    m = re.match(r"^(\d{4})[-/年]?(\d{1,2})?[-/月]?(\d{1,2})?", text)
    if not m:
        return text
    year = m.group(1)
    month = (m.group(2) or "1").zfill(2)
    day = (m.group(3) or "1").zfill(2)
    try:
        # 合法性检查: 月份 1-12, 日 1-31
        mi, di = int(month), int(day)
        if not (1 <= mi <= 12) or not (1 <= di <= 31):
            return text
    except ValueError:
        return text
    return "{}-{}-{}".format(year, month, day)


def _sort_series(items: list[dict[str, Any]], *, sort: str = "time",
                 order: str = "desc") -> list[dict[str, Any]]:
    """按时间或专辑数量排序展会系列.

    ``sort``: ``time``(最新一届的日期) / ``count``(专辑总数)
    ``order``: ``desc``(默认) / ``asc``

    规则:
        * 时间: 用 ``latest``(ISO 日期字符串可直接比较);
        * 数量: 用 ``count``, 相同数量时按 ``latest`` 旧的在后兜底;
        * **时间排序时**没有日期的系列排最后(正序倒序都一样) —— 否则
          正序时一堆空值会占满第一屏; 数量排序不受日期影响;
        * 最后都以名字兜底, 保证顺序稳定(刷新不会乱跳)。
    """
    key = "count" if str(sort or "").lower() == "count" else "time"
    reverse = str(order or "").lower() != "asc"

    def _name(item: Mapping[str, Any]) -> str:
        return str(item.get("name") or "").lower()

    if key == "count":
        def _k(item: Mapping[str, Any]) -> tuple:
            return (int(item.get("count") or 0),
                    str(item.get("latest") or ""))
    else:
        def _k(item: Mapping[str, Any]) -> tuple:
            return (str(item.get("latest") or ""),
                    int(item.get("count") or 0))


    # 稳定排序两趟: 先按名字升序, 再按主键排序。
    # 这样值相同时保持名字升序, 而不会跟着 reverse 一起翻。
    result = sorted(items, key=_name)
    result = sorted(result, key=_k, reverse=reverse)
    if key == "time":
        # 只有**时间排序**才把"没有日期"的压到最后 —— 正序时 sorted 会
        # 把它们翻到最前, 一屏空值看着像坏了。
        # 数量排序时不能这么做: 有没有日期不该影响它在数量序列里的位置。
        dated = [x for x in result if str(x.get("latest") or "")]
        undated = [x for x in result if not str(x.get("latest") or "")]
        return dated + undated
    return result


class PublicDataError(RuntimeError):
    """公开端读不到数据(表不存在 / 库损坏 / 库文件不在)."""


class ReadOnlyStore:
    """``sqlite3`` 的**只读**适配器, 接口与 :class:`dddb.db.Store` 的读部分一致.

    为什么不用 ``Store``: 它的构造函数会 ``CREATE TABLE IF NOT EXISTS`` 并把
    ``journal_mode`` 落盘 —— 那已经是写操作。公开端承诺"只读", 所以这里直接用
    ``mode=ro`` 打开连接: 库文件不存在会直接报错, 任何写语句都会被 SQLite 拒绝。

    只用 :meth:`query` / :meth:`query_one`, 于是 :class:`PublicData` 既能配
    ``Store``(测试里注入) 也能配本类(实际起服务时)。
    """

    def __init__(self, path: Path | str, *, timeout: float = 10.0) -> None:
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise PublicDataError(f"数据库文件不存在: {self.path}")
        uri = "file:" + quote(str(self.path.as_posix())) + "?mode=ro"
        try:
            self._conn = sqlite3.connect(
                uri, uri=True, check_same_thread=False, timeout=float(timeout)
            )
        except sqlite3.Error as exc:
            raise PublicDataError(f"只读打开数据库失败 {self.path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()

    @property
    def readonly(self) -> bool:
        return True

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def normalize(value: Any) -> str:
    """用于"社团结对"的归一化键(失败时退化为去空白小写)."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        norm = _norm_key(text, romaji=False)
    except Exception:
        norm = ""
    return norm or text.lower()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _disc_names(value: Any) -> dict[str, str]:
    """碟名映射(用户 m16568): 键统一成 ``"1"`` 这种碟号字符串, 值去空白.

    ``payload["discs"]`` 在库里可能被写成 ``{1: "..."}``(JSON 里是字符串键,
    但直接读 dict 时也可能是 int), 这里统一归一, 非法项丢掉。
    """
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, str] = {}
    for raw_key, raw_name in value.items():
        try:
            number = int(str(raw_key).strip())
        except (TypeError, ValueError):
            continue
        name = _text(raw_name)
        if number > 0 and name:
            out[str(number)] = name
    return out


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes, Mapping)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _text(item)
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def paginate(total: Any, page: Any = 1, per: Any = DEFAULT_PER, *, window: int = 2) -> dict[str, Any]:
    """通用分页计算(纯函数).

    * 页码越界会被**夹**到合法区间(而不是返回空页), 这样手改 URL 不会看到空白;
    * ``total == 0`` 时仍然返回 ``pages = 1``, 免得模板里出现 "第 1/0 页";
    * ``window`` 是当前页左右各显示几个页码, 首尾页始终显示, 中间用 ``"..."`` 表示省略。
    """
    try:
        per_v = int(per)
    except (TypeError, ValueError):
        per_v = DEFAULT_PER
    per_v = max(1, min(per_v, 500))
    try:
        total_v = max(0, int(total or 0))
    except (TypeError, ValueError):
        total_v = 0
    pages = max(1, (total_v + per_v - 1) // per_v)
    try:
        page_v = int(page or 1)
    except (TypeError, ValueError):
        page_v = 1
    page_v = max(1, min(page_v, pages))
    numbers: list[Any] = []
    span = max(0, int(window))
    low, high = max(1, page_v - span), min(pages, page_v + span)
    if low > 1:
        numbers.append(1)
        if low > 2:
            numbers.append("...")
    numbers.extend(range(low, high + 1))
    if high < pages:
        if high < pages - 1:
            numbers.append("...")
        numbers.append(pages)
    return {
        "total": total_v,
        "page": page_v,
        "per": per_v,
        "pages": pages,
        "offset": (page_v - 1) * per_v,
        "window": numbers,
        "has_prev": page_v > 1,
        "has_next": page_v < pages,
    }


def human_time(ts: Any, lang: Any = "zh") -> str:
    """时间戳 -> ``2026-10-24 03:11``(空值返回空串).

    **按语言时区**格式化(zh=UTC+8 / ja=UTC+9 / en=UTC), 不依赖服务器时区 ——
    云端机器是 UTC, 用 ``time.localtime()`` 会让中文页面慢 8 小时。
    """
    from .timefmt import format_stamp

    return format_stamp(ts, lang)


def coerce_comment_per(per: Any) -> int:
    """评论每页条数只接受白名单值(防止 ``?cper=100000``)."""
    try:
        value = int(per)
    except (TypeError, ValueError):
        return COMMENT_PER_PAGE
    return value if value in COMMENT_PER_PAGE_CHOICES else COMMENT_PER_PAGE


def comment_view(row: Any) -> dict[str, Any]:
    """评论行(``sqlite3.Row`` 或映射) -> 模板视图.

    ``body`` 保持**原文**(模板负责转义), ``created_text`` 是本地时间文本。
    """
    def field(name: str, default: Any = "") -> Any:
        if isinstance(row, Mapping):
            return row.get(name, default)
        try:
            return row[name]
        except (IndexError, KeyError):
            return default

    status = _text(field("status")) or "visible"
    created = field("created_at", 0.0)
    hidden_at = field("hidden_at", 0.0)
    return {
        "id": _int(field("id")),
        "target_kind": _text(field("target_kind")),
        "target_key": _text(field("target_key")),
        "user_id": _int(field("user_id")),
        "username": _text(field("username")),
        "body": str(field("body") or ""),
        "created_at": float(created or 0.0),
        "created_text": human_time(created),
        "status": status,
        "hidden": status != "visible",
        "hidden_by": _text(field("hidden_by")),
        "hidden_at": float(hidden_at or 0.0),
        "hidden_text": human_time(hidden_at),
    }


def group_credits(credits: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """把 credits 按职责分组去重, 并按 :data:`ROLE_ORDER` 排序."""
    buckets: dict[str, list[str]] = {}
    labels: dict[str, str] = {}
    if isinstance(credits, Sequence):
        for credit in credits:
            if not isinstance(credit, Mapping):
                continue
            name = _text(credit.get("name"))
            if not name:
                continue
            role = _text(credit.get("role")) or "artist"
            labels.setdefault(role, role_label(role))
            names = buckets.setdefault(role, [])
            if name not in names:
                names.append(name)
    groups = [
        {"role": role, "label": labels.get(role, role_label(role)), "names": names, "count": len(names)}
        for role, names in buckets.items()
    ]
    groups.sort(key=lambda g: sort_role(g["role"]))
    return groups


def _cover_ok_sql(alias: str = "") -> str:
    """SQL: "这张碟有可用封面" 的判据.

    与 :func:`missing_fields` 保持一致 —— 抓来的 ``cover_url`` 可能是
    ``javascript:`` / ``data:`` 这类不可渲染的值, 那种不算封面, 否则列表页会
    按"有封面"筛选出来却显示占位块。
    """
    prefix = f"{alias}." if alias else ""
    return (
        f"({prefix}cover_local <> '' OR {prefix}cover_url LIKE 'http://%'"
        f" OR {prefix}cover_url LIKE 'https://%')"
    )


def missing_fields(payload: Mapping[str, Any], *, track_count: int | None = None) -> list[str]:
    """这张专辑缺哪些关键信息(编辑者队列与徽标共用)."""
    out: list[str] = []
    tracks = payload.get("tracks") or []
    count = len(tracks) if track_count is None else track_count
    if not (_text(payload.get("cover_local")) or safe_url(_text(payload.get("cover_url")))):
        out.append("no_cover")
    if not count:
        out.append("no_tracks")
    if not _text(payload.get("description")):
        out.append("no_desc")
    if not _text(payload.get("circle")):
        out.append("no_circle")
    if not _text(payload.get("release_date")):
        out.append("no_date")
    if not _text(payload.get("catalog_no")):
        out.append("no_catalog")
    if not list(payload.get("credits") or []):
        out.append("no_credits")
    urls = payload.get("urls") or {}
    if not (isinstance(urls, Mapping) and any(_text(v) for v in urls.values())):
        out.append("no_source_url")
    return out


def _duration_text(seconds: Any) -> str:
    from .templates import duration_text

    return duration_text(seconds)


# --------------------------------------------------------------------------
# PublicData
# --------------------------------------------------------------------------


class PublicData:
    """公开端的读模型.

    ``covers_dir`` 是允许对外发送封面的目录(默认 ``<root>/data/covers``)。
    """

    def __init__(
        self,
        store: Any,
        *,
        root: Path | str | None = None,
        covers_dir: Path | str | None = None,
        cache_ttl: float = 20.0,
    ) -> None:
        self.store = store
        self.root = Path(root).resolve() if root else None
        self.cache_ttl = float(cache_ttl)
        if covers_dir is not None:
            self.covers_dir = Path(covers_dir).resolve()
        elif self.root is not None:
            self.covers_dir = (self.root / "data" / "covers").resolve()
        else:
            self.covers_dir = None
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._columns: set[str] | None = None
        #: ``comments`` 表是否可用(旧库 / 只读副本里可能还没有这张表)
        self._comments_ready: bool | None = None

    # -- 低层 ---------------------------------------------------------------

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.store.query(sql, params))

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.store.query_one(sql, params)

    def _count(self, sql: str, params: Sequence[Any] = (), *, default: int = 0) -> int:
        try:
            row = self._one(sql, params)
        except sqlite3.Error as exc:
            logger.warning("统计查询失败(降级为 %s): %s", default, exc)
            return default
        if row is None:
            return default
        try:
            return int(list(row)[0] or 0)
        except (IndexError, TypeError, ValueError):
            return default

    def columns(self) -> set[str]:
        """``albums`` 表实际存在的列(探测一次并缓存)."""
        if self._columns is not None:
            return self._columns
        try:
            rows = self._query("PRAGMA table_info(albums)")
        except sqlite3.Error as exc:
            raise PublicDataError(f"读不到 albums 表结构: {exc}") from exc
        if not rows:
            raise PublicDataError("数据库里没有 albums 表")
        self._columns = {str(row["name"]) for row in rows}
        return self._columns

    def cached(self, key: str, factory: Callable[[], Any], *, ttl: float | None = None) -> Any:
        """极短的 TTL 缓存 —— 每次请求都要算的头部计数没必要反复查库."""
        life = self.cache_ttl if ttl is None else float(ttl)
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and hit[0] > now:
                return hit[1]
        value = factory()
        with self._lock:
            self._cache[key] = (now + life, value)
        return value

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    # -- 视图 ---------------------------------------------------------------

    def make_view(
        self,
        payload: Mapping[str, Any],
        *,
        updated_at: float | None = None,
        detailed: bool = False,
        track_count: int | None = None,
    ) -> dict[str, Any]:
        """payload -> 模板视图字典.

        ``detailed=False`` 时不解析曲目表与制作人员(列表页只要卡片, 省一半开销)。
        """
        from .templates import quote_segment

        key = _text(payload.get("key"))
        title = _text(payload.get("title_original")) or _text(payload.get("title"))
        artists = _str_list(payload.get("artists"))
        tags = _str_list(payload.get("tags"))
        genres = _str_list(payload.get("genres"))
        tracks_raw = payload.get("tracks") if detailed else []
        tracks: list[dict[str, Any]] = []
        if detailed and isinstance(tracks_raw, Sequence):
            for index, track in enumerate(tracks_raw, start=1):
                if not isinstance(track, Mapping):
                    continue
                duration = track.get("duration")
                try:
                    seconds = int(duration) if duration is not None else 0
                except (TypeError, ValueError):
                    seconds = 0
                tracks.append({
                    "index": int(track.get("index") or index),
                    "disc": int(track.get("disc") or 1),
                    "title": _text(track.get("title")),
                    "artists": _str_list(track.get("artists")),
                    "duration": seconds or None,
                    "duration_text": _duration_text(seconds),
                    # 补充字段: 详情页"有值才展开显示"(默认不占版面)
                    "subtitle": _text(track.get("subtitle")),
                    "title_zh": _text(track.get("title_zh")),
                    "lang": _text(track.get("lang")),
                    "genre": _text(track.get("genre")),
                })
        credits: list[dict[str, Any]] = []
        if detailed:
            raw_credits = payload.get("credits")
            credits = group_credits(raw_credits if isinstance(raw_credits, Sequence) else [])

        if detailed:
            count = len(tracks)
        else:
            # 列表页不解析曲目, 用 track_count 列; 列拿不到时退回 payload 长度
            count = _int(track_count) or len(payload.get("tracks") or [])

        url_rows: list[dict[str, Any]] = []
        urls = payload.get("urls")
        if isinstance(urls, Mapping):
            # **不在页面上写外部 URL**: 每个外链都走本站的 /link/ 中转。
            # 但**链接名要显示用户填的标签**(circle_site / x / youtube ...),
            # 那是用户自己写的名字, 不是"资料来源" —— 只显示名字,
            # 不显示域名/外链地址, 合规要求不变。
            # 序号与 :func:`_outbound_rows` 完全一致(同一个排序函数)。
            named_rows = _outbound_named_rows(urls)
            for position, (name, url) in enumerate(named_rows, 1):
                # **position 是原始序号**(与 /link/<key>/<n> 一致);
                # 模板渲染时可能过滤掉禁用站的条目, 但 href 必须仍用这个序号,
                # 否则按钮序号会错位(点第 2 个跳到第 3 条 / 直接 404)。
                href = f"/link/{quote_segment(key)}/{position}" if key else ""
                # embed: **只认用户自己粘贴的 embed code**, 纯链接不生成播放器
                #   (用户明确要求: 识别到 YouTube/SoundCloud 纯链接不要塞播放器)
                # 只把平台 key 和高度交给模板, **不含外部地址** —— 页面不留外部 URL。
                from .embed import extract_embed as _extract_embed
                _embed = _extract_embed(url) or {}
                url_rows.append({"name": name, "url": url,
                                 "href": href, "position": position,
                                 "embed": _embed.get("platform") or "",
                                 "embed_height": _embed.get("height") or 0,
                                 # iframe 要直连平台(中转会被 CSP 拦) —— 只给
                                 # 播放器地址, 已过 extract_embed 的白名单校验
                                 "embed_src": _embed.get("src") or ""})

        # 封面: **一律走本站 /cover/<key>**(本地缓存命中 -> 外接对象存储 ->
        # 服务端拉取落盘 -> 占位图)。绝不把源站图片地址写进 HTML ——
        # 那既是 hotlink, 也会把源站地址写进我们的 HTML。
        cover_local = _text(payload.get("cover_local"))
        cover_remote = safe_url(_text(payload.get("cover_url")))
        has_cover = bool(cover_local or cover_remote)
        # 封面 URL 带版本号: 换图后 URL 变化, CDN/浏览器才会重新拉取。
        # 版本优先取 payload.cover_v(上传时写入); 没有就用 mtime+大小兜底。
        # 不加版本号的话, 详情页/卡片会用固定 URL 命中长缓存 -> "退出编辑页还是原封面"。
        cover_version = _text(payload.get("cover_v"))
        if not cover_version and cover_local:
            try:
                from pathlib import Path as _Path

                cover_version = str(int(_Path(cover_local).stat().st_mtime))
            except (OSError, ValueError):
                cover_version = ""
        if key and has_cover:
            base = f"/cover/{quote_segment(key)}"
            cover_href = f"{base}?v={cover_version}" if cover_version else base
        else:
            cover_href = ""

        view: dict[str, Any] = {
            "key": key,
            "href": f"/album/{quote_segment(key)}" if key else "",
            "title": title or "(无标题)",
            "title_alt": _text(payload.get("title")) if _text(payload.get("title")) != title else "",
            "romaji": _text(payload.get("title_romaji")),
            "circle": _text(payload.get("circle")),
            "circle_href": f"/circle/{quote_segment(_text(payload.get('circle')))}" if _text(payload.get("circle")) else "",
            "event": _text(payload.get("event")),
            "event_href": f"/event/{quote_segment(_text(payload.get('event')))}" if _text(payload.get("event")) else "",
            "event_kind": _text(payload.get("event_kind")),
            "date": _text(payload.get("release_date")),
            "year": (_text(payload.get("release_date")) or "")[:4],
            "catalog_no": _text(payload.get("catalog_no")),
            "price": _text(payload.get("price")),
            "booth": _text(payload.get("booth")),
            "hall": _text(payload.get("hall")),
            # 用户 m16568: 各碟的名字 / 原作 IP / 制作人 / 发布平台 / 出品类型
            "discs": _disc_names(payload.get("discs")),
            "original_work": _text(payload.get("original_work")),
            "producer": _text(payload.get("producer")),
            "platform": _text(payload.get("platform")),
            "release_kind": _text(payload.get("release_kind")),
            "artists": artists,
            "tags": tags,
            "genres": genres,
            "has_cover": has_cover,
            # 附图(不参与封面): 与 credits/tracks 同级的附加数据
            "photos": [
                {"url": str(p.get("url") or ""),
                 "note": str(p.get("note") or "")}
                for p in (payload.get("photos") or [])
                if isinstance(p, dict) and str(p.get("url") or "")
            ] if isinstance(payload.get("photos"), list) else [],
            "cover_local": cover_local,
            "cover_url": cover_remote,
            "cover": cover_href,
            "track_count": count,
            "tracks": tracks,
            "credits": credits,
            "description": _text(payload.get("description")),
            "urls": url_rows,
            "edited": bool(payload.get("edited")),
            "updated_at": float(updated_at or 0.0),
            "updated_text": human_time(updated_at),
        }
        view["missing"] = missing_fields(payload, track_count=count)
        return view

    @staticmethod
    def _load(payload: Any) -> Mapping[str, Any]:
        if isinstance(payload, Mapping):
            return payload
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, Mapping) else {}

    # -- 筛选 ---------------------------------------------------------------

    def _where(
        self,
        *,
        q: str | None = None,
        event: str | None = None,
        kind: str | None = None,
        year: str | None = None,
        tag: str | None = None,
        circle: str | None = None,
        artist: str | None = None,
        needs: Sequence[str] | None = None,
        has_cover: bool | None = None,
    ) -> tuple[str, list[Any]]:
        """拼 WHERE 子句.

        这里**没有 ``source`` 参数**: 用户端不提供"按来源筛选", 页面也不显示来源。
        """
        cols = self.columns()
        clauses: list[str] = []
        params: list[Any] = []

        if event:
            clauses.append("a.event = ?")
            params.append(str(event))
        if kind:
            clauses.append("a.event_kind = ?")
            params.append(str(kind))
        if year:
            clauses.append("a.year = ?")
            params.append(str(year))
        if circle:
            norm = normalize(circle)
            if "norm_circle" in cols and norm:
                clauses.append("(a.circle = ? OR a.norm_circle = ?)")
                params.extend([str(circle), norm])
            else:
                clauses.append("a.circle = ?")
                params.append(str(circle))
        if tag:
            clauses.append(
                "(EXISTS (SELECT 1 FROM album_tags t WHERE t.album_key = a.key AND t.tag = ?)"
                " OR a.event = ? OR EXISTS (SELECT 1 FROM album_tags t2 WHERE t2.album_key = a.key AND t2.tag LIKE ?))"
            )
            params.extend([str(tag), str(tag), f"%{tag}%"])
        if artist:
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(a.payload, '$.artists') ja WHERE ja.value LIKE ?)"
            )
            params.append(f"%{artist}%")
        if q:
            raw = str(q).strip()
            needle = f"%{raw}%"
            norm = normalize(raw)
            norm_needle = f"%{norm}%" if norm else needle
            parts = [
                "a.title LIKE ?",
                "a.title_original LIKE ?",
                "a.circle LIKE ?",
                "json_extract(a.payload, '$.catalog_no') LIKE ?",
                "EXISTS (SELECT 1 FROM json_each(a.payload, '$.artists') ja WHERE ja.value LIKE ?)",
                "EXISTS (SELECT 1 FROM json_each(a.payload, '$.tracks') jt WHERE jt.value ->> 'title' LIKE ?)",
                # 用户 m16568: 命名的碟(如 Disc 2『Nightmare』)也要能被搜到 ——
                # 流媒体上两碟常以不同名字分别发行, 那个名字就是它的身份之一。
                "EXISTS (SELECT 1 FROM json_each(a.payload, '$.discs') jd WHERE jd.value LIKE ?)",
                # 原作 IP(东方 / 蔚蓝档案 …)与制作人、发布平台也一并纳入
                "json_extract(a.payload, '$.original_work') LIKE ?",
                "json_extract(a.payload, '$.producer') LIKE ?",
                "json_extract(a.payload, '$.platform') LIKE ?",
            ]
            params.extend([needle] * len(parts))
            if "norm_title" in cols:
                parts.append("a.norm_title LIKE ?")
                params.append(norm_needle)
            if "norm_circle" in cols:
                parts.append("a.norm_circle LIKE ?")
                params.append(norm_needle)
            clauses.append("(" + " OR ".join(parts) + ")")
        needs_clauses = _needs_clauses(needs or (), cols)
        if needs_clauses:
            clauses.append("(" + " OR ".join(needs_clauses) + ")")
        if has_cover is True:
            clauses.append(_cover_ok_sql("a"))
        elif has_cover is False:
            clauses.append("NOT " + _cover_ok_sql("a"))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def _order(self, sort: str | None) -> str:
        return {
            "date": "a.release_date DESC, a.circle COLLATE NOCASE, a.title COLLATE NOCASE",
            "title": "a.title COLLATE NOCASE, a.circle COLLATE NOCASE",
            "circle": "a.circle COLLATE NOCASE, a.title COLLATE NOCASE",
            "tracks": "a.track_count DESC, a.circle COLLATE NOCASE, a.title COLLATE NOCASE",
            "event": "a.event DESC, a.circle COLLATE NOCASE, a.title COLLATE NOCASE",
        }.get(str(sort or ""), "a.updated_at DESC, a.title COLLATE NOCASE")

    # -- 专辑列表 -----------------------------------------------------------

    def count_albums(self, **filters: Any) -> int:
        where, params = self._where(**filters)
        return self._count(f"SELECT COUNT(*) FROM albums a{where}", params)

    def query_albums(
        self,
        *,
        page: Any = 1,
        per: Any = DEFAULT_PER,
        sort: str | None = None,
        detailed: bool = False,
        limit: int | None = None,
        **filters: Any,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """返回 ``(视图列表, 分页信息)``."""
        per_v = coerce_per(per)
        pager = paginate(self.count_albums(**filters), page, per_v)
        take = pager["per"] if limit is None else max(1, int(limit))
        offset = pager["offset"] if limit is None else 0
        where, params = self._where(**filters)
        order = self._order(sort)
        rows = self._query(
            f"SELECT a.payload, a.updated_at FROM albums a{where} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, take, offset],
        )
        views = [
            self.make_view(
                self._load(row["payload"]),
                updated_at=row["updated_at"] if "updated_at" in row.keys() else None,
                detailed=detailed,
            )
            for row in rows
        ]
        return views, pager

    def all_views(self, **filters: Any) -> list[dict[str, Any]]:
        """不分页取全部(只给展会页分组这类必须全量的场景用)."""
        where, params = self._where(**filters)
        rows = self._query(
            f"SELECT a.payload, a.updated_at FROM albums a{where} ORDER BY a.circle COLLATE NOCASE,"
            " a.release_date, a.title COLLATE NOCASE",
            params,
        )
        return [
            self.make_view(self._load(row["payload"]),
                           updated_at=row["updated_at"] if "updated_at" in row.keys() else None)
            for row in rows
        ]

    def album_urls(self, *, limit: int = 2000) -> list[tuple[str, float]]:
        """``[(key, updated_at), ...]`` 供 sitemap 生成(按最近更新倒序).

        只读两列 —— sitemap 要给每张专辑一个 URL, 不解析 payload。
        """
        rows = self._query(
            "SELECT key, updated_at FROM albums ORDER BY updated_at DESC LIMIT ?",
            (max(1, int(limit)),),
        )
        out: list[tuple[str, float]] = []
        for row in rows:
            key = _text(row["key"])
            if not key:
                continue
            try:
                updated = float(row["updated_at"] or 0.0) if "updated_at" in row.keys() else 0.0
            except (TypeError, ValueError):
                updated = 0.0
            out.append((key, updated))
        return out

    def get_album(self, key: str) -> dict[str, Any] | None:
        if not key:
            return None
        row = self._one("SELECT payload, updated_at FROM albums WHERE key = ?", (str(key),))
        if row is None:
            return None
        return self.make_view(
            self._load(row["payload"]),
            updated_at=row["updated_at"] if "updated_at" in row.keys() else None,
            detailed=True,
        )

    def cover_lookup(self, key: str) -> tuple[str, str]:
        """``key -> (本地封面路径, 远程封面 URL)``; 找不到返回空串.

        只读两列(不解析整段 payload) —— 封面请求比页面请求频繁得多。
        """
        if not key:
            return "", ""
        cols = self.columns()
        want = [c for c in ("cover_local", "cover_url") if c in cols]
        if not want:
            return "", ""
        row = self._one(f"SELECT {', '.join(want)} FROM albums WHERE key = ?", (str(key),))
        if row is None:
            return "", ""
        keys = row.keys()
        return (
            _text(row["cover_local"]) if "cover_local" in keys else "",
            _text(row["cover_url"]) if "cover_url" in keys else "",
        )

    def album_exists(self, key: str) -> bool:
        """库里到底有没有这张专辑.

        ``/cover/<key>`` 用它区分「确实不存在 -> 404」与
        「存在但没有可用封面 -> 占位图」。
        """
        if not key:
            return False
        return self._one("SELECT 1 FROM albums WHERE key = ?", (str(key),)) is not None

    def outbound_link_urls(self, key: str) -> list[str]:
        """``/link/<key>/<n>`` 用的外链列表(顺序与详情页完全一致).

        与 :func:`make_view` 共用 :func:`_outbound_rows`, 所以序号永远对齐:
        页面上第 n 个「外部链接」就是这里的第 n 条。
        """
        if not key:
            return []
        row = self._one("SELECT payload FROM albums WHERE key = ?", (str(key),))
        if row is None:
            return []
        payload = self._load(row["payload"])
        urls = payload.get("urls")
        if not isinstance(urls, Mapping):
            return []
        return _outbound_rows(urls)

    def resolve_cover_path(self, local: str) -> Path | None:
        """把 ``cover_local`` 解析成允许发送的绝对路径(限定在封面目录内).

        **跨机器兼容**: 库里可能存着**另一台机器上的绝对路径** —— 实测本机是
        ``E:\\...\\data\\covers\\xxx.img``, 搬到 Linux 服务器后直接 resolve
        必然失败, 于是 753 张有封面的专辑全部退化成占位图。
        所以按"文件名"在 ``covers_dir`` 下再找一次: 文件名是稳定的, 目录前缀不是。
        """
        text = _text(local)
        if not text:
            return None
        # Windows 路径在 POSIX 上 ``\\`` 不是分隔符, 统一成正斜杠再取文件名。
        normalized = text.replace("\\", "/")
        candidates: list[Path] = []
        candidate = Path(normalized)
        if not candidate.is_absolute() and self.root is not None:
            candidates.append(self.root / candidate)
        candidates.append(candidate)
        if self.covers_dir is not None:
            name = candidate.name
            if name:
                candidates.append(self.covers_dir / name)
                candidates.append(self.covers_dir / "upload" / name)
            # 保留 ``covers/`` 之后的相对子目录(例如 upload/xxx.jpg)
            parts = candidate.parts
            if "covers" in parts:
                tail = Path(*parts[parts.index("covers") + 1:])
                if str(tail):
                    candidates.append(self.covers_dir / tail)

        for cand in candidates:
            try:
                resolved = cand.resolve()
            except OSError:
                continue
            if not resolved.is_file():
                continue
            if self.covers_dir is not None:
                # 只发封面目录(含子目录, 例如 covers/upload/)里的文件 ——
                # 公开端没有任何理由去读别处的文件。
                try:
                    if not resolved.is_relative_to(self.covers_dir):
                        continue
                except (ValueError, OSError):
                    continue
            return resolved
        if self.covers_dir is not None:
            logger.debug("封面文件找不到(可能在另一台机器上): %s", text)
        return None

    def related_albums(self, album: Mapping[str, Any], *, limit: int = 6) -> list[dict[str, Any]]:
        """同社团的其他专辑(详情页底部)."""
        circle = _text(album.get("circle"))
        key = _text(album.get("key"))
        if not circle:
            return []
        views, _ = self.query_albums(circle=circle, limit=limit + 1, sort="date")
        return [v for v in views if v.get("key") != key][:limit]

    # -- 聚合 ---------------------------------------------------------------

    def event_series(self, *, sort: str = "time",
                     order: str = "desc") -> list[dict[str, Any]]:
        """展会系列列表(每个系列带它的会次, 供索引页卡片用).

        返回::

            [{"name": 系列名, "abbr": 缩写, "full_name": 全称,
              "logo": 图地址, "has_logo": bool,
              "editions": [{"value": 会次名, "display": 展示名, "count": 张数}]}]

        ``count`` 来自 albums 聚合(有多少张专辑)。没有专辑的会次也列出来 ——
        用户可能已经建了词条但还没关联专辑。
        """
        # 专辑数: event -> 张数
        counts: dict[str, int] = {}
        try:
            for row in self._query(
                    "SELECT event, COUNT(*) AS n FROM albums WHERE event != ''"
                    " GROUP BY event"):
                counts[_text(row["event"])] = int(row["n"])
        except sqlite3.Error:
            counts = {}

        # 会次要日期: 取该会次下**最晚的** release_date。
        # 不用 events.year —— 那列有脏数据(见过 3202), release_date 才可靠。
        event_dates: dict[str, str] = {}
        try:
            for row in self._query(
                    "SELECT event, MAX(release_date) AS d FROM albums"
                    " WHERE event != '' AND release_date != ''"
                    " GROUP BY event"):
                d = _text(row["d"])
                if d:
                    event_dates[_text(row["event"])] = d
        except sqlite3.Error:
            event_dates = {}

        out: list[dict[str, Any]] = []
        try:
            raw_series = self._query(
                "SELECT * FROM event_series ORDER BY sort_order, name COLLATE NOCASE")
        except sqlite3.Error:
            raw_series = []

        for row in raw_series:
            keys = row.keys()
            name = _text(row["name"])
            abbr = _text(row["abbr"]) if "abbr" in keys else ""
            full = _text(row["full_name"]) if "full_name" in keys else ""
            logo = ""
            if "logo_local" in keys and _text(row["logo_local"]):
                logo = "/event-series-logo/" + quote(name, safe="")
            elif "logo_url" in keys:
                logo = safe_url(_text(row["logo_url"]))
            editions: list[dict[str, Any]] = []
            try:
                ev_rows = self._query(
                    "SELECT * FROM events WHERE series = ?"
                    " ORDER BY vol DESC, name DESC", (name,))
            except sqlite3.Error:
                ev_rows = []
            # 兜底: 系列改名后可能留下孤儿会次(series 指向旧 key)。
            # 按名字解析能对上本系列的, 也一并列出来 —— 否则那些会次
            # 在任何系列下都查不到, 整届专辑就从索引里消失了。
            try:
                orphans = self._query(
                    "SELECT * FROM events WHERE series = ''"
                    " OR series NOT IN (SELECT name FROM event_series)")
                extra = [ev for ev in orphans
                         if parse_event_name(_text(ev["name"])).get("series")
                         == name]
                if extra:
                    # 合并后**重新排序**, 否则孤儿只是被追加到尾部,
                    # 会次顺序就不是"新->旧"了
                    ev_rows = sorted(
                        list(ev_rows) + extra,
                        key=lambda r: (
                            int(r["vol"] or 0) if "vol" in r.keys() else 0,
                            _text(r["name"])),
                        reverse=True)
            except sqlite3.Error:
                pass
            for ev in ev_rows:
                ek = ev.keys()
                ev_name = _text(ev["name"])
                if not ev_name:
                    continue
                abbr_e = abbr
                vol_label = _text(ev["vol_label"]) if "vol_label" in ek else ""
                # 展示名: "[缩写] 会次自己的名字 (会次标签)".
                #   用**会次名**(如 M3-2026秋 / C108) 而不是系列全称 ——
                #   系列全称对 M3 这种"每年多届"的系列会退化成某一届的名字,
                #   导致所有会次显示成同一个。
                # 中间那段(全称位)取哪个: 会次名通常就对(M3-2026秋),
                # 但像 "C108" 这种**名字 == 会次标签**的, 用系列全称更好看
                #   -> [CM] Comic Market (C108)
                _mid = ev_name
                if vol_label and (ev_name == vol_label
                                  or ev_name.replace(" ", "") == vol_label):
                    _mid = full or name
                # 日期优先级: albums.release_date > events.start_date
                #            > events.end_date > 从名字里抠年份
                _date = event_dates.get(ev_name, "")
                if not _date and "start_date" in ek:
                    _date = _text(ev["start_date"])
                if not _date and "end_date" in ek:
                    _date = _text(ev["end_date"])
                if not _date:
                    _m = re.search(r"(20\d{2})", ev_name)
                    if _m:
                        _date = _m.group(1)
                editions.append({
                    "value": ev_name,
                    "display": display_name(abbr_e, _mid, vol_label,
                                             ev_name),
                    "vol": int(ev["vol"] or 0) if "vol" in ek else 0,
                    "count": counts.get(ev_name, 0),
                    "date": _norm_date(_date),
                })
            # 库里真实的 links(编辑页要回填, 索引页不用)
            try:
                _links_raw = json.loads(_text(row["links_json"])
                                        if "links_json" in keys else "{}")
            except ValueError:
                _links_raw = {}
            if not isinstance(_links_raw, dict):
                _links_raw = {}
            out.append({
                "name": name,
                "abbr": abbr,
                "full_name": full or name,
                "logo": logo,
                "has_logo": bool(logo),
                "poster": ("/event-series-poster/" + quote(name, safe=""))
                if "poster_local" in keys and _text(row["poster_local"]) else
                (safe_url(_text(row["poster_url"]))
                 if "poster_url" in keys else ""),
                "wiki": _text(row["wiki"]) if "wiki" in keys else "",
                # **编辑页回填要用的原始字段** —— 只给站内 URL 的话,
                # 编辑页既拿不到"是否已有图", 也拿不到 wiki/links。
                "logo_local": _text(row["logo_local"])
                if "logo_local" in keys else "",
                "logo_url": _text(row["logo_url"])
                if "logo_url" in keys else "",
                "poster_local": _text(row["poster_local"])
                if "poster_local" in keys else "",
                "poster_url": _text(row["poster_url"])
                if "poster_url" in keys else "",
                "links": {str(k): str(v) for k, v in _links_raw.items() if v},
                "sort_order": int(row["sort_order"] or 0)
                if "sort_order" in keys else 0,
                "editions": editions,
                "count": sum(e["count"] for e in editions),
                # 系列"最新一届"的日期 —— 时间排序用它
                "latest": max((_norm_date(e.get("date")) for e in editions),
                              default="") if editions else "",
                "latest_vol": max((e.get("vol") or 0) for e in editions)
                if editions else 0,
            })
        return _sort_series(out, sort=sort, order=order)

    def events(self) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT event, event_kind, COUNT(*) AS n FROM albums WHERE event != ''"
            " GROUP BY event, event_kind ORDER BY n DESC, event DESC"
        )
        # 展会词条里可能有图片与相关链接 —— 按名字取一次, 挂到对应行。
        # (events 表可能不存在: 老库/只读副本, 那就当没有词条信息。)
        meta: dict[str, dict[str, Any]] = {}
        try:
            for item in self._query("SELECT * FROM events"):
                keys = item.keys()
                name = _text(item["name"])
                if not name:
                    continue
                logo = ""
                if "logo_local" in keys and _text(item["logo_local"]):
                    logo = "/event-logo/" + quote(name, safe="")
                elif "logo_url" in keys:
                    logo = safe_url(_text(item["logo_url"]))
                links: dict[str, str] = {}
                if "links_json" in keys:
                    try:
                        raw = json.loads(_text(item["links_json"]) or "{}")
                    except ValueError:
                        raw = {}
                    if isinstance(raw, Mapping):
                        links = {str(k): str(v) for k, v in raw.items() if v}
                meta[name] = {"logo": logo, "has_logo": bool(logo),
                              "links": links}
        except sqlite3.Error:
            meta = {}
        out: list[dict[str, Any]] = []
        for row in rows:
            name = _text(row["event"])
            entry = {
                "value": name,
                "kind": _text(row["event_kind"]),
                # **显示展会名而不是类型标签** —— 用 `event_kind_label` 会让
                # M3-2026春 / M3-58 / M3-2026秋 全部显示成 "M3", 序号后缀全丢。
                "label": name,
                "kind_label": event_kind_label(row["event_kind"]),
                "count": int(row["n"]),
            }
            entry.update(meta.get(name) or {"logo": "", "has_logo": False,
                                            "links": {}})
            out.append(entry)
        # 用户 m19443: 刚建好的会次可能一张碟都还没有, 以前只按 albums 聚合,
        # 于是建完跳过去能看到详情页, 回索引却找不到它。
        known = {item["value"] for item in out}
        for name, extra in meta.items():
            if name in known:
                continue
            out.append({
                "value": name,
                "kind": "",
                "label": name,
                "kind_label": "",
                "count": 0,
                **extra,
            })
        return out

    def event_kinds(self) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT event_kind, COUNT(*) AS n FROM albums WHERE event_kind != ''"
            " GROUP BY event_kind ORDER BY n DESC"
        )
        return [
            {"value": _text(row["event_kind"]), "label": event_kind_label(row["event_kind"]), "count": int(row["n"])}
            for row in rows
        ]

    def years(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT year, COUNT(*) AS n FROM albums WHERE year != '' AND length(year) = 4"
               " GROUP BY year ORDER BY year DESC")
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [{"value": _text(row["year"]), "count": int(row["n"])} for row in self._query(sql)]

    def tags(self, *, limit: int | None = None, min_count: int = 1,
             include_useless: bool = False) -> list[dict[str, Any]]:
        """公开端标签列表: **只保留有检索意义的标签**.

        "有没有检索意义"的判据在 :mod:`dddb.tagrules`(与后端清理共用同一份规则)。
        这里只做过滤, 不自己定义任何词表 —— 两份规则迟早分叉, 分叉就会出现
        "站内页面按旧规则显示、库里按新规则清理"的错位。

        ``include_useless=True`` 只给调试/测试用(用户端页面永远不传)。
        """
        rows = self._query(
            "SELECT tag, COUNT(*) AS n FROM album_tags GROUP BY tag HAVING n >= ?"
            " ORDER BY n DESC, tag",
            (int(min_count),),
        )
        items: list[dict[str, Any]] = []
        known: set[str] = set()
        for row in rows:
            tag = _text(row["tag"])
            if not include_useless and is_useless_tag(tag):
                continue
            known.add(tag.lower())
            items.append({"value": tag, "count": int(row["n"])})
        # 用户 m19443: 刚建好的标签还没有专辑用它, 只认 album_tags 的话
        # 索引里看不到(建完跳详情页能看到, 回索引就没了)。
        try:
            for row in self._query("SELECT name FROM tags"):
                tag = _text(row["name"])
                if not tag or tag.lower() in known:
                    continue
                if not include_useless and is_useless_tag(tag):
                    continue
                known.add(tag.lower())
                items.append({"value": tag, "count": 0})
        except Exception:        # 旧库 / 只读副本没有 tags 表
            pass
        if limit:
            return items[: max(0, int(limit))]
        return items

    # -- 艺术家索引(从专辑/制作人员聚合) ---------------------------------

    ARTIST_SPLIT = re.compile(r"[、,，;；/|／]+")

    def artists_all(self) -> list[dict[str, Any]]:
        """聚合出艺术家索引: 参与专辑 / 相关社团 / 职业 / 人际关系."""

        def build() -> list[dict[str, Any]]:
            rows = self._query("SELECT key, payload FROM albums")
            bucket: dict[str, dict[str, Any]] = {}

            def entry(name: str) -> dict[str, Any]:
                return bucket.setdefault(name, {
                    "name": name, "albums": {}, "circles": set(),
                    "occupations": set(), "co": set(),
                    # 用户 m19886: 艺人列表/详情卡要显示头像, 头像只存在
                    # artists 表里, 聚合阶段先留个空位, 下面读表时填上。
                    "avatar": "",
                })

            for row in rows:
                try:
                    album = json.loads(row["payload"] or "{}")
                except ValueError:
                    continue
                key = str(row["key"])
                title = str(album.get("title") or key)
                circle = str(album.get("circle") or "").strip()
                # 用户 m19886: 艺人页的「参与专辑」以前只塞 {key,title,href},
                # 卡片拿不到 cover / event / circle / track_count, 于是整片
                # 没有封面、没有展会、没有社团。这里直接存 make_view 的完整
                # 视图(与 circle_detail 一个路数), 卡片要什么有什么。
                view = self.make_view(
                    album,
                    updated_at=row["updated_at"]
                    if "updated_at" in row.keys() else None,
                )
                names: set[str] = set()
                for artist in album.get("artists") or []:
                    text = str(artist).strip()
                    if text:
                        names.add(text)
                for group in album.get("credits") or []:
                    label = str(group.get("label") or "").strip()
                    for name in group.get("names") or []:
                        for part in self.ARTIST_SPLIT.split(str(name or "")):
                            item = part.strip()
                            if not item:
                                continue
                            names.add(item)
                            if label:
                                entry(item)["occupations"].add(label)
                for track in album.get("tracks") or []:
                    for artist in track.get("artists") or []:
                        text = str(artist).strip()
                        if text:
                            names.add(text)
                for name in names:
                    e = entry(name)
                    e["albums"][key] = view
                    if circle:
                        e["circles"].add(circle)
                    for other in names:
                        if other != name:
                            e["co"].add(other)

            # 用户 m19443: 刚建好的艺人还没有任何专辑, 只认 albums 表的话
            # 索引里找不到他(建完跳到详情页, 回列表却"查无此人")。
            try:
                for row in self._query("SELECT name, payload FROM artists"):
                    name = _text(row["name"])
                    if not name:
                        continue
                    try:
                        overlay = json.loads(row["payload"] or "{}")
                    except (ValueError, TypeError):
                        overlay = {}
                    avatar = _text((overlay or {}).get("avatar"))
                    for part in self.ARTIST_SPLIT.split(name):
                        item = part.strip()
                        if not item:
                            continue
                        hit = [k for k in bucket if k.lower() == item.lower()]
                        if not hit:
                            entry(item)
                        if avatar:
                            # 大小写/写法差异下可能命中已有桶, 一并补上头像
                            for k in (hit or [item]):
                                slot = bucket.get(k)
                                if slot is not None and not slot.get("avatar"):
                                    slot["avatar"] = avatar
            except Exception:        # 旧库 / 只读副本没有 artists 表
                pass

            out: list[dict[str, Any]] = []
            for name, e in bucket.items():
                out.append({
                    "name": name,
                    "album_count": len(e["albums"]),
                    # 未命中的视图兜个底(比如来源行没有 payload), 卡片
                    # 至少还能显示标题, 不至于因为缺键整张卡片炸掉。
                    "albums": [
                        view if isinstance(view, dict) else {
                            "key": k, "title": t, "href": _album_href(k)}
                        for k, view in e["albums"].items()
                        for t in [str((view or {}).get("title") or k)]
                    ],
                    "circles": sorted(e["circles"]),
                    "occupations": sorted(e["occupations"]),
                    "relations": sorted(e["co"]),
                    "avatar": e.get("avatar") or "",
                })
            out.sort(key=lambda item: (-item["album_count"], item["name"]))
            return out

        return self.cached("artists_all", build, ttl=60.0)

    def artist_detail(self, name: Any) -> dict[str, Any] | None:
        """单个艺术家的完整资料(自动聚合 + artists 表里的正式字段覆盖)."""
        wanted = str(name or "").strip()
        if not wanted:
            return None
        derived = None
        for item in self.artists_all():
            if item["name"].lower() == wanted.lower():
                derived = item
                break
        overlay: dict[str, Any] = {}
        try:
            rows = self._query(
                "SELECT payload FROM artists WHERE name = ? COLLATE NOCASE", (wanted,))
            if rows:
                overlay = json.loads(rows[0]["payload"] or "{}")
        except Exception:            # 只读库还没建 artists 表 / 旧库
            overlay = {}
        if derived is None and not overlay:
            return None
        detail = dict(derived or {"name": wanted, "albums": [], "circles": [],
                                  "occupations": [], "relations": [],
                                  "album_count": 0})
        detail["name"] = str(overlay.get("name") or wanted)
        detail["overlay"] = overlay
        detail["bio"] = str(overlay.get("bio") or "")
        detail["avatar"] = str(overlay.get("avatar") or "")
        detail["nationality"] = str(overlay.get("nationality") or "")
        detail["real_name"] = str(overlay.get("real_name") or "")
        detail["real_name_en"] = str(overlay.get("real_name_en") or "")
        detail["name_en"] = str(overlay.get("name_en") or "")
        detail["name_ja"] = str(overlay.get("name_ja") or "")
        detail["aliases"] = [str(a) for a in (overlay.get("aliases") or []) if str(a).strip()]
        detail["gender"] = str(overlay.get("gender") or "")
        detail["birthday"] = str(overlay.get("birthday") or "")
        detail["occupations_text"] = str(overlay.get("occupations_text") or "")
        detail["links"] = [
            {"href": str(url.get("href") or ""), "text": str(url.get("text") or "链接")}
            for url in (overlay.get("links") or []) if isinstance(url, Mapping) and url.get("href")
        ]
        return detail

    # -- 社团详情 ---------------------------------------------------------

    def circle_detail(self, name: Any) -> dict[str, Any] | None:
        """社团详情: 资料卡 + 参与专辑 + 相关人士(成员/过往成员/客串)."""
        wanted = str(name or "").strip()
        if not wanted:
            return None
        # 用户 m19443: 刚建好的社团**一张碟都还没有**, 以前这里只看 albums 表,
        # 于是 303 跳过去就是「404 没有这个社团」——"已创建"和 404 同时出现。
        # 所以先把 circles 表里的词条读出来, 有词条就算存在。
        circle_info: dict[str, Any] = {}
        try:
            found = self._query(
                "SELECT payload FROM circles WHERE name = ? COLLATE NOCASE LIMIT 1", (wanted,))
            if found:
                circle_info = json.loads(found[0]["payload"] or "{}")
        except Exception:            # 旧库没有 circles 表时忽略
            circle_info = {}
        rows = self._query(
            "SELECT key, title, payload FROM albums WHERE circle = ? COLLATE NOCASE"
            " ORDER BY release_date DESC, key", (wanted,))
        if not rows:
            # 名字大小写/写法差异: 退回到聚合列表里找
            match = next((c for c in self.circles_all()
                          if c["value"].lower() == wanted.lower()
                          or wanted.lower() in [v.lower() for v in c.get("variants", [])]), None)
            if match is not None:
                rows = self._query(
                    "SELECT key, title, payload FROM albums WHERE circle IN ("
                    + ",".join("?" for _ in match["variants"]) + ")"
                    " ORDER BY release_date DESC, key", tuple(match["variants"]))
                wanted = match["value"]
            elif not circle_info:
                # 既没有专辑、也没有词条 -> 确实不存在
                return None
        albums: list[dict[str, Any]] = []
        events: list[str] = []
        dates: list[str] = []
        people: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                data = json.loads(row["payload"] or "{}")
            except ValueError:
                continue
            key = str(row["key"])
            # **用 make_view 生成完整视图**: 卡片需要 has_cover / cover /
            # track_count 等字段, 手工拼的那几个键会让封面变占位块、
            # 曲目数恒为 0(有曲目也显示"无曲目")。
            view = self.make_view(
                data, updated_at=row["updated_at"]
                if "updated_at" in row.keys() else None,
            )
            view["date"] = str(data.get("date") or view.get("date") or "")
            view["event"] = str(data.get("event") or view.get("event") or "")
            albums.append(view)
            if data.get("event"):
                events.append(str(data["event"]))
            if data.get("date"):
                dates.append(str(data["date"]))
            for group in data.get("credits") or []:
                label = str(group.get("label") or "").strip()
                for raw in group.get("names") or []:
                    for part in self.ARTIST_SPLIT.split(str(raw or "")):
                        person = part.strip()
                        if not person or person.lower() == wanted.lower():
                            continue
                        slot = people.setdefault(person, {"name": person, "roles": set(),
                                                          "albums": 0})
                        slot["albums"] += 1
                        if label:
                            slot["roles"].add(label)
        # 成员/过往/客串 的启发式分类
        current_year = 0
        for value in dates:
            digits = re.findall(r"(\d{4})", value)
            if digits:
                current_year = max(current_year, int(digits[0]))
        latest = current_year or 0

        def _year_of(person: dict[str, Any]) -> int:
            return latest

        member: list[dict[str, Any]] = []
        former: list[dict[str, Any]] = []
        guest: list[dict[str, Any]] = []
        role_keywords = ("主催", "代表", "作家", "作曲", "编曲", "作词", "绘画",
                         "插画", "设计", "デザイン", "イラスト", "音楽", "音乐")
        for slot in people.values():
            item = {"name": slot["name"], "roles": sorted(slot["roles"]),
                    "albums": slot["albums"]}
            core = any(any(word in role for word in role_keywords) for role in item["roles"])
            if slot["albums"] >= 2 and core:
                member.append(item)
            elif slot["albums"] >= 2:
                member.append(item)
            elif core:
                guest.append(item)
            else:
                guest.append(item)
        order = lambda p: (-p["albums"], p["name"])
        return {
            "name": wanted,
            # 用户 m19886: 上传的徽标以前只写 logo_local, 而这里只读
            # logo_url -> 社徽永远显示不出来。两个都认(logo_url 优先,
            # 它是 `/logo/...` 站内地址或远程直链)。
            "logo": str(circle_info.get("logo_url")
                        or circle_info.get("logo_local") or ""),
            "description": str(circle_info.get("description") or ""),
            "activity_type": str(circle_info.get("activity_type") or ""),
            "founded": str(circle_info.get("founded") or ""),
            "links": [{"href": str(u.get("href") or ""), "text": str(u.get("text") or "链接")}
                      for u in (circle_info.get("urls") or []) if isinstance(u, Mapping)],
            "albums": albums,
            "events": sorted(set(events)),
            "first_event": next(iter(sorted(set(events))), ""),
            "last_event": next(iter(sorted(set(events), reverse=True)), ""),
            "first_date": min(dates) if dates else "",
            "last_date": max(dates) if dates else "",
            "latest_year": latest,
            "members": sorted(member, key=order)[:40],
            "former_members": sorted(former, key=order)[:40],
            "guests": sorted(guest, key=order)[:40],
        }

    @staticmethod
    def _split_names(value: Any) -> list[str]:
        return [p.strip() for p in re.split(r"[、,，;；/|／]+", str(value or "")) if p.strip()]

    def circles_all(self) -> list[dict[str, Any]]:
        """``norm_circle`` 聚合后的社团列表(合并 "xxx (yyy)" 这类写法差异).

        1329 个社团 / 1783 行数据在 Python 里聚合一次不到几毫秒, 比写一堆
        SQL 窗口函数好懂; 结果按 TTL 缓存。
        """

        def build() -> list[dict[str, Any]]:
            rows = self._query("SELECT circle, norm_circle FROM albums WHERE circle != ''")
            buckets: dict[str, dict[str, Any]] = {}
            for row in rows:
                name = _text(row["circle"])
                if not name:
                    continue
                try:
                    norm = _text(row["norm_circle"]) or normalize(name)
                except (IndexError, KeyError):
                    norm = normalize(name)
                entry = buckets.setdefault(norm, {"value": name, "count": 0, "variants": {}, "best": 0})
                entry["count"] += 1
                entry["variants"][name] = entry["variants"].get(name, 0) + 1
            out: list[dict[str, Any]] = []
            for entry in buckets.values():
                variants = sorted(entry["variants"], key=lambda v: (-entry["variants"][v], -len(v), v))
                out.append({
                    "value": variants[0],
                    "count": entry["count"],
                    "variants": variants,
                    "norm": normalize(variants[0]),
                })
            # 用户 m19443: 刚建好的社团还没有专辑, 只认 albums 表的话索引里根本
            # 找不到它(建完跳到详情页, 回列表却"查无此社")。把 circles 词条表里
            # 多出来的名字也并进来, count 记 0。
            try:
                known = {item["norm"] for item in out}
                for row in self._query("SELECT name FROM circles"):
                    name = _text(row["name"])
                    if not name or normalize(name) in known:
                        continue
                    known.add(normalize(name))
                    out.append({
                        "value": name,
                        "count": 0,
                        "variants": [name],
                        "norm": normalize(name),
                    })
            except Exception:        # 旧库 / 只读副本没有 circles 表
                pass
            out.sort(key=lambda c: (-c["count"], c["value"]))
            return out

        return self.cached("circles_all", build, ttl=60.0)

    def circles(self, *, query: str = "", page: Any = 1, per: int = 60) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        entries = self.circles_all()
        needle = _text(query).lower()
        if needle:
            entries = [
                e for e in entries
                if needle in e["value"].lower() or any(needle in v.lower() for v in e["variants"])
            ]
        pager = paginate(len(entries), page, per)
        start = pager["offset"]
        return entries[start:start + pager["per"]], pager

    def circle_variants(self, name: str) -> list[str]:
        norm = normalize(name)
        for entry in self.circles_all():
            if entry["norm"] == norm or entry["value"] == name:
                return list(entry["variants"])
        return [name] if name else []

    def album_count(self) -> int:
        return self._count("SELECT COUNT(*) FROM albums")

    # -- 公开端评论(读路径) -------------------------------------------------
    #
    # 读走只读连接, 写走 :meth:`PublicSite.writable_store` —— 与既有架构一致。
    # 只读副本 / 尚未执行过迁移的旧库可能**没有** comments 表, 这里一律优雅降级
    # 成"没有评论", 绝不让详情页 500。

    def comments_available(self) -> bool:
        """comments 表是否存在(探测一次并缓存)."""
        if self._comments_ready is not None:
            return self._comments_ready
        try:
            self._query("SELECT 1 FROM comments LIMIT 1")
            self._comments_ready = True
        except sqlite3.Error as exc:
            logger.info("评论表不可用(评论功能降级为空): %s", exc)
            self._comments_ready = False
        return self._comments_ready

    def list_comments(
        self,
        target_kind: Any,
        target_key: Any,
        *,
        page: Any = 1,
        per: Any = COMMENT_PER_PAGE,
        include_hidden: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """``(评论视图列表, 分页信息)``, 按时间倒序."""
        if not self.comments_available():
            return [], paginate(0, page, coerce_comment_per(per))
        kind = _text(target_kind).lower()
        key = _text(target_key)
        if kind not in ("album", "circle", "artist") or not key:
            return [], paginate(0, page, coerce_comment_per(per))
        total = self.count_comments(kind, key, include_hidden=include_hidden)
        pager = paginate(total, page, coerce_comment_per(per))
        try:
            rows = self._query(
                "SELECT * FROM comments WHERE target_kind = ? AND target_key = ?"
                + ("" if include_hidden else " AND status = 'visible'")
                + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (kind, key, pager["per"], pager["offset"]),
            )
        except sqlite3.Error as exc:
            logger.warning("读取评论失败(降级为空): %s", exc)
            return [], paginate(0, page, pager["per"])
        return [comment_view(row) for row in rows], pager

    def get_comment(self, comment_id: Any) -> dict[str, Any] | None:
        """按 id 取一条评论(删除/举报前做归属校验用)."""
        if not self.comments_available():
            return None
        try:
            key = int(comment_id or 0)
        except (TypeError, ValueError):
            return None
        if key <= 0:
            return None
        try:
            rows = self._query("SELECT * FROM comments WHERE id = ?", (key,))
        except sqlite3.Error:                  # pragma: no cover
            return None
        row = next(iter(rows), None)
        return comment_view(row) if row is not None else None

    def comments_by(self, username: Any, *, limit: int = 100) -> list[dict[str, Any]]:
        """某用户发表过的评论(用于"我的评论"), 时间倒序."""
        if not self.comments_available():
            return []
        name = _text(username)
        if not name:
            return []
        try:
            rows = self._query(
                "SELECT * FROM comments WHERE username = ? COLLATE NOCASE"
                " AND status = 'visible' ORDER BY created_at DESC, id DESC LIMIT ?",
                (name, max(1, int(limit))),
            )
        except sqlite3.Error as exc:            # pragma: no cover - 读失败降级
            logger.warning("读取我的评论失败(降级为空): %s", exc)
            return []
        return [comment_view(row) for row in rows]

    def count_comments(
        self, target_kind: Any, target_key: Any, *, include_hidden: bool = False
    ) -> int:
        if not self.comments_available():
            return 0
        kind = _text(target_kind).lower()
        key = _text(target_key)
        if kind not in ("album", "circle") or not key:
            return 0
        return self._count(
            "SELECT COUNT(*) FROM comments WHERE target_kind = ? AND target_key = ?"
            + ("" if include_hidden else " AND status = 'visible'"),
            (kind, key),
        )

    def target_exists(self, target_kind: Any, target_key: Any) -> bool:
        """评论目标是否真的存在(防止往不存在的 key 上挂评论)."""
        kind = _text(target_kind).lower()
        key = _text(target_key)
        if not key:
            return False
        try:
            if kind == "album":
                return self._one("SELECT 1 FROM albums WHERE key = ?", (key,)) is not None
            if kind == "circle":
                return self._one("SELECT 1 FROM albums WHERE circle = ? LIMIT 1", (key,)) is not None
        except sqlite3.Error as exc:  # pragma: no cover - 库级故障
            logger.warning("校验评论目标失败: %s", exc)
        return False

    def comment_totals(self) -> dict[str, int]:
        """工作队列 / 站点概览要用的评论计数(表不存在时全 0)."""
        if not self.comments_available():
            return {"visible": 0, "hidden": 0, "total": 0}
        out = {"visible": 0, "hidden": 0, "total": 0}
        try:
            rows = self._query("SELECT status, COUNT(*) AS n FROM comments GROUP BY status")
        except sqlite3.Error:  # pragma: no cover
            return out
        for row in rows:
            status = _text(row["status"]) or "visible"
            count = int(row["n"])
            out[status] = count
            out["total"] += count
        return out

    def search_suggestions(self, query: str, *, limit: int = 6) -> dict[str, list[dict[str, Any]]]:
        """零结果时给出的近似命中(标签 / 展会 / 社团).

        搜索条件只覆盖标题/社团/艺人/曲目/编号, **不含标签**; 但访客习惯直接
        拿标签名去搜 —— 实测 ``q=東方Project`` 会 0 命中, 而库里明明有
        15 张打了这个标签。这里在"搜不到"的页面上把这几个入口递过去, 而不是
        偷偷扩大搜索语义(那会让搜索结果变得难以解释)。
        """
        text = _text(query)
        if not text:
            return {}
        needle = f"%{text}%"
        norm = normalize(text)
        out: dict[str, list[dict[str, Any]]] = {}
        tags = self._query(
            "SELECT tag, COUNT(*) AS n FROM album_tags WHERE tag LIKE ?"
            " GROUP BY tag ORDER BY n DESC, tag LIMIT ?", (needle, int(limit)))
        if tags:
            from .templates import quote_segment
            out["tags"] = [
                {"value": _text(row["tag"]), "count": int(row["n"]),
                 "href": "/tag/" + quote_segment(_text(row["tag"]))}
                for row in tags
            ]
        events = self._query(
            "SELECT event, COUNT(*) AS n FROM albums WHERE event != '' AND event LIKE ?"
            " GROUP BY event ORDER BY n DESC, event LIMIT ?", (needle, int(limit)))
        if events:
            from .templates import quote_segment
            out["events"] = [
                {"value": _text(row["event"]), "count": int(row["n"]),
                 "href": "/event/" + quote_segment(_text(row["event"]))}
                for row in events
            ]
        lowered = text.lower()
        circles = [c for c in self.circles_all() if lowered in c["value"].lower()][:limit]
        if circles:
            from .templates import quote_segment
            out["circles"] = [
                {"value": c["value"], "count": c["count"],
                 "href": "/circle/" + quote_segment(c["value"])}
                for c in circles
            ]
        return out

    def totals(self) -> dict[str, Any]:
        """页头小计数(带 TTL 缓存)."""
        return self.cached("totals", lambda: {
            "albums": self.album_count(),
            "circles": len(self.circles_all()),
            "events": self._count("SELECT COUNT(*) FROM (SELECT 1 FROM albums WHERE event != '' GROUP BY event)"),
            "tracks": self._count("SELECT COALESCE(SUM(track_count), 0) FROM albums"),
        })

    def stats(self) -> dict[str, Any]:
        """统计看板所需的全部数字."""
        return self.cached("stats", self._stats, ttl=30.0)

    def _stats(self) -> dict[str, Any]:
        albums = self.album_count()
        tracks = self._count("SELECT COALESCE(SUM(track_count), 0) FROM albums")
        local = self._count("SELECT COUNT(*) FROM albums WHERE cover_local != ''")
        remote_only = self._count(
            "SELECT COUNT(*) FROM albums WHERE cover_local = ''"
            " AND (cover_url LIKE 'http://%' OR cover_url LIKE 'https://%')")
        none = self._count("SELECT COUNT(*) FROM albums WHERE NOT " + _cover_ok_sql())
        completeness = {
            "no_tracks": self._count("SELECT COUNT(*) FROM albums WHERE track_count = 0"),
            "no_desc": self._count("SELECT COUNT(*) FROM albums WHERE COALESCE(json_extract(payload,'$.description'),'') = ''"),
            "no_cover": none,
            "no_circle": self._count("SELECT COUNT(*) FROM albums WHERE circle = ''"),
            "no_date": self._count("SELECT COUNT(*) FROM albums WHERE release_date = ''"),
            "no_credits": self._count("SELECT COUNT(*) FROM albums WHERE COALESCE(json_array_length(payload,'$.credits'),0) = 0"),
        }
        years = list(reversed(self.years(limit=30)))
        kinds = self.event_kinds()
        events = self.events()
        genres = [
            {"value": _text(row["value"]), "count": int(row["n"])}
            for row in self._query(
                "SELECT je.value AS value, COUNT(*) AS n FROM albums a, json_each(a.payload, '$.genres') je"
                " WHERE je.value != '' GROUP BY je.value ORDER BY n DESC, je.value LIMIT 20"
            )
        ]
        has_profile = self._count(
            "SELECT COUNT(*) FROM albums WHERE COALESCE(json_extract(payload,'$.description'),'') != ''")
        return {
            "albums": albums,
            "tracks": tracks,
            "circles": len(self.circles_all()),
            "events": len(events),
            "kinds": kinds,
            "tags": len(self.tags()),
            "edited": self._count("SELECT COUNT(*) FROM albums WHERE json_extract(payload,'$.edited') = 1"),
            "has_profile": has_profile,
            "coverage": {
                "local": local,
                "remote": remote_only,
                "none": none,
                "rate": round((local + remote_only) * 100.0 / albums, 1) if albums else 0.0,
            },
            "completeness": completeness,
            "years": years,
            "event_top": [{**e, "label": e["value"]} for e in events[:12]],
            "circles_top": [{"value": c["value"], "label": c["value"], "count": c["count"]}
                            for c in self.circles_all()[:10]],
            "genres": genres,
            "tags_top": self.tags(limit=20),
        }

    # -- 编辑者队列 ---------------------------------------------------------

    def needs_counts(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for key, label in NEEDS_KINDS:
            where, params = self._where(needs=[key])
            out.append({
                "value": key,
                "label": label,
                "count": self._count(f"SELECT COUNT(*) FROM albums a{where}", params),
            })
        return out

    def inbox(self, kind: str = "", *, page: Any = 1, per: int = DEFAULT_PER) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if kind and kind not in NEEDS_KEYS:
            kind = ""
        views, pager = self.query_albums(needs=[kind] if kind else [], page=page, per=per, sort="updated")
        return views, pager

    def inbox_total(self) -> int:
        kinds = self.needs_counts()
        where, params = self._where(needs=[k["value"] for k in kinds])
        return self._count(f"SELECT COUNT(*) FROM albums a{where}", params)

    # -- 展会分组 -----------------------------------------------------------

    def event_groups(
        self,
        event: str,
        *,
        group_by: str = "circle",
        page: Any = 1,
        per_groups: int = 12,
        per_group: int = 8,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
        """展会的专辑按社团或发售日分组, 组内每社团最多 ``per_group`` 张.

        一个热门展会有一千多张专辑, 全部铺开既慢又没法看; 所以对**组**分页,
        组内只展示前若干张(点社团名可以看全部)。
        """
        views = self.all_views(event=event)
        buckets: dict[str, dict[str, Any]] = {}
        for view in views:
            if group_by == "date":
                label = view.get("date") or "未标注发售日"
            else:
                label = view.get("circle") or "未标注社团"
            entry = buckets.setdefault(label, {"label": label, "count": 0, "items": []})
            entry["count"] += 1
            if len(entry["items"]) < max(1, int(per_group)):
                entry["items"].append(view)
        if group_by == "date":
            # 已知发售日按日期倒序, "未标注" 永远压在最后
            groups = sorted(buckets.values(), key=lambda g: str(g["label"]), reverse=True)
            unknown = [g for g in groups if g["label"] == "未标注发售日"]
            groups = [g for g in groups if g["label"] != "未标注发售日"] + unknown
        else:
            groups = sorted(buckets.values(), key=lambda g: (-g["count"], g["label"]))
        pager = paginate(len(groups), page, per_groups)
        start = pager["offset"]
        return groups[start:start + pager["per"]], pager, len(views)


def coerce_per(per: Any) -> int:
    """每页条数只允许白名单内的值 —— 防止 ``?per=100000`` 把整库拉出来."""
    try:
        value = int(per)
    except (TypeError, ValueError):
        return DEFAULT_PER
    return value if value in PER_PAGE_CHOICES else DEFAULT_PER


def _needs_clauses(needs: Sequence[str], cols: set[str]) -> list[str]:
    """信息缺口 -> SQL 片段(每种缺口一条 OR 分支)."""
    table: dict[str, str] = {
        "no_cover": "NOT " + _cover_ok_sql("a"),
        "no_tracks": "a.track_count = 0",
        "no_desc": "COALESCE(json_extract(a.payload, '$.description'), '') = ''",
        "no_circle": "a.circle = ''",
        "no_date": "a.release_date = ''",
        "no_catalog": "COALESCE(json_extract(a.payload, '$.catalog_no'), '') = ''",
        "no_credits": "COALESCE(json_array_length(a.payload, '$.credits'), 0) = 0",
        "no_source_url": "NOT EXISTS (SELECT 1 FROM json_each(a.payload, '$.urls'))",
    }
    out: list[str] = []
    for key in needs:
        clause = table.get(str(key))
        if clause:
            out.append(clause)
    return out


def parse_filters(query: Mapping[str, Any], *, extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """把 URL 查询参数规整成筛选字典(白名单 + 类型转换).

    非法值(未知排序、负页码)一律丢弃而不是报错 —— 公开站点的 URL 是用户能
    随手改的, 不该因为 ``?page=-3`` 就 500。
    """
    def first(name: str) -> str:
        value = query.get(name)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        return str(value or "").strip()

    filters: dict[str, Any] = {}
    text_fields = ("q", "event", "kind", "year", "tag", "circle", "artist")
    for field in text_fields:
        value = first(field)
        if value:
            filters[field] = value
    needs_raw = first("needs")
    if needs_raw:
        needs = [part for part in needs_raw.replace(",", " ").split() if part in NEEDS_KEYS]
        if needs:
            filters["needs"] = needs
    if first("has_cover") in ("1", "true", "on", "yes"):
        filters["has_cover"] = True
    sort = first("sort")
    if sort in SORT_KEYS:
        filters["sort"] = sort
    if extra:
        for key, value in extra.items():
            if value not in (None, ""):
                filters[key] = value
    # 递进筛选: 具体展会次已经唯一确定了年份 —— 两者同时出现在 URL 里时
    # **以 event 为准**(旧链接 ?year=2026&event=M3-2026秋 不能因此查出 0 条)。
    if filters.get("event"):
        filters.pop("year", None)
    return filters


__all__ = [
    "PublicData",
    "PublicDataError",
    "ReadOnlyStore",
    "SORTS",
    "SORT_KEYS",
    "DEFAULT_SORT",
    "PER_PAGE_CHOICES",
    "DEFAULT_PER",
    "NEEDS_KINDS",
    "NEEDS_KEYS",
    "paginate",
    "parse_filters",
    "coerce_per",
    "normalize",
    "human_time",
    "group_credits",
    "missing_fields",
]