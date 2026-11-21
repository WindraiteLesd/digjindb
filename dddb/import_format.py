"""dddb 导入格式: 统一「专辑 + 曲目 + 词条」的导入载荷.

用户要求:
    "把现有的将本地数据导入至线上数据库的功能按该项目的标准和代码来实现,
     输出的不需要是 bangumi infobox, 而是做成我们 dddb 的导入格式。"

设计:
    一个 **ImportDraft** 表示"准备导入线上的一张专辑"，包含:

        meta      标题/社团/艺人/展会/发售日/... (表单单值字段)
        cover     封面(本地文件或 URL)
        credits   制作人员(role/name/original/alias/raw_role)
        urls      外链(label -> url)
        tracks    曲目(disc/index/title/artists/duration/...)
        source    来源(None / "netease:<album_id>" / "playlist:<id>")
        warnings  需要人看一眼的地方(缺发售日 / 职位未识别 / ...)

    它可以从三个入口生成:
        ① ``from_netease_album()``  —— 贴网易云专辑/歌单链接
        ② ``from_local_album()``    —— 本地库里已有的专辑
        ③ ``from_playlist_missing()`` —— 歌单对比里"线上没有"的那些

    三种入口产出**同一个结构**，于是"导入线上"只有一条代码路径。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

#: 表单里所有单值字段(与线上新建页一一对应)
FORM_TEXT_FIELDS = (
    "title_original", "title", "title_romaji", "circle", "company",
    "event", "release_date", "catalog_no", "price", "hall", "booth",
    "tags", "genres", "description", "artists",
)


@dataclass
class ImportDraft:
    """一张待导入线上的专辑(完整字段)."""

    #: 稳定标识: 用 title|circle 或 netease:id
    key: str = ""
    #: 来源说明(给人看): "网易云专辑 398969728" / "本地库" / "歌单 xxx"
    source: str = ""
    #: 单值字段
    meta: dict[str, str] = field(default_factory=dict)
    #: 封面: 本地路径(优先)或远程 URL
    cover_file: str = ""
    cover_url: str = ""
    #: 制作人员
    credits: list[dict[str, Any]] = field(default_factory=list)
    #: 外链 {标签: URL}
    urls: dict[str, str] = field(default_factory=dict)
    #: 曲目
    tracks: list[dict[str, Any]] = field(default_factory=list)
    #: 需要人看一眼的地方
    warnings: list[str] = field(default_factory=list)
    #: 歌词里认不出的职位原文
    unrecognized_roles: list[str] = field(default_factory=list)
    #: 生成时间
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = time.time()

    # -- 派生 ----------------------------------------------------------

    @property
    def title(self) -> str:
        return self.meta.get("title_original") or \
            self.meta.get("title") or self.key

    @property
    def track_count(self) -> int:
        return len(self.tracks)

    def summary(self) -> dict[str, Any]:
        """给界面列表用的一行摘要."""
        return {
            "key": self.key,
            "title": self.title,
            "circle": self.meta.get("circle") or "",
            "event": self.meta.get("event") or "",
            "release_date": self.meta.get("release_date") or "",
            "tracks": self.track_count,
            "credits": len(self.credits),
            "urls": len(self.urls),
            "has_cover": bool(self.cover_file or self.cover_url),
            "warnings": len(self.warnings),
            "source": self.source,
        }

    # -- 序列化 --------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "source": self.source,
            "meta": dict(self.meta),
            "cover_file": self.cover_file,
            "cover_url": self.cover_url,
            "credits": [dict(c) for c in self.credits],
            "urls": dict(self.urls),
            "tracks": [dict(t) for t in self.tracks],
            "warnings": list(self.warnings),
            "unrecognized_roles": list(self.unrecognized_roles),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ImportDraft":
        d = dict(data or {})
        return cls(
            key=str(d.get("key") or ""),
            source=str(d.get("source") or ""),
            meta={k: str(v) for k, v in (d.get("meta") or {}).items()},
            cover_file=str(d.get("cover_file") or ""),
            cover_url=str(d.get("cover_url") or ""),
            credits=[dict(c) for c in (d.get("credits") or [])],
            urls={str(k): str(v) for k, v in (d.get("urls") or {}).items()},
            tracks=[dict(t) for t in (d.get("tracks") or [])],
            warnings=[str(w) for w in (d.get("warnings") or [])],
            unrecognized_roles=[str(w) for w in
                                (d.get("unrecognized_roles") or [])],
            created_at=float(d.get("created_at") or 0),
        )

    def to_form_payload(self) -> dict[str, Any]:
        """转成 **线上新建页表单** 能直接填的结构.

        与 ``webui._push_to_cloud`` 产出的 ``payload`` 同构 ——
        这样 CDP 填表、桥页书签都能直接用。
        """
        out: dict[str, Any] = {k: str(self.meta.get(k) or "")
                               for k in FORM_TEXT_FIELDS}
        out["credits"] = [
            {
                "role": str(c.get("role") or ""),
                "name": str(c.get("name") or ""),
                "original": str(c.get("original") or ""),
                "alias": str(c.get("alias") or ""),
                "raw_role": str(c.get("raw_role") or ""),
            }
            for c in self.credits if str(c.get("name") or "").strip()
        ]
        out["urls"] = [{"key": k, "value": v}
                       for k, v in self.urls.items() if v]
        out["tracks"] = [
            {
                "disc": str(t.get("disc") or 1),
                "index": str(t.get("index") or i + 1),
                "title": str(t.get("title") or ""),
                "subtitle": str(t.get("subtitle") or ""),
                "title_zh": str(t.get("title_zh") or ""),
                "artists": _join_artists(t.get("artists")),
                "lang": str(t.get("lang") or ""),
                "genre": str(t.get("genre") or ""),
                "duration": _fmt_duration(t.get("duration")),
            }
            for i, t in enumerate(self.tracks)
        ]
        out["cover_file"] = self.cover_file
        out["cover_url"] = self.cover_url
        out["album_key"] = self.key
        return out


def _join_artists(value: Any) -> str:
    if isinstance(value, list):
        return "、".join(str(a).strip() for a in value if str(a).strip())
    return str(value or "").strip()


def _fmt_duration(seconds: Any) -> str:
    try:
        n = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return "{}:{:02d}".format(n // 60, n % 60)


# ---------------------------------------------------------------------------
# 入口 ①: 本地库里的专辑
# ---------------------------------------------------------------------------

def from_local_album(db_path: Path, album_key: str, *,
                     cache_dir: Path | None = None) -> ImportDraft:
    """本地库的一张专辑 -> ImportDraft."""
    import sqlite3

    db_path = Path(db_path)
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

    return from_payload(payload, album_key=album_key, source="本地库",
                        cache_dir=cache_dir)


def from_payload(payload: Mapping[str, Any], *, album_key: str,
                 source: str = "",
                 cache_dir: Path | None = None) -> ImportDraft:
    """albums.payload -> ImportDraft(本地库 / 歌单共用)."""
    d = dict(payload or {})
    meta = {
        "title_original": str(d.get("title_original") or ""),
        "title": str(d.get("title") or ""),
        "title_romaji": str(d.get("title_romaji") or ""),
        "circle": str(d.get("circle") or ""),
        "company": str(d.get("company") or ""),
        "event": str(d.get("event") or ""),
        "release_date": str(d.get("release_date") or ""),
        "catalog_no": str(d.get("catalog_no") or ""),
        "price": str(d.get("price") or ""),
        "hall": str(d.get("hall") or ""),
        "booth": str(d.get("booth") or ""),
        "tags": "、".join(str(x) for x in (d.get("tags") or []))
        if isinstance(d.get("tags"), list) else str(d.get("tags") or ""),
        "genres": "、".join(str(x) for x in (d.get("genres") or []))
        if isinstance(d.get("genres"), list) else str(d.get("genres") or ""),
        "description": str(d.get("description") or ""),
        "artists": _join_artists(d.get("artists")),
    }

    credits: list[dict[str, Any]] = []
    for c in (d.get("credits") or []):
        if not isinstance(c, Mapping) or not str(c.get("name") or "").strip():
            continue
        item = {
            "role": str(c.get("role") or ""),
            "name": str(c.get("name") or ""),
        }
        for k in ("original", "alias", "raw_role"):
            if c.get(k):
                item[k] = str(c[k])
        credits.append(item)

    urls: dict[str, str] = {}
    raw_urls = d.get("urls")
    if isinstance(raw_urls, dict):
        urls = {str(k): str(v) for k, v in raw_urls.items() if v}
    elif isinstance(raw_urls, list):
        for it in raw_urls:
            if isinstance(it, Mapping):
                u = str(it.get("href") or it.get("url") or "")
                if u:
                    urls[str(it.get("text") or it.get("label") or "链接")] = u

    tracks = [dict(t) for t in (d.get("tracks") or [])
              if isinstance(t, Mapping)]

    warnings = []
    if not meta["release_date"]:
        warnings.append("缺发售日")
    if not meta["event"]:
        warnings.append("缺展会")
    if not credits:
        warnings.append("没有制作人员信息")

    return ImportDraft(
        key=album_key, source=source, meta=meta,
        cover_file="", cover_url=str(d.get("cover_url") or ""),
        credits=credits, urls=urls, tracks=tracks, warnings=warnings)


# ---------------------------------------------------------------------------
# 入口 ②: 网易云专辑(贴链接 -> 抓详情 + 歌词解析)
# ---------------------------------------------------------------------------

def parse_netease_ref(value: Any) -> tuple[str, int]:
    """解析网易云链接 -> ``("album"|"playlist"|"song", id)``.

    支持:
        https://music.163.com/album?id=398969728
        https://music.163.com/#/album?id=398969728
        https://music.163.com/playlist?id=123456
        直接给数字(默认当 album)
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError("空的网易云链接")
    if text.isdigit():
        return "album", int(text)
    m = re.search(r"music\.163\.com/(?:#/)?(album|playlist|song)"
                  r"(?:/|\?|$)[^0-9]*(\d+)", text)
    if m:
        return m.group(1), int(m.group(2))
    m = re.search(r"[?&]id=(\d+)", text)
    if m:
        return "album", int(m.group(1))
    raise ValueError("认不出这个网易云链接: {}".format(text[:80]))


__all__ = [
    "FORM_TEXT_FIELDS",
    "ImportDraft",
    "from_local_album",
    "from_payload",
    "parse_netease_ref",
]
