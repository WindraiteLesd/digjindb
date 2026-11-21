"""从网易云链接生成 ImportDraft(抓详情 + 歌词解析).

这是 EXEC_WIKI=BGM 那套流程的 dddb 版:
    贴链接 -> 拉专辑详情/曲目 -> 拉每首歌的歌词 -> 解析制作人员
           -> 产出 ImportDraft(可直接导入线上)
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .import_format import ImportDraft, parse_netease_ref
from .lyrics_credits import parse_credit_lines, to_album_credits
from .lyrics_fetch import lyric_text


def _nm(text: Any) -> str:
    import re

    return re.sub(r"\s+", " ", str(text or "").strip())


def _publish_date(value: Any) -> str:
    """网易云 publishTime(毫秒) -> ``YYYY-MM-DD``."""
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n > 10 ** 11:
        n //= 1000
    import datetime

    try:
        return datetime.date.fromtimestamp(n).isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def _song_artists(song: Mapping[str, Any]) -> list[str]:
    raw = song.get("ar") or song.get("artists") or []
    out: list[str] = []
    if isinstance(raw, list):
        for a in raw:
            if isinstance(a, Mapping):
                n = _nm(a.get("name"))
                if n:
                    out.append(n)
    return out


def draft_from_netease_album(
    client: Any,
    album_id: int,
    *,
    fetch_lyrics: bool = True,
    progress: Callable[[str], None] | None = None,
) -> ImportDraft:
    """网易云专辑 -> ImportDraft(含歌词解析出的制作人员)."""
    def say(msg: str) -> None:
        if progress:
            progress(msg)

    say("拉取专辑详情 {}".format(album_id))
    payload = client.album_detail(album_id) or {}
    album = payload.get("album") or {}
    songs = payload.get("songs") or []
    if not isinstance(album, Mapping):
        album = {}

    title = _nm(album.get("name"))
    artists = []
    for a in (album.get("artists") or []):
        if isinstance(a, Mapping):
            n = _nm(a.get("name"))
            if n:
                artists.append(n)
    circle = artists[0] if artists else ""

    meta = {
        "title_original": title,
        "title": "",
        "title_romaji": "",
        "circle": circle,
        "company": _nm(album.get("company")),
        "event": "",
        "release_date": _publish_date(album.get("publishTime")),
        "catalog_no": "",
        "price": "",
        "hall": "",
        "booth": "",
        "tags": "",
        "genres": "",
        "description": "",
        "artists": "、".join(artists),
    }

    tracks: list[dict[str, Any]] = []
    for i, s in enumerate(songs, 1):
        if not isinstance(s, Mapping):
            continue
        tracks.append({
            "disc": 1,
            "index": i,
            "title": _nm(s.get("name")),
            "artists": _song_artists(s),
            "duration": int(s.get("dt") or s.get("duration") or 0) // 1000,
        })
    say("  曲目 {} 首".format(len(tracks)))

    # 歌词 -> 制作人员
    credits: list[dict[str, Any]] = []
    unrecognized: list[str] = []
    if fetch_lyrics:
        seen: set[tuple[str, str]] = set()
        for i, s in enumerate(songs, 1):
            if not isinstance(s, Mapping):
                continue
            sid = int(s.get("id") or 0)
            if not sid:
                continue
            try:
                raw = client.lyric(sid)
            except Exception:                 # noqa: BLE001
                continue
            text = lyric_text(raw)
            if not text:
                continue
            parsed = parse_credit_lines(text)
            unrecognized.extend(parsed.unrecognized)
            for c in to_album_credits(parsed):
                k = (str(c.get("role")), str(c.get("name")))
                if k in seen:
                    continue
                seen.add(k)
                c = dict(c)
                c["track_index"] = i
                credits.append(c)
            if i % 20 == 0:
                say("  已解析 {}/{} 首歌词".format(i, len(songs)))
        say("  制作人员 {} 条".format(len(credits)))

    warnings: list[str] = []
    if not meta["release_date"]:
        warnings.append("缺发售日")
    if not meta["event"]:
        warnings.append("缺展会")
    if not credits:
        warnings.append("歌词里没找到制作人员信息")

    cover = _nm(album.get("picUrl"))

    return ImportDraft(
        key="netease:{}".format(album_id),
        source="网易云专辑 {}".format(album_id),
        meta=meta,
        cover_url=cover,
        credits=credits,
        urls={"网易云": "https://music.163.com/album?id={}".format(album_id)},
        tracks=tracks,
        warnings=warnings,
        unrecognized_roles=sorted(set(unrecognized))[:50],
    )


def drafts_from_netease_playlist(
    client: Any,
    playlist_id: int,
    *,
    fetch_lyrics: bool = False,
    limit: int = 0,
    progress: Callable[[str], None] | None = None,
) -> list[ImportDraft]:
    """网易云歌单 -> 每个专辑一个 ImportDraft.

    ``fetch_lyrics`` 默认**关**: 歌单可能有上百张专辑, 每张再拉全部歌词
    会非常慢。需要时在预览里单张再抓。
    """
    def say(msg: str) -> None:
        if progress:
            progress(msg)

    data = client.playlist_detail(playlist_id) or {}
    playlist = data.get("playlist") if isinstance(data, Mapping) else None
    raw = (playlist or {}).get("tracks") or []
    say("歌单曲目 {} 首".format(len(raw)))

    # 按专辑 id 聚合
    buckets: dict[int, list[Mapping[str, Any]]] = {}
    for s in raw:
        if not isinstance(s, Mapping):
            continue
        al = s.get("al") or {}
        if not isinstance(al, Mapping):
            continue
        aid = int(al.get("id") or 0)
        if not aid:
            continue
        buckets.setdefault(aid, []).append(s)
    say("聚合为 {} 张专辑".format(len(buckets)))

    ids = sorted(buckets)
    if limit > 0:
        ids = ids[:limit]

    out: list[ImportDraft] = []
    for n, aid in enumerate(ids, 1):
        say("[{}/{}] 专辑 {}".format(n, len(ids), aid))
        try:
            d = draft_from_netease_album(
                client, aid, fetch_lyrics=fetch_lyrics,
                progress=None)
        except Exception as exc:              # noqa: BLE001
            say("  失败: {}".format(exc))
            continue
        d.source = "歌单 {} / 专辑 {}".format(playlist_id, aid)
        out.append(d)
    return out


def draft_from_netease(
    client: Any,
    ref: str,
    **kwargs: Any,
) -> ImportDraft | list[ImportDraft]:
    """贴什么就导什么: 专辑 -> 单个 draft, 歌单 -> 列表."""
    kind, ident = parse_netease_ref(ref)
    if kind == "album":
        return draft_from_netease_album(client, ident, **kwargs)
    if kind == "playlist":
        return drafts_from_netease_playlist(client, ident, **kwargs)
    raise ValueError("暂不支持 {} 类型的链接".format(kind))


__all__ = [
    "draft_from_netease",
    "draft_from_netease_album",
    "drafts_from_netease_playlist",
]
