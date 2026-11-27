"""封面与附图: 数据层(附图带备注).

用户要求:
    "选择文件后弹出预览和框选范围界面再按上传，
     最后成功显示在封面槽位，而右侧加号是添加其他照片，
     其他照片不含封面，可以配备注"

设计:
    albums.payload 里新增 ``photos`` 列表(与 credits/tracks 同级):

        photos = [
            {"url": "/photo/<album>/<id>",      # 站内地址
             "path": "/srv/.../photos/x.jpg",   # 落盘路径(供服务端读)
             "note": "会场照",                  # 备注
             "added_at": 1795000000.0}
        ]

    封面仍然是 cover_local / cover_v —— **照片不参与封面**。
    裁切范围由前端在 canvas 里做, 提交上来的是已裁好的 blob,
    服务端只管存, 不需要图片处理库(零依赖原则)。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

#: 附图上限(张)
MAX_PHOTOS = 24
#: 单张附图大小上限
#: 用户 m18831: 和封面同一个上限(以前 4MB)
MAX_PHOTO_BYTES = 8 * 1024 * 1024


def _nm(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "").strip())


def photos_of(payload: Any) -> list[dict[str, Any]]:
    """从 payload 里取附图列表(容错)."""
    # 入参可能是: JSON 字符串 / 整个 payload dict / **直接的 photos 列表**
    raw: Any
    if isinstance(payload, list):
        raw = payload
    else:
        try:
            d = json.loads(payload) if isinstance(payload, str) else dict(
                payload or {})
        except (ValueError, TypeError):
            return []
        raw = d.get("photos")
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        url = _nm(it.get("url"))
        if not url:
            continue
        out.append({
            "url": url,
            "path": _nm(it.get("path")),
            "note": _nm(it.get("note"))[:200],
            "added_at": float(it.get("added_at") or 0),
        })
    return out


def add_photo(payload: Any, *, url: str, path: str,
              note: str = "") -> tuple[dict[str, Any], bool]:
    """追加一张附图 -> ``(新 payload, 是否成功)``.

    ``url`` 重复时不重复添加(返回 False)。
    """
    try:
        d = json.loads(payload) if isinstance(payload, str) else dict(
            payload or {})
    except (ValueError, TypeError):
        d = {}
    photos = photos_of(d)
    if len(photos) >= MAX_PHOTOS:
        return d, False
    if any(p["url"] == url for p in photos):
        return d, False
    photos.append({
        "url": _nm(url), "path": _nm(path),
        "note": _nm(note)[:200], "added_at": time.time(),
    })
    d["photos"] = photos
    return d, True


def remove_photo(payload: Any, url: str) -> tuple[dict[str, Any], str]:
    """删掉一张附图 -> ``(新 payload, 被删的落盘路径)``."""
    try:
        d = json.loads(payload) if isinstance(payload, str) else dict(
            payload or {})
    except (ValueError, TypeError):
        d = {}
    photos = photos_of(d)
    kept = []
    removed = ""
    for p in photos:
        if p["url"] == url and not removed:
            removed = p["path"]
            continue
        kept.append(p)
    d["photos"] = kept
    return d, removed


def set_photo_note(payload: Any, url: str,
                   note: str) -> tuple[dict[str, Any], bool]:
    """改某张附图的备注."""
    try:
        d = json.loads(payload) if isinstance(payload, str) else dict(
            payload or {})
    except (ValueError, TypeError):
        d = {}
    photos = photos_of(d)
    hit = False
    for p in photos:
        if p["url"] == url:
            p["note"] = _nm(note)[:200]
            hit = True
            break
    d["photos"] = photos
    return d, hit


def photo_dir(covers_dir: Path | str, album_key: str) -> Path:
    """某张专辑的附图目录: ``covers/photos/<hash>/``."""
    import hashlib

    base = Path(covers_dir) / "photos"
    stem = hashlib.sha1(str(album_key).encode("utf-8")).hexdigest()[:16]
    return base / stem


__all__ = [
    "MAX_PHOTOS",
    "MAX_PHOTO_BYTES",
    "add_photo",
    "photo_dir",
    "photos_of",
    "remove_photo",
    "set_photo_note",
]
