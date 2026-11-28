"""SQLite 持久层.

聚合结果、X 动态、网易云匹配与操作记录全部落库, 保证多次运行可增量继续.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import textutil
from .tagrules import is_useless_tag
from .models import (
    Album, Circle, CircleStaff, Credit, MatchResult, Track,
    normalize_activity_type, normalize_partial_date,
)

logger = logging.getLogger(__name__)
#: "人工匹配"的 method 标记 —— 与匹配器共用同一个常量(只此一处定义)。
#: 依赖方向是 db -> netease.matcher, 反方向不存在(matcher 不碰库), 所以没有环。
#: 采集端(网易云匹配)不在公开仓库里时, 用同样的字面值兜底 ——
#: 它只用于判定"这条匹配是人工确认的", 公开只读站点走不到那些写路径。
try:
    from .netease.matcher import MANUAL_METHOD
except ImportError:                      # 采集端不在本仓库
    MANUAL_METHOD = "manual"

#: 社团名里的括号补充说明, 例如 "nitro hyperspace (nitro)" / "柊さんた・雛森アンシア(Winter Bouquet)"
_PAREN_NOTE_RE = re.compile(r"[\(\[【（][^\)\]】）]{0,80}[\)\]】）]")

#: ``netease_rejections``(第一版的"整张碟拒绝"表)**新加**过的列。
#:
#: 那张表已经退役, 但**不能删**:
#: * 它是迁移快照 —— ``netease_blocks`` 里的记录就是从它复制过去的, 留着便于核对
#:   "用户的 21 条一条没丢";
#: * 里面 ``target_album_id = 0`` 的行**无法**转成"对级拉黑"(构不成一对), 只能留在原表,
#:   由 :meth:`Store.legacy_rejection_rows` 暴露出来, 绝不静默丢弃。
_LEGACY_REJECTION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("score", "REAL NOT NULL DEFAULT 0"),
    ("title_score", "REAL NOT NULL DEFAULT 0"),
    ("artist_score", "REAL NOT NULL DEFAULT 0"),
    ("track_score", "REAL NOT NULL DEFAULT 0"),
    ("method", "TEXT NOT NULL DEFAULT ''"),
    ("candidates", "TEXT NOT NULL DEFAULT ''"),
)

#: 公开端评论: 允许挂评论的目标类型
COMMENT_TARGET_KINDS: tuple[str, ...] = ("album", "circle")

#: 公开端评论: 状态(``hidden`` 是软删除 —— 行保留, 编辑者可以恢复)
COMMENT_STATUSES: tuple[str, ...] = ("visible", "hidden")

#: 评论正文长度上限(按字符数算, 不是字节)
COMMENT_BODY_LIMIT = 1000

#: 评论带上的用户名长度上限(用户名本身由用户库校验, 这里只是防御脏数据)
COMMENT_USERNAME_LIMIT = 64

#: 评论里不允许出现的控制字符(保留 \n 与 \t)
_COMMENT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_comment_body(body: Any) -> str:
    """评论正文规整: 统一换行 / 去控制字符 / 去首尾空白行.

    **不做 HTML 转义**: 库里必须存原文(否则编辑、导出、迁移都会拿到
    ``&amp;lt;`` 这种二次转义的东西); 转义在渲染层做。
    """
    text = str(body if body is not None else "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _COMMENT_CONTROL_RE.sub("", text)
    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).strip()


def comment_body_problem(body: Any, *, limit: int = COMMENT_BODY_LIMIT) -> str:
    """评论正文不合规时返回原因(空串 = 合规). 纯函数, 便于单测."""
    text = clean_comment_body(body)
    if not text:
        return "评论内容不能为空"
    if len(text) > int(limit):
        return f"评论最长 {int(limit)} 个字符"
    return ""


#: 保存时允许 Web 面板 / CLI 直接改写的字段
_EDITABLE_FIELDS = (
    "title", "title_original", "title_romaji", "circle", "circles", "artists",
    "release_date", "event", "catalog_no", "company", "genres", "tags",
    "description",
    "price", "cover_url", "booth", "hall", "track_list", "urls", "credits",
    # 编辑者可在用户端**上传封面**(落到 covers/<key>.img 并回写这个字段)
    "cover_local", "cover_v",
    # 用户 m16568: 分碟命名 / 原作 / 制作人 / 发布平台 / 出品侧别
    "discs", "original_work", "producer", "platform", "release_kind",
)

#: 社团里允许人工改写的字段(``name`` 是主键, 单独处理 —— 改名要连带改专辑里的字符串,
#: 不属于"编辑社团"的范畴, 这里直接拒绝)。
_CIRCLE_EDITABLE_FIELDS = (
    "logo_url", "logo_local", "activity_type", "founded", "last_active",
    "description", "urls", "staff",
)

#: ``list_circles`` 的排序白名单 —— 拼 SQL 的东西必须是白名单, 不能拿用户输入拼。
_CIRCLE_ORDER_SQL = {
    "name": "c.name COLLATE NOCASE",
    "updated": "c.updated_at DESC, c.name COLLATE NOCASE",
    "last_active": "c.last_active DESC, c.name COLLATE NOCASE",
    "founded": "c.founded ASC, c.name COLLATE NOCASE",
    "albums": "album_count DESC, c.name COLLATE NOCASE",
}

#: VGMdb 的 Notes 长什么样 —— 这些标记不可能出现在真正的"简介"里
_VGMDB_NOTE_MARKERS = (
    "originated from", "© sega", "Ⓒ", "tr.", "music:", "illustration:",
    "design:", "guitar:", "mastering:", "shop:", "event:", "vocals:",
)


def _looks_like_vgmdb_notes(text: str) -> bool:
    """这段文字是不是被误当简介存进去的 VGMdb Notes.

    旧数据里没有 ``raw["vgmdb_notes"]`` 可以对照, 所以用特征识别 ——
    Notes 是版权/出处/分工的堆砌, 真正的简介不会出现 ``Originated from`` 这类字样。
    """
    lowered = (text or "").lower()
    if not lowered.strip():
        return False
    hits = sum(1 for marker in _VGMDB_NOTE_MARKERS if marker in lowered)
    return hits >= 2 or "originated from" in lowered


#: **无索引价值的标签** —— 它们描述"这是什么类型的东西", 而不是流派/展会,
#: 对检索没有帮助, 只会把标签栏塞满。用户明确要求不收录。
_NOISE_TAGS = frozenset({
    # 与"是什么类型的东西"有关、对流派/展会索引没帮助的
    "cd", "cds", "disc", "doujin", "doujinmusic", "doujinindie", "doujin indie",
    "indie", "indies", "link", "links", "同人", "同人音乐", "同人音樂",
    "音乐", "音樂", "music", "other", "その他",
    # **介质 / 载体 / 版本形态** —— 同样是"是什么东西", 不是流派
    "cd r", "cdr", "cd rw", "8cm cd", "8 cm cd", "md", "dvd", "blu ray", "bluray",
    "vinyl", "cassette", "tape", "digital", "download", "dl card", "dlcard",
    "album", "single", "ep", "mini album", "maxi single", "compilation",
    "omnibus", "box", "box set", "reprint", "reissue", "live event", "demo",
})


def _norm_tag(tag: str) -> str:
    """标签的归一化形式.

    ``norm_key`` 对 "M3-2026春" 这种"字母+数字+汉字"混排会返回**空串**
    (它按词切分后认为没有有效词), 直接拿空串去比较会把展会标签误判成噪声 →
    实测差点把 M3-2026春 这类索引标签全删掉。所以这里加一层兜底。
    """
    raw = str(tag or "").lstrip("#＃").strip()
    if not raw:
        return ""
    norm = textutil.norm_key(raw, romaji=False)
    if norm:
        return norm
    return re.sub(r"[\s#＃]+", "", raw).lower()


#: **不是曲风**的词: 发行介质 / 载体 / 版本形态 / 出处分类。
#: 这些来自 VGMdb 的 classification 与 media_format, 被当曲风写进了 genres。
_NOISE_GENRES = frozenset({
    # 介质 / 载体
    "cd", "cds", "cdr", "cd r", "cd rw", "8cm cd", "8 cm cd", "md", "dvd",
    "blu ray", "bluray", "vinyl", "cassette", "tape", "digital", "download",
    "dl card", "dlcard", "dl", "usb", "sd card",
    # 版本形态
    "album", "single", "ep", "mini album", "maxi single", "compilation",
    "omnibus", "box", "box set", "limited", "first press", "reprint",
    "reissue", "live event", "event", "demo", "trial", "bonus",
    # 出处 / 分类(不是音乐风格; 同样的词已经在 tags 里)
    "original work", "original soundtrack", "original", "soundtrack",
    "ost", "o s t", "game", "anime", "movie", "doujin", "doujin indie",
    "indie", "doujinmusic",
})


def split_tag_values(values: Any) -> list[str]:
    """把标签/曲风**拆成独立词**并去重.

    实测库里出现过 ``"CD, Digital"`` / ``"Compilation, CD, Digital"`` —— 那是
    VGMdb 的 ``media_format`` 原文被当成**一个标签**存进来的, 于是噪声判据
    匹配不到(归一化成 ``"cd, digital"``)。所有写入路径都要先过这里。
    """
    out: list[str] = []
    for raw in (values or []):
        for part in re.split(r"[,，、;；/]", str(raw or "")):
            text = part.strip()
            if text and text not in out:
                out.append(text)
    return out


def is_tag_worth_keeping(tag: str) -> bool:
    """这个标签**值得留在库里/页面上**吗.

    两层判据叠在一起:
    * ``is_noise_tag``: 介质/载体/"是什么东西"层面的噪声
    * ``is_useless_tag``: **没有检索意义**的词(展会名/会次/纯年份/虚拟歌姬名/
      平台格式/杂词) —— 展会与年份已经有结构化字段, 再当标签存一遍纯属重复。

    实测: 本库 391 种标签里, 后者占 30 种 / 2747 次(约 53% 的出现次数)。
    """
    return not is_noise_tag(tag) and not is_useless_tag(tag)


def is_noise_genre(value: str) -> bool:
    """这个词能不能当**曲风**.

    与标签噪声不同: 曲风还额外排除介质/载体/版本/出处分类
    (``Digital`` / ``DL Card`` / ``Original Work`` / ``EP`` … 都不是音乐风格)。
    """
    if is_noise_tag(value):
        return True
    norm = _norm_tag(value)
    return bool(norm) and norm in _NOISE_GENRES


def is_noise_tag(tag: str) -> bool:
    """这个标签是不是"CD / Doujin / Indie / Link / 同人音乐" 这类噪声."""
    if not str(tag or "").strip():
        return True                 # 空标签本来就该丢
    norm = _norm_tag(tag)
    if not norm:
        return False                # 归一化失败 -> **保留**(宁可多留, 不可误删索引)
    return norm in _NOISE_TAGS


def _circle_core(circle: str) -> str:
    """社团名去掉括号补充说明后的规范化形式.

    ``"nitro hyperspace (nitro)"`` 与 ``"nitro hyperspace"`` 是同一个社团,
    但键盘上多打一对括号就会让"社团完全相等"的判据失效 —— 这是重复入库的主因之一。
    """
    return textutil.norm_key(_PAREN_NOTE_RE.sub(" ", circle or ""), romaji=False)


def _tokens(text: str) -> set[str]:
    return {tok for tok in (text or "").split() if len(tok) >= 2}


def _circles_related(a: Album, b: Album, *, ratio: float = 0.9) -> bool:
    """两个社团名是否指向同一个社团.

    判据由严到宽:

    1. 去掉括号补充说明后完全相同 —— ``nitro hyperspace (nitro)`` vs ``nitro hyperspace``;
    2. 一方的词集合是另一方的子集(至少 2 个词) ——
       ``Winter Bouquet`` vs ``柊さんた・雛森アンシア(Winter Bouquet)``;
    3. 规范化后相似度 >= ``ratio``.
    """
    core_a, core_b = _circle_core(a.circle), _circle_core(b.circle)
    norm_a = textutil.norm_key(a.circle, romaji=False)
    norm_b = textutil.norm_key(b.circle, romaji=False)
    if core_a and core_a == core_b:
        return True
    tok_a, tok_b = _tokens(core_a), _tokens(core_b)
    if len(tok_a) >= 2 and tok_a <= tok_b:
        return True
    if len(tok_b) >= 2 and tok_b <= tok_a:
        return True
    # 括号里才是社团名, 括号外写的是艺人(或反过来): 用整串的词集再做一次子集判断
    if tok_a and tok_b and (tok_a <= _tokens(norm_b) or tok_b <= _tokens(norm_a)):
        return True
    if norm_a and norm_b:
        return textutil.similarity(a.circle, b.circle) >= ratio
    return False


def _title_similarity(a: Album, b: Album) -> float:
    """标题相似度: 在两边所有身份标题(原名/译名/罗马字)里取最高分."""
    best = 0.0
    for ta in a.identity_titles():
        for tb in b.identity_titles():
            best = max(best, textutil.similarity(ta, tb))
            if best >= 1.0:
                return best
    return best


def _same_occasion(a: Album, b: Album) -> bool:
    """展会/年份是否对得上 —— 防止"同名不同展会"的两张碟被并成一张."""
    if a.event and b.event:
        return a.event == b.event
    year_a = (a.release_date or "")[:4]
    year_b = (b.release_date or "")[:4]
    if year_a and year_b:
        return year_a == year_b
    return True


def _title_numbers(album: Album) -> tuple[int, ...]:
    """标题里的数字序列(卷号/期号).

    ``TTL EUROBEAT VOL.25`` 与 ``VOL.19``、``Melodious Melodies 4`` 与 ``5``
    的规范化字符串相似度都超过 0.94 —— 但它们显然是**不同的碟**。数字不一致时
    一律不算重复, 这条判据比调高阈值更准(也不用牺牲"大小写/标点不同"的召回)。
    """
    text = textutil.norm_key(album.title_original or album.title, romaji=False)
    return tuple(int(chunk) for chunk in re.findall(r"\d+", text))


def _is_duplicate_pair(a: Album, b: Album, *, threshold: float = 0.94, strict: bool = False) -> bool:
    """两条记录是否指向**同一张**专辑(去重判定的唯一入口).

    造这条判据的动机: 同一张碟在不同站点上的写法并不一致 —— 社团名多/少一段
    括号补充(``nitro hyperspace (nitro)`` vs ``nitro hyperspace``,
    ``Winter Bouquet`` vs ``柊さんた・雛森アンシア(Winter Bouquet)``),
    标题的大小写/全半角差异(``BLOOM`` vs ``Bloom``)。旧实现只比
    「标题规范化后完全相等 + 社团完全相等」, 于是同一张碟每采集一轮就多一条。
    """
    if not a.key or not b.key:
        return False
    # **编号相同就是同一张碟**。编号(catalog_no)是商品唯一标识, 比标题可靠得多 ——
    # 用户手改了标题之后, 标题相似度会崩, 但编号还在, 靠它把重复认回来。
    catalog_a = textutil.norm_key(a.catalog_no, romaji=False) if a.catalog_no else ""
    catalog_b = textutil.norm_key(b.catalog_no, romaji=False) if b.catalog_no else ""
    if catalog_a and catalog_b and catalog_a == catalog_b:
        return True
    if _title_numbers(a) != _title_numbers(b):
        return False
    score = _title_similarity(a, b)
    if strict:
        if score < max(threshold, 0.99):
            return False
        # 严格模式连"括号补充"也不放宽: 社团规范化后必须逐字相等
        norm_a = textutil.norm_key(a.circle, romaji=False)
        norm_b = textutil.norm_key(b.circle, romaji=False)
        return bool(norm_a) and norm_a == norm_b
    if score < threshold:
        return False
    if a.circle or b.circle:
        if not _circles_related(a, b):
            return False
    core_a = _circle_core(a.circle)
    strong = bool(core_a) and core_a == _circle_core(b.circle)
    if not strong and not _same_occasion(a, b):
        return False
    return True


#: 站点通用图的文件名特征(命中这些才判定"可疑封面")
_GENERIC_COVER_HINTS = (
    # 注意: **不收 og-image / ogp** —— 对"单碟专用站"(xxx.netlify.app 这种)
    # 来说 og:image 就是这张碟的封面, 收进来全是误判(实测 26 张里绝大多数是这种)。
    "logo", "favicon", "thumbnail", "eyecatch", "noimage", "no_image",
    "banner", "header", "hero", "/share", "sprite",
    # 实测踩到的: 社团站上的这些**不是这张碟的封面**
    "share image", "share_image", "artist_photo", "artist-photo",
    "profile", "member", "gallery", "eyecatch",
)

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS albums (
    key          TEXT PRIMARY KEY,
    title        TEXT NOT NULL DEFAULT '',
    title_original TEXT NOT NULL DEFAULT '',
    circle       TEXT NOT NULL DEFAULT '',
    event        TEXT NOT NULL DEFAULT '',
    event_kind   TEXT NOT NULL DEFAULT '',
    release_date TEXT NOT NULL DEFAULT '',
    year         TEXT NOT NULL DEFAULT '',
    cover_url    TEXT NOT NULL DEFAULT '',
    cover_local  TEXT NOT NULL DEFAULT '',
    track_count  INTEGER NOT NULL DEFAULT 0,
    norm_title   TEXT NOT NULL DEFAULT '',
    norm_circle  TEXT NOT NULL DEFAULT '',
    payload      TEXT NOT NULL,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_albums_event ON albums(event);
CREATE INDEX IF NOT EXISTS idx_albums_kind ON albums(event_kind);
CREATE INDEX IF NOT EXISTS idx_albums_normtitle ON albums(norm_title);
CREATE INDEX IF NOT EXISTS idx_albums_circle ON albums(norm_circle);

CREATE TABLE IF NOT EXISTS album_sources (
    source    TEXT NOT NULL,
    source_id TEXT NOT NULL,
    album_key TEXT NOT NULL,
    url       TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_album_sources_key ON album_sources(album_key);

CREATE TABLE IF NOT EXISTS album_tags (
    album_key TEXT NOT NULL,
    tag       TEXT NOT NULL,
    PRIMARY KEY (album_key, tag)
);

-- 用户自定义标记: 例如把"已加入歌单""已购买""待确认"标在专辑上, 之后可以按标记筛选/排除
CREATE TABLE IF NOT EXISTS album_marks (
    album_key  TEXT NOT NULL,
    mark       TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY (album_key, mark)
);
CREATE INDEX IF NOT EXISTS idx_album_marks_mark ON album_marks(mark);

-- 歌单独有专辑的标记("这张我已经补进线上库了").
--
-- 为什么不能复用 album_marks: 那里的主键是 **本地专辑 key**, 而「② 歌单里有、本地库没有」
-- 的条目本地库根本还没有 key —— 只有网易云的 album_id。硬写 album_marks 会被
-- ``_albums_mark`` 的 ``get_album(key) is None`` 挡掉(那也是对的: 不许给不存在的碟打标记)。
-- 所以按 album_id 另存一份; 名字一起留着, 方便以后清孤儿记录和显示。
CREATE TABLE IF NOT EXISTS playlist_album_marks (
    album_id   INTEGER NOT NULL,
    mark       TEXT NOT NULL,
    album_name TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY (album_id, mark)
);
CREATE INDEX IF NOT EXISTS idx_playlist_album_marks_mark ON playlist_album_marks(mark);

CREATE TABLE IF NOT EXISTS netease_matches (
    album_key     TEXT PRIMARY KEY,
    kind          TEXT NOT NULL DEFAULT 'song',
    target_id     INTEGER NOT NULL DEFAULT 0,
    target_name   TEXT NOT NULL DEFAULT '',
    target_artists TEXT NOT NULL DEFAULT '[]',
    target_album_id INTEGER NOT NULL DEFAULT 0,
    target_album_name TEXT NOT NULL DEFAULT '',
    score         REAL NOT NULL DEFAULT 0,
    title_score   REAL NOT NULL DEFAULT 0,
    artist_score  REAL NOT NULL DEFAULT 0,
    track_score   REAL NOT NULL DEFAULT 0,
    method        TEXT NOT NULL DEFAULT '',
    payload       TEXT NOT NULL DEFAULT '{}',
    matched_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_matches_score ON netease_matches(score);

-- 「网易云查过但没有」的**带时效**负缓存。
-- 为什么必须有 TTL: 网易云的艺人/社团条目是**用户随时创建**的, 今天查不到不代表
-- 下个月还没有。永久缓存会让我们永久错过后来才出现的条目, 所以只记一段时间。
CREATE TABLE IF NOT EXISTS netease_misses (
    kind       TEXT NOT NULL,        -- artist / album ...
    key        TEXT NOT NULL,        -- 规范化后的检索词
    checked_at REAL NOT NULL,
    PRIMARY KEY (kind, key)
);

CREATE INDEX IF NOT EXISTS idx_misses_checked ON netease_misses(checked_at);

-- **人工「拉黑」的持久记录** —— 「这张本地碟 ← 不要再匹配那一条网易云条目」.
--
-- 这是人的判断, 不是机器缓存, 所以**没有 TTL**。
--
-- **粒度是"对"而不是"整张碟"**(用户后来纠正的意图): 用户点"取消匹配"时拒的往往是某一条
-- **同名错条**, 而正确条目可能就在下一个候选里 —— 所以本地专辑**仍然参与匹配**, 只是
-- 那一条 album_id 被排除在外。主键因此是 ``(album_key, target_album_id)``,
-- 且 ``target_album_id`` **不许为 0**(没有 album_id 就构不成一条"对")。
CREATE TABLE IF NOT EXISTS netease_blocks (
    album_key       TEXT NOT NULL,
    target_album_id INTEGER NOT NULL,
    target_name     TEXT NOT NULL DEFAULT '',
    reason          TEXT NOT NULL DEFAULT '',
    blocked_at      REAL NOT NULL,
    -- 拉黑时那条匹配的**完整分量** —— 用户每拉黑一次就是一条带标签的负样本
    score           REAL NOT NULL DEFAULT 0,
    title_score     REAL NOT NULL DEFAULT 0,
    artist_score    REAL NOT NULL DEFAULT 0,
    track_score     REAL NOT NULL DEFAULT 0,
    method          TEXT NOT NULL DEFAULT '',
    candidates      TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (album_key, target_album_id)
);
CREATE INDEX IF NOT EXISTS idx_blocks_album ON netease_blocks(album_key);
CREATE INDEX IF NOT EXISTS idx_blocks_at ON netease_blocks(blocked_at);

CREATE TABLE IF NOT EXISTS crawl_state (
    source     TEXT NOT NULL,
    scope      TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    PRIMARY KEY (source, scope, key)
);

CREATE TABLE IF NOT EXISTS events (
    name       TEXT PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT '',
    year       INTEGER NOT NULL DEFAULT 0,
    season     TEXT NOT NULL DEFAULT '',
    start_date TEXT NOT NULL DEFAULT '',
    end_date   TEXT NOT NULL DEFAULT '',
    tags       TEXT NOT NULL DEFAULT '[]',
    -- 展会词条也允许图片与相关链接(与 circles 的 logo_url/urls_json 同构)
    logo_url   TEXT NOT NULL DEFAULT '',
    logo_local TEXT NOT NULL DEFAULT '',
    links_json TEXT NOT NULL DEFAULT '{}',
    -- 两级结构: 这条会次属于哪个系列; vol 是会次号(如 M3-58 的 58)
    --
    -- 外键是**必须的**: 早先靠手工 UPDATE 维护 events.series, 系列改名时
    -- 漏了一条, 那个会次(1096 张专辑)就从索引里消失了。
    -- ON UPDATE CASCADE 让"改系列名"自动带着会次走;
    -- ON DELETE SET NULL 让"删系列"不连坐删掉会次。
    -- NULL = 未归类(不能用空串: 空串会变成一个非法引用)。
    series     TEXT DEFAULT NULL
               REFERENCES event_series(name)
               ON UPDATE CASCADE ON DELETE SET NULL,
    vol        INTEGER NOT NULL DEFAULT 0,
    vol_label  TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL
);

-- 展会**系列**(一等公民): 一个系列含多个会次。
-- 用户要求展示成 "[M3] M3-2026秋 (M3-58)": 缩写与全称属于系列, 会次属于 events。
CREATE TABLE IF NOT EXISTS event_series (
    name        TEXT PRIMARY KEY,      -- 系列主名, 如 "M3" / "Comic Market"
    abbr        TEXT NOT NULL DEFAULT '',   -- 缩写, 如 "M3" / "CM"
    full_name   TEXT NOT NULL DEFAULT '',   -- 全称, 如 "Comic Market"
    logo_url    TEXT NOT NULL DEFAULT '',
    logo_local  TEXT NOT NULL DEFAULT '',
    poster_url  TEXT NOT NULL DEFAULT '',
    poster_local TEXT NOT NULL DEFAULT '',
    wiki        TEXT NOT NULL DEFAULT '',   -- MD 语法的介绍页
    links_json  TEXT NOT NULL DEFAULT '{}',
    sort_order  INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_series_updated ON event_series(updated_at);

-- 社团(一等公民数据)。
-- ``albums.circle`` **仍然是字符串**, 这张表按 ``name`` 与它对应 —— 这里存的是
-- 社团级的补充信息(logo / 主催 / 成立时间 / 链接), 不是专辑的作者信息。
-- 所以删一张专辑不影响社团, 删社团也不会动专辑。
CREATE TABLE IF NOT EXISTS circles (
    name          TEXT PRIMARY KEY,
    logo_url      TEXT NOT NULL DEFAULT '',
    logo_local    TEXT NOT NULL DEFAULT '',
    activity_type TEXT NOT NULL DEFAULT '',
    founded       TEXT NOT NULL DEFAULT '',
    last_active   TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    urls_json     TEXT NOT NULL DEFAULT '{}',
    payload       TEXT NOT NULL DEFAULT '{}',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_circles_updated ON circles(updated_at);
CREATE INDEX IF NOT EXISTS idx_circles_last_active ON circles(last_active);

-- 人员组成: role = owner(主催) / member(成员)。
-- position 保留界面上的排列顺序(主催排在成员前面是编辑时定下的, 不该被读库打乱)。
CREATE TABLE IF NOT EXISTS circle_staff (
    circle   TEXT NOT NULL,
    role     TEXT NOT NULL,
    name     TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (circle, role, name)
);
CREATE INDEX IF NOT EXISTS idx_circle_staff_circle ON circle_staff(circle);
CREATE INDEX IF NOT EXISTS idx_circle_staff_name ON circle_staff(name);

-- ==========================================================================
-- 公开端(用户产生内容): 评论
-- ==========================================================================
-- 用户端是**云上唯一真相**, 所以评论落在专辑主库里(而不是像账号那样独立成文件):
-- 评论要跟专辑/社团同生共死, 也需要同一个备份/迁移通道。
--
-- * ``target_kind``: album | circle; ``target_key`` 对应 albums.key 或社团名
--   (**不加外键**: 专辑可能被重新合并/改名, 评论不该因此消失或挡住删除);
-- * ``status``: visible | hidden —— 编辑者隐藏是**软删除**(行还在, 可恢复,
--   也留作滥用审计), 作者删自己的才是物理删除;
-- * ``ip_hash``: 只是 sha256(salt|ip) 的截断, 用来识别"同一个来源刷屏",
--   不可逆, 也**不存明文 IP**;
-- * ``user_id/username``: 冗余存一份用户名 —— 即使用户后来被删除, 历史评论
--   仍然有署名(和 ``album_marks`` 的 note 式冗余同思路)。
CREATE TABLE IF NOT EXISTS album_revisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    album_key   TEXT    NOT NULL,
    user_id     INTEGER NOT NULL DEFAULT 0,
    username    TEXT    NOT NULL DEFAULT '',
    summary     TEXT    NOT NULL DEFAULT '',
    payload     TEXT    NOT NULL DEFAULT '',
    created_at  REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_revisions_album ON album_revisions(album_key, created_at);
CREATE TABLE IF NOT EXISTS entities (
  kind TEXT    NOT NULL,
  id   INTEGER NOT NULL,
  name TEXT    NOT NULL COLLATE NOCASE,
  PRIMARY KEY (kind, id),
  UNIQUE (kind, name)
);
CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(kind, name COLLATE NOCASE);

-- 专辑 <-> 各实体的关联(按 id)
CREATE TABLE IF NOT EXISTS album_circles (
  album_key TEXT NOT NULL, circle_id INTEGER NOT NULL,
  PRIMARY KEY (album_key, circle_id));
CREATE TABLE IF NOT EXISTS album_artists (
  album_key TEXT NOT NULL, artist_id INTEGER NOT NULL,
  PRIMARY KEY (album_key, artist_id));
CREATE TABLE IF NOT EXISTS album_events (
  album_key TEXT NOT NULL, event_id INTEGER NOT NULL,
  PRIMARY KEY (album_key, event_id));
CREATE TABLE IF NOT EXISTS album_tag_ids (
  album_key TEXT NOT NULL, tag_id INTEGER NOT NULL,
  PRIMARY KEY (album_key, tag_id));
CREATE INDEX IF NOT EXISTS idx_album_circles_c ON album_circles(circle_id);
CREATE INDEX IF NOT EXISTS idx_album_artists_a ON album_artists(artist_id);
CREATE INDEX IF NOT EXISTS idx_album_events_e ON album_events(event_id);
CREATE INDEX IF NOT EXISTS idx_album_tag_ids_t ON album_tag_ids(tag_id);

CREATE TABLE IF NOT EXISTS tags (
  name       TEXT PRIMARY KEY COLLATE NOCASE,
  kind       TEXT NOT NULL DEFAULT '',
  note       TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tags_updated ON tags(updated_at);

CREATE TABLE IF NOT EXISTS artists (
  name       TEXT PRIMARY KEY COLLATE NOCASE,
  payload    TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_artists_updated ON artists(updated_at);

CREATE TABLE IF NOT EXISTS comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_kind TEXT    NOT NULL DEFAULT 'album',
    target_key  TEXT    NOT NULL DEFAULT '',
    user_id     INTEGER NOT NULL DEFAULT 0,
    username    TEXT    NOT NULL DEFAULT '',
    body        TEXT    NOT NULL DEFAULT '',
    created_at  REAL    NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL DEFAULT 'visible',
    hidden_by   TEXT    NOT NULL DEFAULT '',
    hidden_at   REAL    NOT NULL DEFAULT 0,
    ip_hash     TEXT    NOT NULL DEFAULT ''
);
-- 主查询: 某张专辑/某个社团的可见评论, 按时间倒序
CREATE TABLE IF NOT EXISTS comment_likes (
    comment_id INTEGER NOT NULL,
    username   TEXT    NOT NULL COLLATE NOCASE DEFAULT '',
    created_at REAL    NOT NULL DEFAULT 0,
    PRIMARY KEY (comment_id, username)
);
CREATE INDEX IF NOT EXISTS idx_comment_likes_user ON comment_likes(username);
CREATE INDEX IF NOT EXISTS idx_comments_target
    ON comments(target_kind, target_key, status, created_at);
-- 作者的评论(「作者可删自己的」/ 限速查重)
CREATE INDEX IF NOT EXISTS idx_comments_author ON comments(user_id, created_at);
-- 按来源识别刷屏
CREATE INDEX IF NOT EXISTS idx_comments_ip ON comments(ip_hash, created_at);
-- 工作队列: 被隐藏的评论
CREATE INDEX IF NOT EXISTS idx_comments_status ON comments(status, created_at);
"""


def _kind_of(event: str) -> str:
    """展会名 -> 类型(如 comiket / m3). 薄封装, 供多处复用."""
    from .models import event_kind

    return event_kind(event)


#: 描述里常见的来源库说明(采集器写进去的, 属于来源痕迹, 不该出现在云端)
#: 描述里常见的来源库说明(采集器写进去的, 属于来源痕迹, 不该出现在云端).
#:
#: 只匹配**整行就是一句来源说明**的情况, 例如 ``DoujinStyle 提供的格式: FLAC``;
#: 不能写成宽泛的 ``^doujinstyle.*`` —— 那会把"DoujinStyle 提供的格式: FLAC\n正文"
#: 整段连正文一起判掉。
_SOURCE_NOTE_PATTERNS = (
    r"^\s*doujin\s*style\s*提供的格式\s*[:：]\s*\S+\s*$",
    r"^\s*来源\s*[:：]\s*(vgmdb|bangumi|bgm|doujin\s*style)\s*$",
    r"^\s*(vgmdb|bangumi|bgm\.tv)\s*提供的格式\s*[:：]\s*\S+\s*$",
    r"^\s*下载自\s*(vgmdb|bangumi|bgm|doujin\s*style)\s*$",
)


#: 只应保存在本地站长端的"来源数据库"域名(云端库要全部清除)
SOURCE_HOSTS: tuple[str, ...] = (
    "vgmdb.net",
    "vgmdb.info",
    "bgm.tv",
    "bangumi.tv",
    "bangumi.biz",
    "chii.in",
    "lain.bgm.tv",
    # 第三个采集源: doujinstyle —— 之前漏了, 导致 105 张专辑还带着它的链接
    "doujinstyle.com",
    "doujinstyle",
    "doujin-styler",
)

#: 云端**不应该保留**的来源数据库标识(外链 + source_ids 键)
SOURCE_ID_KEYS: tuple[str, ...] = (
    "vgmdb", "bgm", "bgmtv", "bangumi", "bangumi_tv", "doujinstyle",
    "dojinmusic", "doujinmusic",
)

#: 只存在于 payload 里的"来源"痕迹(逐条记录/原始抓取/可信度)
SOURCE_META_KEYS: tuple[str, ...] = (
    "trust_by_source", "raw", "raw_payload", "sources", "source", "fetched_from",
)


#: 只应保存在本地站长端的"来源数据库"域名(云端库要全部清除)
SOURCE_HOSTS: tuple[str, ...] = (
    "vgmdb.net",
    "vgmdb.info",
    "bgm.tv",
    "bangumi.tv",
    "bangumi.biz",
    "chii.in",
    "lain.bgm.tv",
    # 第三个采集源: doujinstyle —— 之前漏了, 导致 105 张专辑还带着它的链接
    "doujinstyle.com",
    "doujinstyle",
    "doujin-styler",
)


#: 云端**不应该保留**的来源数据库标识(外链 + source_ids 键)
SOURCE_ID_KEYS: tuple[str, ...] = (
    "vgmdb", "bgm", "bgmtv", "bangumi", "bangumi_tv", "doujinstyle",
    "dojinmusic", "doujinmusic",
)


#: 只存在于 payload 里的"来源"痕迹(逐条记录/原始抓取/可信度)
SOURCE_META_KEYS: tuple[str, ...] = (
    "trust_by_source", "raw", "raw_payload", "sources", "source", "fetched_from",
)


def _url_host(url: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return ""
    return (parsed.hostname or "").lower()


def url_from_source_host(url: str, hosts: tuple[str, ...] = SOURCE_HOSTS) -> bool:
    host = _url_host(url)
    return bool(host) and any(host == h or host.endswith("." + h) for h in hosts)


def _parse_ts(value: str) -> float:
    if not value:
        return time.time()
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return time.time()


def _scrub_source_meta(value: Any, hosts: tuple[str, ...],
                       id_keys: set[str]) -> tuple[Any, int]:
    """递归剔除来源痕迹, 返回 ``(清理后的值, 剔除条数)``."""
    removed = 0
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            lower = str(key).strip().lower()
            if lower in ("source", "sources") :
                removed += 1
                continue
            if lower in ("trust_by_source", "raw", "raw_payload", "fetched_from"):
                removed += 1
                continue
            if lower == "source_ids" and isinstance(item, dict):
                keep = {k: v for k, v in item.items() if str(k).lower() not in id_keys}
                removed += len(item) - len(keep)
                if keep:
                    cleaned[key] = keep
                continue
            if lower == "urls" and isinstance(item, dict):
                keep = {k: v for k, v in item.items()
                        if v and not url_from_source_host(str(v), hosts)}
                removed += len(item) - len(keep)
                if keep:
                    cleaned[key] = keep
                continue
            if lower == "cover_url" and isinstance(item, str) and url_from_source_host(item, hosts):
                removed += 1
                continue
            sub, sub_removed = _scrub_source_meta(item, hosts, id_keys)
            removed += sub_removed
            cleaned[key] = sub
        return cleaned, removed
    if isinstance(value, list):
        out = []
        for item in value:
            sub, sub_removed = _scrub_source_meta(item, hosts, id_keys)
            removed += sub_removed
            out.append(sub)
        return out, removed
    return value, 0


def _scrub_album_sources(album: Any) -> None:
    """剔除专辑里**对外展示**的来源站痕迹(写库时自动执行).

    重要: 本项目里 ``Album.urls`` 的语义是 **来源标识映射**
    (``{"doujinstyle": "https://doujinstyle.com/?p=album&id=1042"}``),
    跨源合并与信任判定都依赖它, 所以**绝不能在这里删** —— 展示层负责不渲染。
    同理 ``source_ids`` / ``trust_by_source`` 也保持原样。

    这里只清两类纯展示字段:

    * ``cover_url`` 指向来源站 CDN 的地址(云端会用本地缓存的封面)
    * ``description`` 里的"DoujinStyle 提供的格式: FLAC"之类来源说明
    """
    hosts = tuple(h.lower() for h in SOURCE_HOSTS)

    # 1) 指向来源站 CDN 的封面
    cover = str(getattr(album, "cover_url", "") or "")
    if cover and url_from_source_host(cover, hosts):
        album.cover_url = ""

    # 2) 自由文本里的来源说明
    description = str(getattr(album, "description", "") or "")
    if description:
        album.description = scrub_source_notes(description)


def scrub_source_notes(text: str) -> str:
    """剔除自由文本里的"来源库说明"片段(如 ``DoujinStyle 提供的格式: FLAC``).

    全文可能已经被 ``clean_whitespace`` 压成一行(换行变空格), 所以不能只看行:
    先在整段文本里按已知句式做**定点删除**, 再收拾多余空白。
    这样 "DoujinStyle 提供的格式: FLAC 真正的简介" 也能只留下正文。
    """
    import re as _re

    if not text or not str(text).strip():
        return text
    original = str(text)
    result = original
    # 定点删除: 句式 + 紧随的一个"格式词"(FLAC/WAV/MP3/...)
    inline_patterns = (
        _re.compile(r"doujin\s*style\s*提供的格式\s*[:：]?\s*"
                    r"(flac|wav|mp3|alac|aiff|ogg|m4a|ape|tta|dsd)?",
                    _re.I),
        _re.compile(r"下载自\s*(vgmdb|bangumi|bgm\.tv|doujin\s*style|doujinstyle)",
                    _re.I),
        _re.compile(r"来源\s*[:：]\s*(vgmdb|bangumi|bgm\.tv|doujin\s*style|doujinstyle)",
                    _re.I),
    )
    for pattern in inline_patterns:
        result = pattern.sub(" ", result)
    if result == original:
        return text
    # 收拾: 多空格合一, 去首尾
    result = _re.sub(r"\s{2,}", " ", result).strip()
    return result


def strip_source_links(store: "Store", hosts: tuple[str, ...] = SOURCE_HOSTS,
                       *, apply: bool = False,
                       id_keys: tuple[str, ...] = SOURCE_ID_KEYS) -> tuple[int, int]:
    """彻底剔除来源数据库痕迹(云端不保留, 本地站长端自行保留).

    清理内容: ``urls`` 里的来源站链接、``source_ids`` 的来源库编号、
    ``credits`` / ``tracks`` 里的 ``source`` 标记、``trust_by_source``、``raw``、
    指向来源 CDN 的 ``cover_url``。返回 ``(受影响专辑数, 剔除条数)``。
    """
    affected = removed = 0
    lowered = {k.lower() for k in id_keys}
    for row in store.query("SELECT key, payload FROM albums"):
        try:
            album = json.loads(row["payload"] or "{}")
        except ValueError:
            continue
        cleaned, count = _scrub_source_meta(
            album, hosts, lowered)
        if not count:
            continue
        affected += 1
        removed += count
        if apply:
            store.execute(
                "UPDATE albums SET payload = ?, updated_at = ? WHERE key = ?",
                (json.dumps(cleaned, ensure_ascii=False, sort_keys=True),
                 time.time(), str(row["key"])))
    return affected, removed


#: 「重复专辑合并」能选的部分(用户 m13899: 不再是全量合并)。
#: 键同时是表单里 checkbox 的值, 顺序就是界面上的顺序。
MERGE_PARTS: tuple[tuple[str, str], ...] = (
    ("tracks", "曲目表"),
    ("credits", "制作人员"),
    ("urls", "外链"),
    ("photos", "其他照片"),
    ("meta", "基本信息（发售日 / 展会 / 社团 / 编号 / 价格 / 会场）"),
    ("description", "简介"),
    ("cover", "封面"),
    ("tags", "艺人 / 风格 / 标签"),
)


class Store:
    """聚合数据仓库."""

    def __init__(self, path: Path | str, *, dedupe_mode: str = "balanced") -> None:
        self.path = Path(path)
        #: 跨源合并的严格程度, 见 :meth:`find_similar_album`
        self.dedupe_mode = (dedupe_mode or "balanced").lower()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
            self._migrate_events_media()
            self._migrate_events_series_fk()
            self._migrate_rejections()

    def _migrate_events_media(self) -> None:
        """给老库的 ``events`` 表补上 logo/links 列.

        ``CREATE TABLE IF NOT EXISTS`` 对**已存在**的表不会加列, 所以老库
        直接升级后写 ``logo_url`` 会报 "no such column"。这里按 PRAGMA 检查后
        逐列 ALTER; 已存在的列跳过(幂等)。
        """
        try:
            tables = {str(r["name"]) for r in self.query(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
        except Exception:                     # noqa: BLE001
            return
        if "events" not in tables:
            return
        try:
            have = {str(r["name"]) for r in self.query("PRAGMA table_info(events)")}
        except Exception:                     # noqa: BLE001
            return
        for name, decl in (("logo_url", "TEXT NOT NULL DEFAULT ''"),
                           ("logo_local", "TEXT NOT NULL DEFAULT ''"),
                           ("links_json", "TEXT NOT NULL DEFAULT '{}'"),
                           # series 用 NULL 表示未归类(外键下空串非法)
                           ("series", "TEXT DEFAULT NULL"),
                           ("vol", "INTEGER NOT NULL DEFAULT 0"),
                           ("vol_label", "TEXT NOT NULL DEFAULT ''")):
            if name in have:
                continue
            try:
                self.execute(f"ALTER TABLE events ADD COLUMN {name} {decl}")
            except Exception:                 # noqa: BLE001
                pass

    def _migrate_rejections(self) -> None:
        """把第一版的「整张碟拒绝」表(``netease_rejections``)迁移成**对级拉黑**.

        迁移规则(用户那 21 条人工决定一条都不许丢):

        * ``target_album_id > 0`` 的行 -> 直接复制进 ``netease_blocks``(语义等价: 他当时
          拒的就是那一条候选), 用 ``INSERT OR IGNORE`` 保证重复打开不会写重;
        * ``target_album_id = 0`` 的行 -> **构不成一对**, 没法转换; 留在原表不动, 由
          :meth:`legacy_rejection_rows` 暴露给上层报告, **绝不静默丢弃**;
        * 老表本身**保留**作为迁移快照(当库里没有这张表时什么都不做)。

        另外顺手把老表缺的列补上(第一版没有分量字段), 这样快照行也能读全。
        """
        try:
            tables = {str(r["name"]) for r in self.query(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
        except Exception:
            return
        if "netease_rejections" not in tables:
            return
        # ① 老表缺列就补上(只加列; 列名/类型与 netease_blocks 对齐)
        try:
            have = {str(r["name"]) for r in self.query("PRAGMA table_info(netease_rejections)")}
            for name, decl in _LEGACY_REJECTION_COLUMNS:
                if name not in have:
                    try:
                        self.execute(f"ALTER TABLE netease_rejections ADD COLUMN {name} {decl}")
                    except Exception:
                        pass
        except Exception:
            pass
        # ② **只迁移一次** —— 迁移完在 crawl_state 里记一个标记。
        #
        # 为什么必须有这个标记: 老表要留着当快照, 而 ``INSERT OR IGNORE`` 每次打开库都会
        # 把老表里还在的行再灌一遍。于是用户后来**解禁**掉的那条会自己"复活" ——
        # 实测踩到: 手动匹配某条被拉黑的候选之后(按互斥规则解禁), 下一次打开库它又回来了。
        try:
            if self.get_state("migration", "netease_blocks", "legacy_v1") == "done":
                return
        except Exception:
            pass
        try:
            self.execute(
                "INSERT OR IGNORE INTO netease_blocks("
                " album_key, target_album_id, target_name, reason, blocked_at,"
                " score, title_score, artist_score, track_score, method, candidates)"
                " SELECT album_key, target_album_id, target_name, reason, rejected_at,"
                "        score, title_score, artist_score, track_score, method, candidates"
                " FROM netease_rejections WHERE COALESCE(target_album_id, 0) > 0"
            )
        except Exception:
            # 迁移失败也不能让库打不开; 原表还在, 下次打开会再试一次(标记没写就不算迁完)
            return
        try:
            self.set_state("migration", "netease_blocks", "legacy_v1", "done")
        except Exception:
            pass

    # -- 低层 ---------------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- 专辑 ---------------------------------------------------------------

    def upsert_album(self, album: Album, *, event: str | None = None) -> str:
        """写入 / 合并专辑, 返回 ``'created' | 'updated' | 'unchanged'``.

        归并优先级: 站点原生 ID 命中 > 聚合 key 命中 > 规范化标题+社团相似命中。
        """
        if event:
            album.event = event
            album.event_kind = album.event_kind or _kind_of(event)
        if not album.key:
            album.key = album.compute_key()

        # 1) 站点原生 ID 命中(最可靠)
        existing: Album | None = None
        for source, sid in album.source_ids.items():
            if not sid:
                continue
            row = self.query_one(
                "SELECT album_key FROM album_sources WHERE source = ? AND source_id = ?", (source, str(sid))
            )
            if row is not None:
                existing = self.get_album(row["album_key"])
                if existing is not None:
                    break

        # 2) 聚合 key 命中
        if existing is None:
            existing = self.get_album(album.key)

        # 3) 规范化标题 + 社团相似命中
        if existing is None:
            existing = self.find_similar_album(album)

        if existing is not None:
            album.key = existing.key

        now = time.time()
        if existing is None:
            merged = album
            status = "created"
        else:
            # 注意: Album.merge() 返回的是 self, 所以快照必须在合并"之前"取,
            # 否则就是拿对象跟自己比, 永远判定为 unchanged, 更新永远不会落库。
            before = existing.to_json()
            merged = existing.merge(album)
            merged.key = existing.key
            status = "updated" if merged.to_json() != before else "unchanged"
            if status == "unchanged":
                self._link_sources(merged)
                return status
        self._write_album(merged, now)
        self._link_sources(merged)
        return status

    def _write_album(self, album: Album, now: float) -> None:
        # **标签/曲风收口**: 先按逗号拆成独立词(否则 "CD, Digital" 整串躲过过滤),
        # 再各自去噪。放在这里而不是每个解析器里改 —— 所有数据源都走这一个写入口。
        album.tags = [t for t in split_tag_values(album.tags) if is_tag_worth_keeping(t)]
        album.genres = [g for g in split_tag_values(album.genres) if not is_noise_genre(g)]
        # **来源痕迹收口**: 所有数据源都走这一个写入口, 所以在这里统一剔除
        # VGMdb / Bangumi / DoujinStyle 的链接、编号与"提供的格式"之类说明,
        # 保证任何入库路径(采集/手动上传/歌单反向导入)都不会把它们带进库。
        try:
            _scrub_album_sources(album)
        except Exception:            # noqa: BLE001 - 清洗失败不能阻断入库
            pass
        payload = album.to_json()
        year = (album.release_date or "")[:4]
        self.execute(
            """
            INSERT INTO albums(key, title, title_original, circle, event, event_kind, release_date, year,
                               cover_url, cover_local, track_count, norm_title, norm_circle, payload,
                               created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                title=excluded.title, title_original=excluded.title_original, circle=excluded.circle,
                event=excluded.event, event_kind=excluded.event_kind, release_date=excluded.release_date,
                year=excluded.year, cover_url=excluded.cover_url, cover_local=excluded.cover_local,
                track_count=excluded.track_count, norm_title=excluded.norm_title,
                norm_circle=excluded.norm_circle, payload=excluded.payload, updated_at=excluded.updated_at
            """,
            (
                album.key,
                album.title,
                album.title_original,
                album.circle,
                album.event,
                album.event_kind,
                album.release_date,
                year,
                album.cover_url,
                album.cover_local,
                len(album.tracks),
                textutil.norm_key(album.title_original or album.title, romaji=False),
                textutil.norm_key(album.circle, romaji=False),
                payload,
                now,
                now,
            ),
        )
        # 展会不再进标签: 它是结构化字段(event), 进标签只会重复(实测这类占一半出现次数)
        for tag in album.tags + album.genres:
            tag = textutil.clean_whitespace(tag)
            # 曲风走更严的判据(介质/版本/出处一律不算曲风); 标签用原判据。
            # 但写进 album_tags 表时统一按标签处理 —— 表里只存"可检索的词"。
            if tag and is_tag_worth_keeping(tag):
                self.execute("INSERT OR IGNORE INTO album_tags(album_key, tag) VALUES (?,?)", (album.key, tag))

    def _link_sources(self, album: Album) -> None:
        now = time.time()
        for source, sid in album.source_ids.items():
            if not sid:
                continue
            self.execute(
                "REPLACE INTO album_sources(source, source_id, album_key, url, updated_at) VALUES (?,?,?,?,?)",
                (source, str(sid), album.key, album.urls.get(source, ""), now),
            )

    def get_album(self, key: str) -> Album | None:
        row = self.query_one("SELECT payload FROM albums WHERE key = ?", (key,))
        if row is None:
            return None
        return Album.from_json(row["payload"])

    def get_album_by_source(self, source: str, source_id: str) -> Album | None:
        row = self.query_one(
            "SELECT album_key FROM album_sources WHERE source = ? AND source_id = ?", (source, str(source_id))
        )
        if row is None:
            return None
        return self.get_album(row["album_key"])

    def find_similar_album(
        self,
        album: Album,
        *,
        threshold: float = 0.94,
        mode: str | None = None,
    ) -> Album | None:
        """按「标题相似度 + 社团归属」找同源重复.

        为什么要模糊比较(而不是字符串相等): 同一张碟在不同站点上的写法并不一致 ——

        * 社团名多/少一段括号补充: ``nitro hyperspace (nitro)`` vs ``nitro hyperspace``;
          ``Winter Bouquet`` vs ``柊さんた・雛森アンシア(Winter Bouquet)``;
        * 标题的大小写/标点/全半角差异: ``BLOOM`` vs ``Bloom``。

        以前这里只做 ``norm_title`` 与 ``norm_circle`` 的**完全相等**比较,
        ``threshold`` 参数虽然写在签名里却从未被使用 —— 于是每轮采集都会
        为「同一张碟的另一种写法」新建一条记录, 跑几轮就满地重复。

        ``mode``:

        * ``"strict"`` —— 只有**两边社团都存在且相同**时才合并, 且不做模糊标题;
        * ``"balanced"``(默认) —— 社团归属一致(允许括号补充/词子集), 标题相似度
          达到 ``threshold`` 即合并; 两边都没有社团时只互相匹配(旧行为)。
        * ``"off"`` —— 除站点原生 ID 外一律不合并。
        """
        mode = (mode or getattr(self, "dedupe_mode", "balanced") or "balanced").lower()
        if mode == "off":
            return None
        norm_title = textutil.norm_key(album.title_original or album.title, romaji=False)
        if not norm_title:
            return None
        strict = mode == "strict"
        best: tuple[float, Album] | None = None
        for cand in self._dedupe_candidates(album, norm_title):
            if not _is_duplicate_pair(album, cand, threshold=threshold, strict=strict):
                continue
            score = _title_similarity(album, cand)
            if best is None or score > best[0]:
                best = (score, cand)
        return best[1] if best else None

    def _dedupe_candidates(self, album: Album, norm_title: str) -> list[Album]:
        """拉一批可能重复的候选(按标题 / 社团 / 社团核心三段查询, 去重后返回)."""
        rows: dict[str, Album] = {}

        def take(sql: str, params: Sequence[Any]) -> None:
            for row in self.query(sql, params):
                if row["key"] in rows:
                    continue
                try:
                    rows[row["key"]] = Album.from_json(row["payload"])
                except (ValueError, TypeError):
                    continue

        take("SELECT key, payload FROM albums WHERE norm_title = ? LIMIT 40", (norm_title,))
        norm_circle = textutil.norm_key(album.circle, romaji=False)
        if norm_circle:
            take("SELECT key, payload FROM albums WHERE norm_circle = ? LIMIT 40", (norm_circle,))
        core = _circle_core(album.circle)
        if core and core != norm_circle:
            take("SELECT key, payload FROM albums WHERE norm_circle LIKE ? LIMIT 40", (f"%{core}%",))

        # **源内 id**: 同一个站点的同一条目就是同一张碟, 与标题/社团是否被改过无关。
        # 这是"人工改过信息后被别的源重新抓成重复"的第一道防线。
        for source, sid in (album.source_ids or {}).items():
            if not source or not sid:
                continue
            take(
                "SELECT a.key, a.payload FROM albums a"
                " JOIN album_sources s ON s.album_key = a.key"
                " WHERE s.source = ? AND s.source_id = ? LIMIT 20",
                (str(source), str(sid)),
            )

        # **编号(catalog_no)**: 商品编号是碟的唯一标识, 用户一般不会去改它。
        # 标题被手改过时, norm_title 匹配会失效, 但编号还能把它认回来。
        catalog = textutil.clean_whitespace(album.catalog_no) if album.catalog_no else ""
        if catalog:
            take(
                "SELECT key, payload FROM albums"
                " WHERE json_extract(payload, '$.catalog_no') = ? LIMIT 20",
                (catalog,),
            )
        return list(rows.values())

    @staticmethod
    def _album_filters(
        *,
        event: str | None = None,
        event_kind: str | None = None,
        tag: str | None = None,
        source: str | None = None,
        query: str | None = None,
        year: str | None = None,
        mark: str | None = None,
        only_unmarked: bool = False,
        only_unmatched: bool = False,
        exclude_mark: str | None = None,
        has_cover: bool | None = None,
    ) -> tuple[str, list[Any]]:
        """把筛选条件拼成 WHERE 子句 —— list_albums 与 count_albums 共用.

        抽出来是为了让"总数"和"取一页"用的是**同一套条件**, 否则分页页数会算错。
        """
        clauses: list[str] = []
        params: list[Any] = []
        if event:
            clauses.append("a.event = ?")
            params.append(event)
        if event_kind:
            clauses.append("a.event_kind = ?")
            params.append(event_kind)
        if year:
            clauses.append("a.year = ?")
            params.append(str(year))
        if source:
            clauses.append("EXISTS (SELECT 1 FROM album_sources s WHERE s.album_key = a.key AND s.source = ?)")
            params.append(source)
        if tag:
            clauses.append(
                "(EXISTS (SELECT 1 FROM album_tags t WHERE t.album_key = a.key AND t.tag = ?)"
                " OR a.event = ? OR EXISTS (SELECT 1 FROM album_tags t2 WHERE t2.album_key = a.key AND t2.tag LIKE ?))"
            )
            params.extend([tag, tag, f"%{tag}%"])
        if query:
            # 输入框写着「搜索标题 / 社团 / 曲目」, 但原来只查了 title/title_original/circle
            # —— **曲目根本没查**, 而且不做归一化, 所以 "Something blue*" 里的 * 只能靠
            # 库里恰好也带 * 才撞上。
            #
            # 现在: 原文 + 归一化两条腿, 覆盖 标题 / 原名 / 社团 / 编号 / 艺人 / 曲目。
            # 归一化用 ``norm_key(romaji=False)``: 全角转半角、去标点、压空白,
            # 但**保留假名与汉字**(romaji=True 会把「だってもっと…」变成半截罗马字, 反而搜不到)。
            raw = str(query).strip()
            needle = f"%{raw}%"
            norm = textutil.norm_key(raw, romaji=False, drop_noise=True)
            norm_needle = f"%{norm}%" if norm else needle
            clauses.append(
                "(a.title LIKE ? OR a.title_original LIKE ? OR a.circle LIKE ?"
                " OR json_extract(a.payload, '$.catalog_no') LIKE ?"
                " OR a.norm_title LIKE ? OR a.norm_circle LIKE ?"
                " OR EXISTS (SELECT 1 FROM json_each(a.payload, '$.artists') ja"
                "            WHERE ja.value LIKE ?)"
                " OR EXISTS (SELECT 1 FROM json_each(a.payload, '$.tracks') jt"
                "            WHERE jt.value ->> 'title' LIKE ?))"
            )
            params.extend([needle, needle, needle, needle,
                           norm_needle, norm_needle, needle, needle])
        if mark:
            clauses.append("EXISTS (SELECT 1 FROM album_marks m WHERE m.album_key = a.key AND m.mark = ?)")
            params.append(mark)
        if exclude_mark:
            clauses.append("NOT EXISTS (SELECT 1 FROM album_marks m WHERE m.album_key = a.key AND m.mark = ?)")
            params.append(exclude_mark)
        if only_unmarked:
            clauses.append("NOT EXISTS (SELECT 1 FROM album_marks m WHERE m.album_key = a.key)")
        if only_unmatched:
            clauses.append("NOT EXISTS (SELECT 1 FROM netease_matches n WHERE n.album_key = a.key)")
        if has_cover is True:
            clauses.append("(a.cover_local <> '' OR a.cover_url <> '')")
        elif has_cover is False:
            clauses.append("(a.cover_local = '' AND a.cover_url = '')")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def count_albums(self, **filters: Any) -> int:
        """当前筛选条件下的**总条数**(分页算页数要用它, 不能用当前页长度)."""
        where, params = self._album_filters(**filters)
        row = self.query_one(f"SELECT COUNT(*) AS n FROM albums a{where}", params)
        return int(row["n"]) if row else 0

    def list_albums(
        self,
        *,
        event: str | None = None,
        event_kind: str | None = None,
        tag: str | None = None,
        source: str | None = None,
        query: str | None = None,
        year: str | None = None,
        mark: str | None = None,
        only_unmarked: bool = False,
        only_unmatched: bool = False,
        exclude_mark: str | None = None,
        has_cover: bool | None = None,
        limit: int | None = None,
        offset: int = 0,
        order: str = "updated",
    ) -> list[Album]:
        where, params = self._album_filters(
            event=event, event_kind=event_kind, tag=tag, source=source, query=query,
            year=year, mark=mark, only_unmarked=only_unmarked,
            only_unmatched=only_unmatched, exclude_mark=exclude_mark,
            has_cover=has_cover,
        )
        order_sql = {
            "circle": "a.circle COLLATE NOCASE, a.release_date, a.title",
            "title": "a.title COLLATE NOCASE",
            "date": "a.release_date DESC, a.circle COLLATE NOCASE",
            "updated": "a.updated_at DESC",
        }.get(order, "a.circle COLLATE NOCASE, a.title")
        sql = f"SELECT a.payload FROM albums a{where} ORDER BY {order_sql}"
        if limit:
            sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
        return [Album.from_json(r["payload"]) for r in self.query(sql, params)]

    def updated_at_map(self, keys: Sequence[str]) -> dict[str, float]:
        """``key -> updated_at``(秒). 卡片要显示"最近更新时间"就得靠它.

        ``updated_at`` 只是数据库列, 不在 payload 里, 所以列表接口要单独取一次。
        一次查一批, 不是每张一次。
        """
        keys = [str(k) for k in keys if k]
        if not keys:
            return {}
        marks = ",".join("?" for _ in keys)
        rows = self.query(
            f"SELECT key, updated_at FROM albums WHERE key IN ({marks})", tuple(keys))
        return {str(r["key"]): float(r["updated_at"] or 0.0) for r in rows}

    def all_albums(self) -> list[Album]:
        return [Album.from_json(r["payload"]) for r in self.query("SELECT payload FROM albums ORDER BY key")]

    def album_tags(self) -> list[tuple[str, int]]:
        rows = self.query(
            "SELECT tag, COUNT(*) AS n FROM album_tags GROUP BY tag ORDER BY n DESC, tag"
        )
        return [(r["tag"], int(r["n"])) for r in rows]

    def album_events(self) -> list[tuple[str, str, int]]:
        rows = self.query(
            "SELECT event, event_kind, COUNT(*) AS n FROM albums WHERE event != ''"
            " GROUP BY event, event_kind ORDER BY event DESC"
        )
        return [(r["event"], r["event_kind"], int(r["n"])) for r in rows]

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for table in ("albums", "album_sources", "album_tags", "album_marks", "netease_matches"):
            row = self.query_one(f"SELECT COUNT(*) AS n FROM {table}")
            out[table] = int(row["n"]) if row else 0
        # **已匹配但没封面** —— 这些碟在歌单对照里会显示"无封面", 而它们本来可以
        # 直接用网易云的专辑图。单独报出来, 省得又变成"为什么有匹配却没图"。
        row = self.query_one(
            "SELECT COUNT(*) AS n FROM netease_matches m JOIN albums a ON a.key = m.album_key"
            " WHERE a.cover_url = '' AND a.cover_local = ''"
        )
        out["matches_missing_cover"] = int(row["n"]) if row else 0
        # 社团是独立的一等公民数据(见 circles 表), 概览卡片上也要能看到有多少条
        row = self.query_one("SELECT COUNT(*) AS n FROM circles")
        out["circles"] = int(row["n"]) if row else 0
        return out

    # -- 社团 ---------------------------------------------------------------

    def _circle_from_row(self, row: sqlite3.Row) -> Circle:
        """把 circles 表的一行还原成 :class:`Circle`.

        以 ``payload`` 为准(它才是完整的社团对象), payload 坏了/为空时退回各列 ——
        老库或者外部工具写坏 payload 时, 至少别让整个社团读不出来。
        读社团结点时列与 payload 必须**一起**处理, 免得出现"列改了 payload 没改"
        (读出来还是旧值)那种坑 —— 写入侧由 :meth:`_save_circle` 保证两处同步。
        """
        data: Any = None
        try:
            data = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            data = None
        if not isinstance(data, dict) or not data.get("name"):
            try:
                urls = json.loads(row["urls_json"] or "{}")
            except (TypeError, ValueError):
                urls = {}
            data = {
                "name": row["name"],
                "logo_url": row["logo_url"],
                "logo_local": row["logo_local"],
                "activity_type": row["activity_type"],
                "founded": row["founded"],
                "last_active": row["last_active"],
                "description": row["description"],
                "urls": urls if isinstance(urls, dict) else {},
            }
        circle = Circle.from_dict(data)
        if not circle.created_at:
            circle.created_at = float(row["created_at"] or 0.0)
        if not circle.updated_at:
            circle.updated_at = float(row["updated_at"] or 0.0)
        return circle

    def _staff_of(self, name: str) -> list[CircleStaff]:
        rows = self.query(
            "SELECT role, name FROM circle_staff WHERE circle = ? ORDER BY position, rowid",
            (str(name),),
        )
        out: list[CircleStaff] = []
        for row in rows:
            try:
                out.append(CircleStaff(name=row["name"], role=row["role"]))
            except ValueError:
                # 库里混进非法角色(手改库/老数据)时跳过这一条, 而不是让整个社团读不出来
                continue
        return out

    def _staff_map(self, names: Sequence[str]) -> dict[str, list[CircleStaff]]:
        """一次查完一批社团的人员(列表页每行都要显示主催, 不能一行一个查询)."""
        cleaned = [str(n) for n in names if n]
        out: dict[str, list[CircleStaff]] = {n: [] for n in cleaned}
        for start in range(0, len(cleaned), 400):
            chunk = cleaned[start:start + 400]
            if not chunk:
                continue
            marks = ",".join("?" * len(chunk))
            rows = self.query(
                f"SELECT circle, role, name FROM circle_staff WHERE circle IN ({marks})"
                " ORDER BY position, rowid",
                chunk,
            )
            for row in rows:
                try:
                    out.setdefault(str(row["circle"]), []).append(
                        CircleStaff(name=row["name"], role=row["role"])
                    )
                except ValueError:
                    continue
        return out

    def _save_circle(self, circle: Circle, *, created_at: float | None = None) -> None:
        """整条写入社团记录(列 + payload + 人员一起).

        **已有的行走直接 UPDATE, 不搞"合并"语义**: 空值也要覆盖 —— 清掉 logo、
        删掉最后一条链接、把成立时间改空, 都必须真的落库。这与 ``set_uploaded_cover``
        单独用直接 UPDATE 的理由完全一样(``upsert_album`` 那种 trust 感知的合并
        改不了 value -> value, 也清不掉值)。
        """
        now = time.time()
        row = self.query_one("SELECT created_at FROM circles WHERE name = ?", (circle.name,))
        if created_at is None:
            created_at = float(row["created_at"]) if row is not None else now
        circle.created_at = float(created_at or now)
        circle.updated_at = now
        payload = circle.to_json()
        urls_json = json.dumps(circle.urls, ensure_ascii=False, sort_keys=True)
        values = (
            circle.logo_url, circle.logo_local, circle.activity_type, circle.founded,
            circle.last_active, circle.description, urls_json, payload,
            circle.created_at, circle.updated_at,
        )
        if row is None:
            self.execute(
                "INSERT INTO circles(name, logo_url, logo_local, activity_type, founded,"
                " last_active, description, urls_json, payload, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (circle.name, *values),
            )
        else:
            self.execute(
                "UPDATE circles SET logo_url = ?, logo_local = ?, activity_type = ?,"
                " founded = ?, last_active = ?, description = ?, urls_json = ?,"
                " payload = ?, created_at = ?, updated_at = ? WHERE name = ?",
                (*values, circle.name),
            )
        # 人员整组重写: 删人 / 改角色 / 调顺序都靠这一步
        self.execute("DELETE FROM circle_staff WHERE circle = ?", (circle.name,))
        for position, person in enumerate(circle.staff):
            self.execute(
                "INSERT OR REPLACE INTO circle_staff(circle, role, name, position)"
                " VALUES (?,?,?,?)",
                (circle.name, person.role, person.name, position),
            )

    def upsert_circle(self, circle: Circle | Mapping[str, Any], *, now: float | None = None) -> str:
        """写入 / 覆盖一个社团, 返回 ``'created' | 'updated' | 'unchanged'``.

        语义是**整条替换**(写什么就是什么: 没给的字段会被清空)。要做"只改几个字段"
        的局部编辑请用 :meth:`update_circle_fields`。

        ``now`` 只给测试注入固定时间用。
        """
        item = circle if isinstance(circle, Circle) else Circle.from_dict(circle)
        row = self.query_one("SELECT payload FROM circles WHERE name = ?", (item.name,))
        if row is not None:
            try:
                before = Circle.from_dict(json.loads(row["payload"] or "{}"))
            except (TypeError, ValueError, KeyError):
                before = None
            if before is not None and self._circle_core(before) == self._circle_core(item):
                item.staff = self._staff_of(item.name)
                return "unchanged"
        self._save_circle(item, created_at=float(now) if now is not None else None)
        return "updated" if row is not None else "created"

    @staticmethod
    def _circle_core(circle: Circle) -> dict[str, Any]:
        """比较"内容是否变了"用的视图 —— 时间戳不算内容."""
        data = circle.to_dict()
        data.pop("created_at", None)
        data.pop("updated_at", None)
        return data

    def get_circle(self, name: str) -> Circle | None:
        row = self.query_one("SELECT * FROM circles WHERE name = ?", (str(name),))
        if row is None:
            return None
        circle = self._circle_from_row(row)
        circle.staff = self._staff_of(circle.name)
        return circle

    def list_circles(
        self,
        *,
        query: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        order: str = "name",
    ) -> list[Circle]:
        """按名字/简介/人员模糊搜索社团, 支持排序与分页.

        ``order``: ``name`` | ``updated`` | ``last_active`` | ``founded`` | ``albums``
        (``albums`` 按"这个社团名下有多少张专辑"排, 用于列表页一眼看出主力社团)。
        """
        clauses: list[str] = []
        params: list[Any] = []
        for term in re.split(r"[\s,，、]+", (query or "").strip()):
            if not term:
                continue
            like = f"%{term}%"
            clauses.append(
                "(c.name LIKE ? OR c.description LIKE ? OR c.urls_json LIKE ?"
                " OR EXISTS (SELECT 1 FROM circle_staff s"
                "            WHERE s.circle = c.name AND s.name LIKE ?))"
            )
            params.extend([like, like, like, like])
        sql = (
            "SELECT c.*, (SELECT COUNT(*) FROM albums a WHERE a.circle = c.name) AS album_count"
            " FROM circles c"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY " + _CIRCLE_ORDER_SQL.get(order or "name", _CIRCLE_ORDER_SQL["name"])
        # SQLite 的 LIMIT -1 = 不限制 —— 用 None 表示"全都要"更顺手
        sql += " LIMIT ? OFFSET ?"
        params.extend([int(limit) if limit is not None else -1, max(0, int(offset))])
        rows = self.query(sql, params)
        circles = [self._circle_from_row(row) for row in rows]
        staff = self._staff_map([c.name for c in circles])
        for circle in circles:
            circle.staff = staff.get(circle.name, [])
        return circles

    def circle_album_counts(self) -> dict[str, int]:
        """每个社团名下的专辑数(只统计 ``albums.circle`` 非空的)."""
        rows = self.query(
            "SELECT circle, COUNT(*) AS n FROM albums WHERE circle != '' GROUP BY circle"
        )
        return {str(row["circle"]): int(row["n"]) for row in rows}

    def albums_of_circle(self, name: str, *, limit: int = 200) -> list[Album]:
        """社团名下的专辑(列表页/详情页用; 按发售日倒序)."""
        rows = self.query(
            "SELECT payload FROM albums WHERE circle = ? ORDER BY release_date DESC, key LIMIT ?",
            (str(name), max(1, int(limit))),
        )
        return [Album.from_json(row["payload"]) for row in rows]

    def update_circle_fields(self, name: str, patch: dict[str, Any], *, create: bool = False) -> tuple[bool, str]:
        """人工编辑社团信息(Web 面板「社团」页 / CLI 用).

        与 :meth:`update_album_fields` 同语义: **写什么就是什么** —— 能清空、能改正。
        ``staff`` 与 ``urls`` 是**整组替换**(传空列表/空对象就是全删), 因为界面上的
        编辑器本来就持有完整的一组; 想只删一个人, 前端把剩下的完整列表发过来。

        ``create=True``: 库里没有这个社团名时**先建再改**(面板上"新建"与"编辑"共用一个
        入口); 校验失败会把刚建的空记录回滚掉, 不留半个空社团。
        """
        if not isinstance(patch, dict):
            return False, "patch 必须是对象"
        circle = self.get_circle(name)
        created = False
        if circle is None:
            if not create:
                return False, "找不到这个社团"
            try:
                circle = Circle(name=str(name))
            except ValueError as exc:
                return False, str(exc)
            created = True

        def give_up(message: str) -> tuple[bool, str]:
            if created:
                self.delete_circle(circle.name)      # 校验没过就把刚建的空记录收回去
            return False, message

        unknown = [k for k in patch if k not in _CIRCLE_EDITABLE_FIELDS and k != "name"]
        if unknown:
            return give_up("不支持修改的字段: " + ", ".join(sorted(unknown)))
        if "name" in patch and str(patch["name"]).strip() != circle.name:
            return give_up("社团名是主键, 不支持改名（请新建一个社团，并到专辑里改社团名）")

        def as_text(value: Any) -> str:
            return str(value).strip() if value is not None else ""

        try:
            for field in ("logo_url", "logo_local", "description"):
                if field in patch:
                    setattr(circle, field, as_text(patch[field]))
            if "activity_type" in patch:
                circle.activity_type = normalize_activity_type(patch["activity_type"])
            if "founded" in patch:
                circle.founded = normalize_partial_date(patch["founded"])
            if "last_active" in patch:
                circle.last_active = normalize_partial_date(patch["last_active"])
            if "urls" in patch:
                raw_urls = patch["urls"]
                if raw_urls in (None, ""):
                    circle.urls = {}
                elif isinstance(raw_urls, Mapping):
                    circle.urls = {
                        as_text(k): as_text(v) for k, v in raw_urls.items() if as_text(k) and as_text(v)
                    }
                else:
                    raise ValueError("相关链接必须是 {名称: 链接} 对象")
            if "staff" in patch:
                raw_staff = patch["staff"]
                if raw_staff in (None, ""):
                    circle.staff = []
                elif isinstance(raw_staff, Iterable):
                    built: list[CircleStaff] = []
                    for item in raw_staff:
                        if isinstance(item, CircleStaff):
                            built.append(item)
                        elif isinstance(item, Mapping):
                            built.append(CircleStaff.from_dict(item))
                        else:
                            raise ValueError("人员每一项必须是 {name, role} 对象")
                    circle.staff = built
                    # 重新走一遍数据类的规整(去重 / 大小写不敏感的同人判重)
                    circle = Circle.from_dict({**circle.to_dict(), "staff": [p.to_dict() for p in built]})
                else:
                    raise ValueError("人员必须是数组")
        except ValueError as exc:
            return give_up(str(exc))

        self._save_circle(circle)
        return True, ""

    def delete_circle(self, name: str) -> bool:
        """删掉社团记录(连同人员)。

        **不动专辑**: ``albums.circle`` 是字符串, 删社团只是不再有这条补充信息,
        专辑还在、社团名也还在卡片上 —— 与删除专辑是两件独立的事。
        """
        row = self.query_one("SELECT name FROM circles WHERE name = ?", (str(name),))
        if row is None:
            return False
        self.execute("DELETE FROM circle_staff WHERE circle = ?", (str(name),))
        self.execute("DELETE FROM circles WHERE name = ?", (str(name),))
        return True

    # -- 社团 logo(与专辑封面的双字段模式一致: 本地文件 / 外链二选一) -------

    def set_circle_logo_url(self, name: str, logo_url: str) -> bool:
        """写外部 logo 直链, 同时**清掉本地文件路径**(用户贴了链接就以链接为准)."""
        ok, _ = self.update_circle_fields(name, {"logo_url": str(logo_url or ""), "logo_local": ""})
        return ok

    def set_circle_logo_local(self, name: str, logo_path: str) -> bool:
        """写用户上传的本地 logo, 同时**清掉远程地址**.

        走直接 UPDATE 的那条路(:meth:`_save_circle`): 空值必须能覆盖旧值, 否则
        "换成自己传的图"时老外链会一直挂着。
        """
        ok, _ = self.update_circle_fields(name, {"logo_local": str(logo_path or ""), "logo_url": ""})
        return ok

    def clear_circle_logo(self, name: str, *, delete_file: bool = True) -> tuple[bool, str, bool]:
        """清掉 logo, 返回 ``(社团是否存在, 原来的本地文件路径, 文件是否真的删了)``.

        文件删除的安全边界与 :meth:`clear_album_covers` 一致: **只删 ``data/circles/``
        目录下的文件**, 别的路径一律不碰 —— 库里的路径有可能是用户手工填的,
        照着删就是把人家自己的图删了。
        """
        circle = self.get_circle(name)
        if circle is None:
            return False, "", False
        old_local = circle.logo_local
        ok, _ = self.update_circle_fields(name, {"logo_url": "", "logo_local": ""})
        removed = False
        if delete_file and old_local:
            logos_root = (self.path.parent / "circles").resolve()
            try:
                target = Path(old_local).resolve()
            except (OSError, RuntimeError):
                target = None
            if target is not None and logos_root in target.parents and target.is_file():
                try:
                    target.unlink()
                    removed = True
                except OSError:
                    removed = False
        return ok, old_local, removed

    # -- 网易云匹配 ---------------------------------------------------------

    def save_match(self, match: MatchResult) -> None:
        payload = json.dumps(match.to_dict(), ensure_ascii=False)
        ts = _parse_ts(match.matched_at)
        self.execute(
            """
            INSERT OR REPLACE INTO netease_matches(album_key, kind, target_id, target_name, target_artists,
                target_album_id, target_album_name, score, title_score, artist_score, track_score,
                method, payload, matched_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                match.album_key,
                match.kind,
                int(match.target_id),
                match.target_name,
                json.dumps(match.target_artists, ensure_ascii=False),
                int(match.target_album_id),
                match.target_album_name,
                float(match.score),
                float(match.title_score),
                float(match.artist_score),
                float(match.track_score),
                match.method,
                payload,
                ts,
            ),
        )
        # **互斥规则收敛在这里**: 人工匹配 = 用户"这张才是对的", 所以顺手把**那一条**候选的
        # 拉黑解掉(否则他刚手挂完, 那条又被自己的黑名单挡着, 会显得"手动匹配不生效")。
        # 注意只解这一条: 其它被拉黑的候选仍然是他明确拒过的, 不该被顺手放开。
        if str(getattr(match, "method", "") or "") == MANUAL_METHOD:
            target_album_id = int(getattr(match, "target_album_id", 0) or 0)
            if target_album_id:
                self.clear_block(match.album_key, target_album_id)

    def manual_matches(self) -> list[MatchResult]:
        """所有**人工指定**的匹配(面板/CLI 用来列"哪些是我手挂的")."""
        rows = self.query(
            "SELECT payload FROM netease_matches WHERE method = ? ORDER BY matched_at DESC",
            (MANUAL_METHOD,),
        )
        return [MatchResult.from_dict(json.loads(r["payload"])) for r in rows]

    def clear_manual_match(self, album_key: str) -> int:
        """**撤销手动标记, 改回自动匹配**: 删掉那条 ``method="manual"`` 的匹配记录.

        只删"人工指定"的那条 —— 如果该 key 当前是自动匹配(或压根没匹配), 什么都不做,
        返回 0。这样即使前端传错了 key 也不会误删算法的结果。
        返回删掉的条数。
        """
        key = str(album_key or "").strip()
        if not key:
            return 0
        current = self.get_match(key)
        if current is None or not str(getattr(current, "method", "") or "") == MANUAL_METHOD:
            return 0
        return self.delete_match(key)

    # -- 封面复用检测 ----------------------------------------------------------
    #
    # 同一张封面被**多张不同专辑**共用, 基本可以断定抓错了 —— 最常见的是抓到
    # 艺人的主页图(SoundCloud 的 artwork 就是)或站点的通用图。
    # 实测踩到过: `CUTE Fruits` 和 `Fruit Basket : Orange` 拿到同一张 sndcdn 作品图。

    def duplicate_covers(self, *, min_count: int = 2, event: str = "") -> list[dict]:
        """返回被 >= ``min_count`` 张专辑共用的封面 URL, 带上用到它的专辑."""
        sql = ("SELECT cover_url, COUNT(*) AS n FROM albums "
               "WHERE cover_url != '' AND cover_url IS NOT NULL")
        params: tuple = ()
        if event:
            sql += " AND event = ?"
            params = (event,)
        sql += " GROUP BY cover_url HAVING n >= ? ORDER BY n DESC, cover_url"
        rows = self.query(sql, params + (int(min_count),))
        out: list[dict] = []
        for row in rows:
            url = str(row["cover_url"])
            albums = self.query(
                "SELECT key, title, title_original, circle, event FROM albums WHERE cover_url = ?"
                " ORDER BY circle, title", (url,))
            out.append({
                "cover_url": url,
                "count": int(row["n"]),
                "albums": [
                    {
                        "key": str(a["key"]),
                        "title": str(a["title_original"] or a["title"] or ""),
                        "circle": str(a["circle"] or ""),
                        "event": str(a["event"] or ""),
                    }
                    for a in albums
                ],
            })
        return out

    def reclassify_special_links(self) -> int:
        """把 ``special`` 里**其实不是特设页**的链接按主机重新归类.

        CD 的 ``link_url`` 实测有指向 X 原帖 / YouTube 的 —— 那些不该占着"特设页"字段。
        """
        try:
            from .sources.dojinmusicinfo import classify_link_url
        except Exception:
            return 0

        changed = 0
        for row in self.query("SELECT key, payload FROM albums"):
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            urls = payload.get("urls")
            if not isinstance(urls, dict):
                continue
            old = str(urls.get("special") or "").strip()
            if not old:
                continue
            key = classify_link_url(old, default="special")
            if key == "special" or not key:
                continue
            urls.setdefault(key, old)
            urls.pop("special", None)
            payload["urls"] = urls
            self.execute(
                "UPDATE albums SET payload = ?, updated_at = ? WHERE key = ?",
                (json.dumps(payload, ensure_ascii=False), time.time(), str(row["key"])),
            )
            changed += 1
        return changed

    def migrate_special_to_circle_site(self) -> int:
        """一次性迁移: 把旧的 ``urls["special"]`` 挪到 ``urls["circle_site"]``.

        背景: 老解析器把「社团名后面第一个链接」写进了 ``special``, 但那其实是**社团的
        官方/链接聚合页**(实测主机名全是 lit.link / linktr.ee / YouTube 频道 / Google Sites),
        不是专辑特设页。真正的特设页只来自 CD 记录自己的 ``link_url``。

        所以库里现存的 ``special`` 全都是错语义, 统一挪到 ``circle_site`` 并把
        ``special`` 清空 —— 这样面板上就不会再把社团主页当成"特设页"展示。
        返回迁移了几条。
        """
        moved = 0
        for row in self.query("SELECT key, payload FROM albums"):
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            urls = payload.get("urls")
            if not isinstance(urls, dict):
                continue
            old_special = str(urls.get("special") or "").strip()
            if not old_special:
                continue
            if not str(urls.get("circle_site") or "").strip():
                urls["circle_site"] = old_special
            urls.pop("special", None)
            payload["urls"] = urls
            self.execute(
                "UPDATE albums SET payload = ?, updated_at = ? WHERE key = ?",
                (json.dumps(payload, ensure_ascii=False), time.time(), str(row["key"])),
            )
            moved += 1
        return moved

    def set_cover_url(self, album_key: str, cover_url: str) -> bool:
        """直接替换远程封面 URL(同时写列与 payload).

        **必须直接 UPDATE**: ``upsert_album`` 是 trust 感知的合并, 空值不覆盖已有值,
        而"值 -> 另一个值"在 trust 相同时也倾向于保留旧的 —— 实测封面替换会静默失效。
        """
        row = self.query_one("SELECT payload FROM albums WHERE key = ?", (str(album_key),))
        if row is None:
            return False
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload["cover_url"] = str(cover_url)
        payload["cover_local"] = ""
        self.execute(
            "UPDATE albums SET cover_url = ?, cover_local = '', payload = ?,"
            " updated_at = ? WHERE key = ?",
            (str(cover_url), json.dumps(payload, ensure_ascii=False),
             time.time(), str(album_key)),
        )
        return True

    def set_uploaded_cover(self, album_key: str, cover_path: str) -> bool:
        """写用户上传的本地封面, 并**清掉远程 cover_url**.

        为什么不能走 ``upsert_album``: 它是 trust 感知的合并, **空值不会覆盖已有值**,
        所以 ``cover_url`` 清不掉, 老的错误封面会一直挂着。
        和 ``clear_cover`` 一样, 这里直接 UPDATE 列 + 改写 payload。
        """
        row = self.query_one("SELECT payload FROM albums WHERE key = ?", (str(album_key),))
        if row is None:
            return False
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload["cover_local"] = str(cover_path)
        payload["cover_url"] = ""
        self.execute(
            "UPDATE albums SET cover_local = ?, cover_url = '', payload = ?,"
            " updated_at = ? WHERE key = ?",
            (str(cover_path), json.dumps(payload, ensure_ascii=False),
             time.time(), str(album_key)),
        )
        return True

    def suspicious_special_covers(self, *, event: str = "") -> list[dict]:
        """列出手动"封面与社团主页同域"的可疑封面.

        依据: 历史上 ``urls["special"]`` 存的其实是**社团官方页面**(不是专辑特设页),
        从那个页面抓来的封面多半是社团主页图。判据是 **封面 URL 的主机名 ==
        该专辑 special/circle_site 的主机名** —— 同一台主机上既当主页又当封面,
        基本就是主页上的某张图。

        这是**启发式**, 不是铁证(有些社团确实把封面放在自己站上),
        所以配上 ``--clear`` 时要先看清单。
        """
        from urllib.parse import urlparse

        sql = "SELECT key, title, title_original, circle, cover_url, payload FROM albums WHERE cover_url != ''"
        params: tuple = ()
        if event:
            sql += " AND event = ?"
            params = (event,)
        out: list[dict] = []
        for row in self.query(sql, params):
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            urls = (payload or {}).get("urls") or {}
            cover_host = urlparse(str(row["cover_url"])).netloc.lower()
            if not cover_host:
                continue
            page = str(urls.get("special") or urls.get("circle_site") or "")
            page_host = urlparse(page).netloc.lower()
            # 只把**看起来是站点通用图**的算作可疑: 同域 + 文件名很泛
            # (og / ogp / og-image / logo / main / top / thumbnail / eyecatch ...)。
            # 实测同域本身不够 —— `fsx_diversion_kari_800.jpg` 这种同域但文件名带专辑名的
            # 往往就是真封面, 直接清会误删。
            generic = any(hint in str(row["cover_url"]).lower()
                          for hint in _GENERIC_COVER_HINTS)
            if page_host and cover_host == page_host and generic:
                out.append({
                    "key": str(row["key"]),
                    "title": str(row["title_original"] or row["title"] or ""),
                    "circle": str(row["circle"] or ""),
                    "cover_url": str(row["cover_url"]),
                    "page": page,
                })
        return out

    def clear_cover(self, cover_url: str, *, event: str = "") -> int:
        """把某个 URL 上的封面从专辑里清掉(连本地文件路径一起), 返回清了几张.

        两个坑:

        * 库里 ``cover_url`` 既有**列**也有 ``payload`` JSON, 读回来走 payload ——
          只改列的话读出来还是老值, 两个都得改。
        * **不能走 ``upsert_album``**: 它是 trust 感知的合并, 空的封面不会覆盖已有的,
          所以清不掉。这里直接 UPDATE。
        """
        sql = "SELECT key, payload FROM albums WHERE cover_url = ?"
        params: tuple = (str(cover_url),)
        if event:
            sql += " AND event = ?"
            params = params + (event,)
        rows = self.query(sql, params)
        cleared = 0
        for row in rows:
            key = str(row["key"])
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            if isinstance(payload, dict):
                payload["cover_url"] = ""
                payload["cover_local"] = ""
            self.execute(
                "UPDATE albums SET cover_url = '', cover_local = '', payload = ?,"
                " updated_at = ? WHERE key = ?",
                (json.dumps(payload, ensure_ascii=False), time.time(), key),
            )
            cleared += 1
        return cleared

    def clear_album_covers(self, keys: Sequence[str], *, delete_files: bool = True) -> dict[str, Any]:
        """按专辑删掉封面: 远程地址 + 本地文件一起清.

        为什么必须有这个方法: 面板上"把封面地址清空"只写空了 ``cover_url``,
        而封面实际是靠 ``cover_local``(下载/上传到 ``data/covers/`` 的那张图)显示的,
        所以地址清掉以后图还在。这里两个字段一起清, 并把没人再用的本地文件删掉。

        安全边界: 只删 ``data/covers/`` 目录下的文件(别的路径一律不碰),
        并且**还有其它专辑引用同一个文件时不删**(封面被多张专辑共用的情况确实存在,
        见 :meth:`duplicate_covers`)。
        """
        covers_root = (self.path.parent / "covers").resolve()
        cleared = 0
        files_deleted = 0
        missing: list[str] = []
        for raw_key in keys:
            key = str(raw_key or "")
            album = self.get_album(key)
            if album is None:
                missing.append(key)
                continue
            local = (album.cover_local or "").strip()
            if local or album.cover_url:
                cleared += 1
            album.cover_local = ""
            album.cover_url = ""
            self._write_album(album, time.time())
            if not delete_files or not local:
                continue
            try:
                target = Path(local).resolve()
            except (OSError, RuntimeError):
                continue
            if covers_root not in target.parents or not target.is_file():
                continue
            shared = self.query_one(
                "SELECT 1 FROM albums WHERE cover_local = ? LIMIT 1", (local,))
            if shared is not None:
                continue
            try:
                target.unlink()
                files_deleted += 1
            except OSError:
                pass
        return {"cleared": cleared, "files": files_deleted, "missing": missing}

    def clear_local_covers(self, keys: Sequence[str]) -> int:
        """只清 ``cover_local``(保留远程 ``cover_url``), 返回清了几张.

        用途: 本地文件被删/换了机器后, 界面会退回远程地址显示 —— 这时要拿掉的
        只是那条失效的本地路径, 别顺手把还能用的远程地址一起清掉。
        """
        cleared = 0
        for raw_key in keys:
            album = self.get_album(str(raw_key or ""))
            if album is None or not album.cover_local:
                continue
            album.cover_local = ""
            self._write_album(album, time.time())
            cleared += 1
        return cleared

    # -- 「查过但没有」的带时效负缓存 ------------------------------------------
    #
    # 网易云的艺人/社团条目是**用户随时创建**的: 同一批专辑里, 有的社团今天还没有
    # 条目, 几个月后就有了。所以负结果只能记住一段时间, 不能永久记 ——
    # 否则会永久错过后来才出现的条目。

    def miss_age(self, kind: str, key: str) -> float | None:
        """这个检索词上次被确认「没有」到现在过了多少秒; ``None`` = 没有记录."""
        row = self.query_one(
            "SELECT checked_at FROM netease_misses WHERE kind = ? AND key = ?",
            (str(kind), str(key)),
        )
        if row is None:
            return None
        return max(0.0, time.time() - float(row["checked_at"]))

    def is_miss_fresh(self, kind: str, key: str, *, ttl_seconds: float) -> bool:
        """负缓存是否仍然有效(在 TTL 内)."""
        if ttl_seconds <= 0:
            return False
        age = self.miss_age(kind, key)
        return age is not None and age <= ttl_seconds

    def record_miss(self, kind: str, key: str) -> None:
        self.execute(
            "INSERT OR REPLACE INTO netease_misses(kind, key, checked_at) VALUES (?, ?, ?)",
            (str(kind), str(key), time.time()),
        )

    def clear_miss(self, kind: str, key: str) -> None:
        """查到东西了就把负记录删掉(下次不用再等 TTL)."""
        self.execute("DELETE FROM netease_misses WHERE kind = ? AND key = ?",
                     (str(kind), str(key)))

    def prune_misses(self, *, ttl_seconds: float) -> int:
        """清掉过期记录, 返回清了几条."""
        if ttl_seconds <= 0:
            return 0
        cutoff = time.time() - ttl_seconds
        cur = self.execute("DELETE FROM netease_misses WHERE checked_at < ?", (cutoff,))
        return int(getattr(cur, "rowcount", 0) or 0)

    def delete_match(self, album_key: str) -> int:
        """删掉某张专辑的匹配记录, 返回删了几条.

        重新匹配时要清掉旧的: 匹配阈值/算法调整后, 之前存下的假阳性不会自己消失,
        报告里就会一直挂着已经不该再算数的匹配。
        """
        cur = self.execute("DELETE FROM netease_matches WHERE album_key = ?", (str(album_key),))
        return int(getattr(cur, "rowcount", 0) or 0)

    def get_match(self, album_key: str) -> MatchResult | None:
        row = self.query_one("SELECT payload FROM netease_matches WHERE album_key = ?", (album_key,))
        return MatchResult.from_dict(json.loads(row["payload"])) if row else None

    def list_matches(self, *, min_score: float = 0.0, limit: int | None = None) -> list[MatchResult]:
        sql = "SELECT payload FROM netease_matches WHERE score >= ? ORDER BY score DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [MatchResult.from_dict(json.loads(r["payload"])) for r in self.query(sql, (float(min_score),))]

    # -- 人工拉黑(**对级**: 本地专辑 × 网易云 album_id) -------------------------
    #
    # 与上面的 ``netease_matches`` 不同, 这张表记的是**人的决定**, 所以:
    # * **没有 TTL** —— 机器缓存会过期, 人的"不要这一条"不该自己失效;
    # * 只在用户显式操作时写入(面板"取消网易云匹配" / ``dddb db unmatch``);
    # * 只在用户显式操作时清除(面板"允许重新匹配/解禁" / ``dddb db rematch``)。
    #
    # **粒度是对, 不是整张碟**(用户纠正过的意图): 用户拒的往往是某一条同名错条, 正确条目
    # 可能就在下一个候选里。所以匹配器**不会**因此跳过整张碟, 而是只把这一条候选排除掉
    # (见 ``NeteaseMatcher`` 里的候选过滤)。

    def block_match(
        self,
        album_key: str,
        target_album_id: int,
        *,
        target_name: str = "",
        reason: str = "",
        match: MatchResult | None = None,
    ) -> bool:
        """拉黑 ``(这张碟, 那条 album_id)`` 这一对; 返回是否真的写入了.

        ``target_album_id`` **必须 > 0** —— 没有 album_id 就构不成"对", 这时不写入并返回
        ``False``(调用方应给出可读错误, 而不是静默记一条无意义的记录)。

        ``match`` 是取消时库里那条 :class:`MatchResult` —— **记分量是重点**: 用户每取消
        一次就产生一条带标签的负样本(标题分/艺人数/曲目重合/method/候选列表)。
        """
        key = str(album_key or "").strip()
        try:
            album_id = int(target_album_id or 0)
        except (TypeError, ValueError):
            album_id = 0
        if not key or album_id <= 0:
            return False
        if match is not None:
            target_name = target_name or str(getattr(match, "target_name", "") or "")
        try:
            candidates_json = json.dumps(list(getattr(match, "candidates", []) or [])[:8],
                                         ensure_ascii=False) if match else ""
        except (TypeError, ValueError):
            candidates_json = ""
        self.execute(
            """
            INSERT OR REPLACE INTO netease_blocks(
                album_key, target_album_id, target_name, reason, blocked_at,
                score, title_score, artist_score, track_score, method, candidates)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (key, album_id, str(target_name or ""), str(reason or ""), time.time(),
             float(getattr(match, "score", 0.0) or 0.0) if match else 0.0,
             float(getattr(match, "title_score", 0.0) or 0.0) if match else 0.0,
             float(getattr(match, "artist_score", 0.0) or 0.0) if match else 0.0,
             float(getattr(match, "track_score", 0.0) or 0.0) if match else 0.0,
             str(getattr(match, "method", "") or "") if match else "",
             candidates_json),
        )
        # **一致性**: 如果库里当前那条匹配**正好就是**这一对, 顺手把它删掉 —— 否则界面会
        # 出现"已经拉黑了它, 却还显示匹配到它"。
        # 注意只在"同一对"时删: 拉黑**别的**候选不会误删已经匹配对的那条(这正是新语义的
        # 价值 —— 用户可以先有一个正确匹配, 再拉黑旁边那些同名错条)。
        try:
            current = self.get_match(key)
            if (current is not None
                    and int(getattr(current, "target_album_id", 0) or 0) == album_id):
                self.delete_match(key)
        except Exception:
            pass
        return True

    def is_blocked(self, album_key: str, target_album_id: int) -> bool:
        """``(这张碟, 那条候选)`` 是否被人工拉黑."""
        return self.get_block(album_key, target_album_id) is not None

    def get_block(self, album_key: str, target_album_id: int) -> dict[str, Any] | None:
        row = self.query_one(
            "SELECT * FROM netease_blocks WHERE album_key = ? AND target_album_id = ?",
            (str(album_key or "").strip(), int(target_album_id or 0)),
        )
        return dict(row) if row else None

    def blocked_ids(self, album_key: str) -> set[int]:
        """这张碟被拉黑的候选 album_id 集合(匹配器每张查一次就够)."""
        rows = self.query("SELECT target_album_id FROM netease_blocks WHERE album_key = ?",
                          (str(album_key or "").strip(),))
        return {int(r["target_album_id"]) for r in rows}

    def blocks_map(self) -> dict[str, set[int]]:
        """一次读出全部拉黑(列表接口用, 避免逐张打库)."""
        out: dict[str, set[int]] = {}
        for r in self.query("SELECT album_key, target_album_id FROM netease_blocks"):
            out.setdefault(str(r["album_key"]), set()).add(int(r["target_album_id"]))
        return out

    def list_blocks(self, album_key: str | None = None, *, limit: int | None = None) -> list[dict[str, Any]]:
        """拉黑记录(新的在前); 给 ``album_key`` 就只看那张碟的."""
        sql = "SELECT * FROM netease_blocks"
        params: tuple[Any, ...] = ()
        if album_key is not None:
            sql += " WHERE album_key = ?"
            params = (str(album_key).strip(),)
        sql += " ORDER BY blocked_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self.query(sql, params)]

    def clear_block(self, album_key: str, target_album_id: int) -> int:
        """**逐条解禁**: 只放开这一对; 返回删了几条(0/1)."""
        cur = self.execute(
            "DELETE FROM netease_blocks WHERE album_key = ? AND target_album_id = ?",
            (str(album_key or "").strip(), int(target_album_id or 0)),
        )
        return int(getattr(cur, "rowcount", 0) or 0)

    def clear_blocks(self, album_key: str) -> int:
        """**清空这张碟的黑名单**(面板「允许重新匹配」= 让它重新参与所有候选)."""
        cur = self.execute("DELETE FROM netease_blocks WHERE album_key = ?",
                           (str(album_key or "").strip(),))
        return int(getattr(cur, "rowcount", 0) or 0)

    def legacy_rejection_rows(self) -> list[dict[str, Any]]:
        """迁移**遗留**的老拒绝记录(第一版 ``netease_rejections`` 表里还没转过来的行).

        正常情况下应该是空的(迁移会把 ``target_album_id > 0`` 的全转成对级拉黑)。
        留下来的只可能是 ``target_album_id = 0`` 的行 —— 它们构不成"对", 无法自动转换,
        所以要暴露出来让人看见, 而不是静默丢弃。
        """
        try:
            rows = self.query("SELECT * FROM netease_rejections")
        except Exception:
            return []
        out = []
        for r in rows:
            row = dict(r)
            if int(row.get("target_album_id") or 0) <= 0:
                out.append(row)
        return out

    # -- 爬取状态 -----------------------------------------------------------

    def set_state(self, source: str, scope: str, key: str, value: str) -> None:
        self.execute(
            "REPLACE INTO crawl_state(source, scope, key, value, updated_at) VALUES (?,?,?,?,?)",
            (source, scope, key, value, time.time()),
        )

    def get_state(self, source: str, scope: str, key: str) -> str | None:
        row = self.query_one(
            "SELECT value FROM crawl_state WHERE source = ? AND scope = ? AND key = ?", (source, scope, key)
        )
        return row["value"] if row else None

    # -- 事件 ---------------------------------------------------------------

    def save_event(self, name: str, *, kind: str = "", year: int = 0, season: str = "",
                   start_date: str = "", end_date: str = "", tags: Sequence[str] = (),
                   logo_url: str = "", logo_local: str = "",
                   links: Mapping[str, str] | None = None,
                   series: str = "", vol: int = 0,
                   vol_label: str = "") -> None:
        """写入/更新一条展会.

        ``logo_url`` / ``logo_local`` / ``links`` 是**词条级**补充信息
        (展会图与官网/售票页等链接), 与 circles 的处理方式一致。
        """
        # 外键要求 series 必须已存在。调用方(采集/编辑/测试)常常先写会次,
        # 所以这里自动补建一个最小系列记录 —— 否则直接 FOREIGN KEY 失败。
        _series_key = (str(series).strip() or None) if series else None
        if _series_key:
            exists = self.query_one(
                "SELECT 1 FROM event_series WHERE name = ?", (_series_key,))
            if exists is None:
                self.save_series(_series_key)

        self.execute(
            "REPLACE INTO events(name, kind, year, season, start_date, end_date,"
            " tags, logo_url, logo_local, links_json, series, vol, vol_label,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, kind, int(year), season, start_date, end_date,
             json.dumps(list(tags), ensure_ascii=False),
             str(logo_url or ""), str(logo_local or ""),
             json.dumps(dict(links or {}), ensure_ascii=False),
             _series_key,
             int(vol or 0), str(vol_label or ""),
             time.time()),
        )

    # -- 展会系列 -----------------------------------------------------------

    def list_series(self) -> list[dict[str, Any]]:
        """所有展会系列(按 sort_order, 再按名字)."""
        rows = self.query(
            "SELECT * FROM event_series ORDER BY sort_order, name COLLATE NOCASE")
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["links"] = json.loads(d.get("links_json") or "{}")
            except ValueError:
                d["links"] = {}
            if not isinstance(d["links"], dict):
                d["links"] = {}
            d["has_logo"] = bool(d.get("logo_local") or d.get("logo_url"))
            d["has_poster"] = bool(d.get("poster_local") or d.get("poster_url"))
            out.append(d)
        return out

    def get_series(self, name: str) -> dict[str, Any] | None:
        if not str(name or "").strip():
            return None
        row = self.query_one(
            "SELECT * FROM event_series WHERE name = ?", (str(name).strip(),))
        if row is None:
            return None
        d = dict(row)
        try:
            d["links"] = json.loads(d.get("links_json") or "{}")
        except ValueError:
            d["links"] = {}
        if not isinstance(d["links"], dict):
            d["links"] = {}
        d["has_logo"] = bool(d.get("logo_local") or d.get("logo_url"))
        d["has_poster"] = bool(d.get("poster_local") or d.get("poster_url"))
        return d

    def save_series(self, name: str, *, abbr: str = "", full_name: str = "",
                    logo_url: str = "", logo_local: str = "",
                    poster_url: str = "", poster_local: str = "",
                    wiki: str = "", links: Mapping[str, str] | None = None,
                    sort_order: int = 0) -> None:
        """写入/更新一个展会系列(按 name 覆盖)."""
        key = str(name or "").strip()
        if not key:
            return
        now = time.time()
        existing = self.query_one(
            "SELECT created_at FROM event_series WHERE name = ?", (key,))
        created = float(existing["created_at"]) if existing else now
        self.execute(
            "REPLACE INTO event_series(name, abbr, full_name, logo_url,"
            " logo_local, poster_url, poster_local, wiki, links_json,"
            " sort_order, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, str(abbr or ""), str(full_name or ""),
             str(logo_url or ""), str(logo_local or ""),
             str(poster_url or ""), str(poster_local or ""),
             str(wiki or ""),
             json.dumps(dict(links or {}), ensure_ascii=False),
             int(sort_order or 0), created, now),
        )

    def delete_series(self, name: str) -> bool:
        key = str(name or "").strip()
        if not key:
            return False
        self.execute("DELETE FROM event_series WHERE name = ?", (key,))
        # 系列没了, 会次的 series 指向清空(会次本身保留)
        self.execute(
            "UPDATE events SET series = '' WHERE series = ?", (key,))
        return True

    def events_of_series(self, series: str) -> list[dict[str, Any]]:
        """某系列下的全部会次, 按会次号倒序(新的在前)."""
        rows = self.query(
            "SELECT * FROM events WHERE series = ?"
            " ORDER BY vol DESC, name DESC", (str(series or "").strip(),))
        return [self._event_row(r) for r in rows]

    @staticmethod
    def _event_row(row: Any) -> dict[str, Any]:
        d = dict(row)
        # series 在库里是 NULL(= 未归类), 上层统一按空串处理
        if d.get("series") is None:
            d["series"] = ""
        try:
            d["tags"] = json.loads(d.get("tags") or "[]")
        except ValueError:
            d["tags"] = []
        try:
            d["links"] = json.loads(d.get("links_json") or "{}")
        except ValueError:
            d["links"] = {}
        if not isinstance(d["links"], dict):
            d["links"] = {}
        d["has_logo"] = bool(d.get("logo_local") or d.get("logo_url"))
        return d

    def list_events(self) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM events ORDER BY year DESC, name DESC")
        return [self._event_row(r) for r in rows]

    def reset(self, *, keep_matches: bool = False) -> None:
        self.execute("DELETE FROM albums")
        self.execute("DELETE FROM album_sources")
        self.execute("DELETE FROM album_tags")
        if not keep_matches:
            self.execute("DELETE FROM netease_matches")
            # 人工拉黑也是"某张碟"的从属信息: 专辑都没了, 留着只会变成幽灵记录
            # (key 一旦被重新聚合出来就会被莫名挡住)。老快照表一并清掉。
            self.execute("DELETE FROM netease_blocks")
            try:
                self.execute("DELETE FROM netease_rejections")
            except Exception:
                pass

    # -- 删除 ---------------------------------------------------------------

    def delete_albums(self, keys: Sequence[str]) -> int:
        """按 key 批量删除专辑(连同来源映射、标签、标记、X 关联、匹配结果)."""
        cleaned = [str(k) for k in keys if k]
        if not cleaned:
            return 0
        deleted = 0
        for chunk_start in range(0, len(cleaned), 400):
            chunk = cleaned[chunk_start : chunk_start + 400]
            placeholders = ",".join("?" * len(chunk))
            row = self.query_one(
                f"SELECT COUNT(*) AS n FROM albums WHERE key IN ({placeholders})", chunk
            )
            deleted += int(row["n"]) if row else 0
            for table in ("album_sources", "album_tags", "album_marks", "netease_matches",
                          "netease_blocks"):
                self.execute(f"DELETE FROM {table} WHERE album_key IN ({placeholders})", chunk)
            # 老快照表可能缺列/缺表, 单独兜一下
            try:
                self.execute(f"DELETE FROM netease_rejections"
                             f" WHERE album_key IN ({placeholders})", chunk)
            except Exception:
                pass
            self.execute(f"DELETE FROM albums WHERE key IN ({placeholders})", chunk)
        return deleted

    def delete_albums_where(
        self,
        *,
        event: str | None = None,
        tag: str | None = None,
        source: str | None = None,
        query: str | None = None,
        mark: str | None = None,
        only_unmarked: bool = False,
        only_unmatched: bool = False,
        dry_run: bool = False,
    ) -> list[str]:
        """按筛选条件删除, 返回(将)被删除的 key 列表.

        与 :meth:`list_albums` 的筛选语义保持一致, 这样 Web 面板"按当前筛选删除"不会误删。
        """
        keys = [a.key for a in self.list_albums(
            event=event, tag=tag, source=source, query=query,
            mark=mark, only_unmarked=only_unmarked, only_unmatched=only_unmatched,
            limit=None,
        )]
        if not dry_run:
            self.delete_albums(keys)
        return keys

    # -- 修改 ---------------------------------------------------------------

    def set_event(self, key: str, event: str, *, event_kind: str | None = None) -> bool:
        """给专辑重新指定展会(``event=""`` 表示摘掉展会标签)."""
        album = self.get_album(key)
        if album is None:
            return False
        album.event = event or ""
        album.event_kind = event_kind or _kind_of(album.event)
        self._write_album(album, time.time())
        return True

    def create_album(self, album: Album, *, event: str | None = None) -> str:
        """手工新增一张专辑(Web 面板 / CLI 用). 返回写入状态."""
        return self.upsert_album(album, event=event)

    #: 重解析时**强制覆盖**的字段(这些是被解析 bug 弄脏的"元数据"字段)。
    #: 刻意不含 cover_*/tags/marks/matches —— 那些是用户资产或其它源的贡献。
    REPARSE_FIELDS = (
        "title", "title_original", "title_romaji", "circle", "artists",
        "credits", "catalog_no", "release_date", "price", "description",
        "urls", "tracks",
    )

    def replace_from_fresh(self, key: str, fresh: Album, *,
                           fields: Sequence[str] | None = None) -> tuple[bool, str]:
        """用重新解析出来的 ``fresh`` 覆盖 ``key`` 的**元数据字段**.

        与 :meth:`upsert_album` 的合并语义不同: 这里是要**纠正**已经写错的字段
        (比如社团被拼成 "X / X / X"), 所以是"写什么就是什么"。
        但只动 :attr:`REPARSE_FIELDS`, 封面 / 标签 / 标记 / 匹配记录一概保留;
        人工编辑过的专辑也跳过(``edited`` 是用户资产)。
        """
        album = self.get_album(key)
        if album is None:
            return False, "找不到这张专辑"
        if album.edited:
            return False, "人工编辑过, 跳过"
        # 封面单独处理: 只在"当前没有"或"当前是占位图"时才用新封面覆盖,
        # 已有的封面(可能是 bgm.tv 的高清图或用户上传的)不碰。
        fresh_cover = (getattr(fresh, "cover_url", "") or "").strip()
        bad_markers = ("nocover", "/db/icons/", "/db/img/")
        if fresh_cover and not any(m in fresh_cover.lower() for m in bad_markers):
            current_cover = (album.cover_url or "").strip()
            if not current_cover or any(m in current_cover.lower() for m in bad_markers):
                album.cover_url = fresh_cover
                album.cover_local = ""
        elif (album.cover_url or "") and any(
                m in album.cover_url.lower() for m in bad_markers):
            # 当前存的是 UI 图标/占位图, 而新解析也没有真封面 -> **清空**,
            # 宁可显示"无封面", 也不要显示一个按钮图标当封面。
            album.cover_url = ""
            album.cover_local = ""

        for name in (fields or self.REPARSE_FIELDS):
            if not hasattr(fresh, name):
                continue
            value = getattr(fresh, name)
            if name == "tracks":
                if value:
                    album.tracks = list(value)
                continue
            if name == "description":
                # 简介是"多源择优"的字段: 重新解析 vgmdb 时 fresh.description 通常是空的,
                # 直接清空会连 bgm.tv / 特设页写好的简介一起洗掉。
                # 只在这种情况下才清: 当前简介其实就是旧的 VGMdb Notes(被误当简介存进去的)。
                current = (album.description or "").strip()
                legacy_notes = str((album.raw or {}).get("vgmdb_notes") or "").strip()
                if value:
                    album.description = value
                elif current and (current == legacy_notes or _looks_like_vgmdb_notes(current)):
                    album.description = ""
                continue
            if value or name == "urls":
                setattr(album, name, value)
        # 来源信息必须**合并**而不是替换, 否则重解析会把"多源"洗成"单源"
        # (用户反馈过: 重解析后数据源只剩一个)
        album.source_ids = {**fresh.source_ids, **album.source_ids}
        album.trust_by_source = {**(fresh.trust_by_source or {}),
                                 **(album.trust_by_source or {})}
        album.trust = max(int(album.trust or 0), int(fresh.trust or 0))
        album.raw = {**fresh.raw, **album.raw}
        self._write_album(album, time.time())
        # album_sources 表也要同步, 否则界面上的"来源"会缺
        self._link_sources(album)
        return True, ""

    def clean_noise_tags(self, *, dry_run: bool = False) -> dict[str, int]:
        """把 **整库**的噪声标签清掉(``album_tags`` 表 + payload 里的 tags/genres).

        返回 ``{"albums": 处理张数, "removed": 删掉的标签条数, "kinds": 种类数}``。
        """
        removed = 0
        touched = 0
        kinds: set[str] = set()

        # 1) album_tags 表
        for row in self.query("SELECT DISTINCT tag FROM album_tags"):
            tag = str(row["tag"] or "")
            # 这里同时清"噪声"与"无检索意义"两类(展会名/年份/虚拟歌姬名/格式词)
            if is_tag_worth_keeping(tag):
                continue
            kinds.add(tag)
            hit = self.query_one(
                "SELECT COUNT(*) AS n FROM album_tags WHERE tag = ?", (tag,))
            removed += int(hit["n"]) if hit else 0
            if not dry_run:
                self.execute("DELETE FROM album_tags WHERE tag = ?", (tag,))

        # 2) payload 里的 tags / genres
        for row in self.query("SELECT key, payload FROM albums"):
            try:
                album = Album.from_json(row["payload"])
            except (ValueError, TypeError):
                continue
            before = (list(album.tags), list(album.genres))
            album.tags = [t for t in split_tag_values(album.tags) if is_tag_worth_keeping(t)]
            album.genres = [g for g in split_tag_values(album.genres) if not is_noise_genre(g)]
            if (album.tags, album.genres) == before:
                continue
            touched += 1
            if not dry_run:
                self._write_album(album, time.time())
        return {"albums": touched, "removed": removed, "kinds": len(kinds)}

    def update_album_fields(self, key: str, patch: dict[str, Any]) -> tuple[bool, str]:
        """直接改写一张已入库专辑的元数据(Web 面板「编辑信息」/ CLI 用).

        为什么单独开一个方法而不是复用 :meth:`upsert_album`: 后者是**合并**语义
        (空值不覆盖已有值、按来源可信度挑字段), 而人工编辑要的是"写什么就是什么"
        —— 清空一个写错的字段、改掉错字都必须真的落库。

        ``key`` 保持不变: 它是 album_sources / album_tags / album_marks /
        netease_matches 的事实主键, 改标题不应该换身份; 之后再采集到同一条时,
        站点原生 ID 会把它认回来(``upsert_album`` 第一优先级就是原生 ID 命中)。
        """
        album = self.get_album(key)
        if album is None:
            return False, "找不到这张专辑"
        unknown = [k for k in patch if k not in _EDITABLE_FIELDS]
        if unknown:
            return False, "不支持修改的字段: " + ", ".join(sorted(unknown))

        def as_text(value: Any) -> str:
            return str(value).strip() if value is not None else ""

        def as_list(value: Any) -> list[str]:
            if value is None:
                return []
            if isinstance(value, str):
                parts = re.split(r"[,，、\n]", value)
            elif isinstance(value, Iterable):
                parts = [str(v) for v in value]
            else:
                return []
            return [p.strip() for p in parts if p and p.strip()]

        # 注意: 这个列表必须与 _EDITABLE_FIELDS 里"纯文本"那批**保持一致**。
        # 早先白名单加了字段但这里漏了赋值, 结果 ok=True 却读回空值
        # (cover_local / company 都踩过)。
        for field in ("title", "title_original", "title_romaji", "circle", "release_date",
                      "catalog_no", "company", "description", "price",
                      "cover_url", "cover_local", "cover_v",
                      "booth", "hall",
                      # 用户 m16568
                      "original_work", "producer", "platform", "release_kind"):
            if field in patch:
                setattr(album, field, as_text(patch[field]))
        # 碟名(用户 m16568): {碟号: 名字}, 空名字不占位
        if "discs" in patch:
            raw_discs = patch["discs"]
            discs: dict[str, str] = {}
            if isinstance(raw_discs, Mapping):
                for raw_no, raw_name in raw_discs.items():
                    try:
                        number = int(str(raw_no).strip())
                    except (TypeError, ValueError):
                        continue
                    name = as_text(raw_name)
                    if number > 0 and name:
                        discs[str(number)] = name
            album.discs = discs
        if "event" in patch:
            album.event = as_text(patch["event"])
            album.event_kind = _kind_of(album.event)
        # 社团名单(用户 m20229): 合辑可能有两个以上社团, 不能合并成一个名字。
        if "circles" in patch:
            album.circles = as_list(patch["circles"])
        elif "circle" in patch:
            album.circles = [album.circle] if album.circle else []
        if "artists" in patch:
            album.artists = as_list(patch["artists"])
        if "genres" in patch:
            album.genres = as_list(patch["genres"])
        if "tags" in patch:
            album.tags = as_list(patch["tags"])
        if "urls" in patch and isinstance(patch["urls"], dict):
            album.urls = {str(k): str(v) for k, v in patch["urls"].items() if v}
        if "track_list" in patch:
            tracks: list[Track] = []
            for index, item in enumerate(patch["track_list"] or [], 1):
                if isinstance(item, dict):
                    title = as_text(item.get("title"))
                    if not title:
                        continue
                    tracks.append(Track(
                        title=title,
                        # 碟号: 表单里留空 = 1(单碟), 多碟时用户手填
                        disc=int(item.get("disc") or 1),
                        index=int(item.get("index") or index),
                        duration=item.get("duration"),
                        artists=[str(a) for a in (item.get("artists") or []) if str(a).strip()],
                        # 补充字段(编辑界面可填, 详情页有值才展示)
                        subtitle=as_text(item.get("subtitle")),
                        title_zh=as_text(item.get("title_zh")),
                        lang=as_text(item.get("lang")),
                        genre=as_text(item.get("genre")),
                    ))
                else:
                    title = as_text(item)
                    if title:
                        tracks.append(Track(title=title, index=index))
            album.tracks = tracks

        if "credits" in patch:
            # ``credits`` 是 [{role, name}] —— **role 允许是自定义词条名称**
            # (用户要求: 编辑界面要能加自定义词条, 而不是只能用固定的几类)。
            built: list[Credit] = []
            for item in patch["credits"] or []:
                if isinstance(item, dict):
                    name = as_text(item.get("name"))
                    role = as_text(item.get("role")) or "artist"
                else:
                    name = as_text(item)
                    role = "artist"
                if not name:
                    continue
                built.append(Credit(name=name, role=role, source="manual"))
            album.credits = built

        now = time.time()
        # **标记为人工编辑**: 之后自动采集只补空缺, 不再覆盖这里写进去的值。
        # 不加这个标记的话, 你手工改对的标题/社团会在下次采集时被第三方来源改回去。
        album.edited = True
        self._write_album(album, now)
        # _write_album 的标签是"只增不减"(INSERT OR IGNORE) —— 人工删掉的标签
        # 不会消失, 所以这里整组重写。
        self.execute("DELETE FROM album_tags WHERE album_key = ?", (album.key,))
        album.tags = [t for t in split_tag_values(album.tags) if is_tag_worth_keeping(t)]
        album.genres = [g for g in split_tag_values(album.genres) if not is_noise_genre(g)]
        # 曲风与标签会撞车(专辑自带的 tags 里常常就含 genres) —— 以**曲风**为准,
        # 从标签里摘掉重复项, 免得同一个词在界面上出现两遍、筛选时也算两次。
        genre_norm = {textutil.norm_key(g, romaji=False) for g in album.genres}
        album.tags = [t for t in album.tags
                      if textutil.norm_key(t, romaji=False) not in genre_norm]
        # 展会不再进标签: 它是结构化字段(event), 进标签只会重复(实测这类占一半出现次数)
        for tag in album.tags + album.genres:
            tag = textutil.clean_whitespace(tag)
            if tag and is_tag_worth_keeping(tag):
                self.execute("INSERT OR IGNORE INTO album_tags(album_key, tag) VALUES (?,?)",
                             (album.key, tag))
        return True, ""

    def duplicate_groups(self, *, threshold: float = 0.94, limit: int = 200) -> list[dict[str, Any]]:
        """找出库里疑似重复的专辑组(供 ``dddb db dedupe`` 与面板使用).

        分簇用并查集: 只要两条记录判为重复就连成一簇 —— "跑了三轮采集、产生三条"
        会归到同一组, 而不是散成两对。每组的 ``keep`` 是建议保留项: 可信度最高、
        字段最全、标题最短(最不像拼接出来的那一条)。
        """
        albums = self.all_albums()
        by_title: dict[str, list[Album]] = {}
        by_circle: dict[str, list[Album]] = {}
        for item in albums:
            title_key = textutil.norm_key(item.title_original or item.title, romaji=False)
            circle_key = textutil.norm_key(item.circle, romaji=False)
            if title_key:
                by_title.setdefault(title_key, []).append(item)
            if circle_key:
                by_circle.setdefault(circle_key, []).append(item)

        parent: dict[str, str] = {item.key: item.key for item in albums}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(x: str, y: str) -> None:
            root_x, root_y = find(x), find(y)
            if root_x != root_y:
                parent[root_y] = root_x

        checked: set[tuple[str, str]] = set()
        for bucket in (by_title, by_circle):
            for items in bucket.values():
                if len(items) < 2:
                    continue
                for i in range(len(items)):
                    for j in range(i + 1, len(items)):
                        first, second = items[i], items[j]
                        pair = (first.key, second.key) if first.key < second.key else (second.key, first.key)
                        if pair in checked:
                            continue
                        checked.add(pair)
                        if _is_duplicate_pair(first, second, threshold=threshold):
                            union(first.key, second.key)

        clusters: dict[str, list[Album]] = {}
        for item in albums:
            clusters.setdefault(find(item.key), []).append(item)

        def richness(item: Album) -> int:
            return sum(1 for field in ("title_original", "circle", "release_date",
                                       "cover_url", "cover_local", "catalog_no")
                       if getattr(item, field)) + (1 if item.tracks else 0) + len(item.source_ids)

        groups: list[dict[str, Any]] = []
        for members in clusters.values():
            if len(members) < 2:
                continue
            members.sort(key=lambda item: (-int(item.trust or 0), -richness(item), len(item.title)))
            groups.append({
                "keys": [item.key for item in members],
                "keep": members[0].key,
                "title": members[0].display_title,
                "circle": members[0].circle,
                "count": len(members),
                "sources": [sorted(item.sources()) for item in members],
            })
        groups.sort(key=lambda group: (-group["count"], group["title"]))
        return groups[:limit]


    def merge_albums(self, keep_key: str, drop_key: str,
                     *, parts: Iterable[str] | None = None) -> bool:
        """把 ``drop_key`` 并进 ``keep_key``(去重清理用), 然后删掉被并的那条.

        保留项的字段优先; 它缺的字段用被并项补。依赖表(来源/标记/网易云匹配)
        一并迁移, 否则删掉被并项会留下孤儿行。

        ``parts``: **只合并哪些部分**(用户 m13899 要求合并可选, 而不是一律全量)。
        取值见 :data:`MERGE_PARTS`; 传 ``None`` 表示全部(命令行/旧调用方行为不变)。
        """
        keep = self.get_album(keep_key)
        drop = self.get_album(drop_key)
        if keep is None or drop is None or keep_key == drop_key:
            return False
        # MERGE_PARTS 是 (键, 中文名) 元组, 这里要的是**键**的集合 ——
        # 早先写成 set(MERGE_PARTS) 得到的是元组集合, want &= 之后永远为空,
        # 于是勾什么都"合并成功"却什么都没并过来。
        _part_keys = {part_key for part_key, _ in MERGE_PARTS}
        want = set(_part_keys if parts is None else parts) & _part_keys

        if "meta" in want:
            for field in ("title_original", "title_romaji", "circle", "release_date",
                          "catalog_no", "price", "booth", "hall"):
                if not getattr(keep, field) and getattr(drop, field):
                    setattr(keep, field, getattr(drop, field))
            if not keep.event and drop.event:
                keep.event = drop.event
                keep.event_kind = keep.event_kind or drop.event_kind
        if "description" in want and not keep.description and drop.description:
            keep.description = drop.description
        if "cover" in want:
            for field in ("cover_url", "cover_local"):
                if not getattr(keep, field) and getattr(drop, field):
                    setattr(keep, field, getattr(drop, field))
        if "tags" in want:
            keep.artists = list(dict.fromkeys([*keep.artists, *drop.artists]))
            keep.genres = list(dict.fromkeys([*keep.genres, *drop.genres]))
            keep.tags = list(dict.fromkeys([*keep.tags, *drop.tags]))
        if "tracks" in want and not keep.tracks and drop.tracks:
            keep.tracks = list(drop.tracks)
        if "credits" in want:
            # 制作人员: 按 (职责, 规范化名字) 去重后并集 —— 保留项已有的同一职责
            # 同一人不再重复; 被并项独有的职责(比如只在旧条目里补的编曲)会带过来。
            _seen_credits = {(c.role, textutil.norm_key(c.name)) for c in keep.credits}
            for credit in drop.credits:
                stamp = (credit.role, textutil.norm_key(credit.name))
                if stamp in _seen_credits:
                    continue
                _seen_credits.add(stamp)
                keep.credits.append(credit)
        if "photos" in want and drop.photos:
            # 其他照片: 按地址去重后并集(照片是独立文件, 迁移条目不该丢图)。
            _seen_photos = {str(p.get("url") or "") for p in keep.photos
                            if isinstance(p, dict)}
            for photo in drop.photos:
                if not isinstance(photo, dict):
                    continue
                url = str(photo.get("url") or "")
                if url and url in _seen_photos:
                    continue
                if url:
                    _seen_photos.add(url)
                keep.photos.append(dict(photo))
        if "urls" in want:
            keep.urls = {**drop.urls, **keep.urls}
        keep.source_ids = {**drop.source_ids, **keep.source_ids}
        keep.raw = {**drop.raw, **keep.raw}
        keep.trust = max(int(keep.trust or 0), int(drop.trust or 0))
        keep.trust_by_source = {**drop.trust_by_source, **keep.trust_by_source}

        self._write_album(keep, time.time())
        self.execute("UPDATE album_sources SET album_key = ? WHERE album_key = ?", (keep_key, drop_key))
        self.execute(
            "INSERT OR IGNORE INTO album_marks(album_key, mark, note, created_at)"
            " SELECT ?, mark, note, created_at FROM album_marks WHERE album_key = ?",
            (keep_key, drop_key),
        )
        self.execute("DELETE FROM album_marks WHERE album_key = ?", (drop_key,))
        if self.query_one("SELECT 1 FROM netease_matches WHERE album_key = ?", (keep_key,)) is None:
            self.execute("UPDATE netease_matches SET album_key = ? WHERE album_key = ?", (keep_key, drop_key))
        else:
            self.execute("DELETE FROM netease_matches WHERE album_key = ?", (drop_key,))
        self.execute("DELETE FROM album_tags WHERE album_key = ?", (drop_key,))
        self.execute("DELETE FROM albums WHERE key = ?", (drop_key,))
        return True

    def prune_events(
        self,
        *,
        before_days: int = 45,
        after_days: int = 21,
        drop: bool = False,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """清理"发行日与所标展会不符"的专辑.

        ``drop=False`` 只摘掉展会标签(专辑保留), ``drop=True`` 直接删除。
        返回 ``{"checked", "mismatched", "changed", "keys", "samples"}``。
        """
        from . import events as events_mod

        albums = self.all_albums()
        mismatched: list[Album] = []
        for album in albums:
            if not album.event:
                continue
            parsed = events_mod.parse_event(album.event)
            if parsed is None:
                continue
            verdict = events_mod.release_matches_event(
                album.release_date, parsed, before_days=before_days, after_days=after_days
            )
            if verdict is False:
                mismatched.append(album)

        keys = [a.key for a in mismatched]
        samples = [
            {
                "key": a.key,
                "title": a.display_title,
                "event": a.event,
                "release_date": a.release_date,
                "sources": a.sources(),
            }
            for a in mismatched[:50]
        ]
        changed = 0
        if not dry_run and keys:
            if drop:
                changed = self.delete_albums(keys)
            else:
                now = time.time()
                for album in mismatched:
                    album.event = ""
                    album.event_kind = ""
                    self._write_album(album, now)
                    changed += 1
        return {
            "checked": len(albums),
            "mismatched": len(mismatched),
            "changed": changed,
            "dropped": bool(drop),
            "dry_run": dry_run,
            "keys": keys,
            "samples": samples,
        }

    # -- 标记 ---------------------------------------------------------------

    def add_mark(self, key: str, mark: str, note: str = "") -> bool:
        """给专辑打标记(例如「已加入歌单」). 幂等."""
        mark = (mark or "").strip()
        if not key or not mark:
            return False
        self.execute(
            "INSERT OR REPLACE INTO album_marks(album_key, mark, note, created_at) VALUES (?,?,?,?)",
            (key, mark, note or "", time.time()),
        )
        return True

    def remove_mark(self, key: str, mark: str) -> int:
        cur = self.execute("DELETE FROM album_marks WHERE album_key = ? AND mark = ?", (key, (mark or "").strip()))
        return cur.rowcount

    def marks_of(self, key: str) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT mark, note, created_at FROM album_marks WHERE album_key = ? ORDER BY created_at", (key,)
        )
        return [dict(r) for r in rows]

    def mark_counts(self) -> list[tuple[str, int]]:
        rows = self.query(
            "SELECT mark, COUNT(*) AS n FROM album_marks GROUP BY mark ORDER BY n DESC, mark"
        )
        return [(r["mark"], int(r["n"])) for r in rows]

    def marked_keys(self, mark: str | None = None) -> set[str]:
        if mark:
            rows = self.query("SELECT album_key FROM album_marks WHERE mark = ?", (mark,))
        else:
            rows = self.query("SELECT DISTINCT album_key FROM album_marks")
        return {r["album_key"] for r in rows}

    def marks_map(self) -> dict[str, list[str]]:
        """``{album_key: [mark, ...]}`` —— 列表接口一次取回, 避免 N+1 查询."""
        out: dict[str, list[str]] = {}
        for row in self.query("SELECT album_key, mark FROM album_marks ORDER BY created_at"):
            out.setdefault(row["album_key"], []).append(row["mark"])
        return out

    # -- 歌单独有专辑的标记 -------------------------------------------------
    #
    # 「② 歌单里有、本地库没有」的条目还没有本地 key, 只认网易云 album_id。
    # 用户在那里的操作是「这张我已经补进线上库了」, 标完之后它就不该再催 ——
    # 复用 :meth:`add_mark` 是不行的(它按 album_key 存, 而这里还没有 key, 硬写
    # 会在 ``_albums_mark`` 被 ``get_album(key) is None`` 挡掉, 那也是对的:
    # 不给不存在的专辑打标记)。所以按 album_id 单独一张表。

    def add_playlist_mark(self, album_id: int, mark: str, album_name: str = "") -> bool:
        """给「歌单里有、本地库没有」的专辑按 album_id 打标记. 幂等."""
        mark = (mark or "").strip()
        try:
            album_id = int(album_id or 0)
        except (TypeError, ValueError):
            return False
        if album_id <= 0 or not mark:
            return False
        self.execute(
            "INSERT OR REPLACE INTO playlist_album_marks"
            "(album_id, mark, album_name, created_at) VALUES (?,?,?,?)",
            (album_id, mark, str(album_name or ""), time.time()),
        )
        return True

    def remove_playlist_mark(self, album_id: int, mark: str) -> int:
        try:
            album_id = int(album_id or 0)
        except (TypeError, ValueError):
            return 0
        cur = self.execute(
            "DELETE FROM playlist_album_marks WHERE album_id = ? AND mark = ?",
            (album_id, (mark or "").strip()),
        )
        return cur.rowcount

    def playlist_marks_map(self) -> dict[int, list[str]]:
        """``{album_id: [mark, ...]}`` —— 歌单对照一次取回, 避免 N+1."""
        out: dict[int, list[str]] = {}
        for row in self.query(
            "SELECT album_id, mark FROM playlist_album_marks ORDER BY created_at"
        ):
            out.setdefault(int(row["album_id"] or 0), []).append(row["mark"])
        out.pop(0, None)
        return out

    def playlist_marked_ids(self, mark: str | None = None) -> set[int]:
        if mark:
            rows = self.query(
                "SELECT album_id FROM playlist_album_marks WHERE mark = ?", (mark,)
            )
        else:
            rows = self.query("SELECT DISTINCT album_id FROM playlist_album_marks")
        return {int(r["album_id"] or 0) for r in rows if int(r["album_id"] or 0) > 0}

    # -- 公开端评论 ---------------------------------------------------------
    #
    # 约定(后续波次的「纠错反馈 / 站点反馈」会沿用同一套写法):
    #
    # * **写方法只做数据层的事**: 参数校验 + SQL, 不碰 HTTP、不碰会话;
    # * 返回 ``(ok, message, ...)`` 形式, 失败原因是给用户看的中文, 不抛异常;
    # * 隐藏 = 软删除(``status='hidden'``), 物理删除只留给「作者删自己的」;
    # * 所有时间戳由调用方传 ``now``(单测可复现), 默认取当前时间。

    @staticmethod
    def _comment_dict(row: sqlite3.Row) -> dict[str, Any]:
        """评论行 -> 普通字典(模板/测试都可直接用)."""
        status = str(row["status"] or "visible")
        return {
            "id": int(row["id"]),
            "target_kind": str(row["target_kind"] or ""),
            "target_key": str(row["target_key"] or ""),
            "user_id": int(row["user_id"] or 0),
            "username": str(row["username"] or ""),
            "body": str(row["body"] or ""),
            "created_at": float(row["created_at"] or 0.0),
            "status": status,
            "hidden": status != "visible",
            "hidden_by": str(row["hidden_by"] or ""),
            "hidden_at": float(row["hidden_at"] or 0.0),
            "ip_hash": str(row["ip_hash"] or ""),
        }

    @staticmethod
    def _comment_target(target_kind: Any, target_key: Any) -> tuple[str, str, str]:
        """校验 ``(target_kind, target_key)``; 返回 ``(kind, key, 错误消息)``."""
        kind = str(target_kind or "").strip().lower()
        key = str(target_key or "").strip()
        if kind not in COMMENT_TARGET_KINDS:
            return "", "", "评论对象类型不合法"
        if not key:
            return kind, "", "评论对象不能为空"
        if len(key) > 300:
            return kind, "", "评论对象标识过长"
        return kind, key, ""

    # -- 编辑历史(版本) ---------------------------------------------------

    def add_revision(self, album_key: Any, *, user_id: Any = 0, username: Any = "",
                     summary: Any = "", payload: Any = None,
                     now: float | None = None) -> int:
        """记录一次修订(存**完整 payload** 快照). 返回修订 id; 失败返回 0."""
        key = str(album_key or "").strip()
        if not key:
            return 0
        if payload is None:
            album = self.get_album(key)
            if album is None:
                return 0
            payload = album.to_json() if hasattr(album, "to_json") else album
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        stamp = time.time() if now is None else float(now)
        cursor = self.execute(
            "INSERT INTO album_revisions(album_key, user_id, username, summary, payload, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (key, int(user_id or 0), str(username or "")[:64], str(summary or "")[:200],
             text, stamp),
        )
        return int(getattr(cursor, "lastrowid", 0) or 0)

    def list_revisions(self, album_key: Any, *, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT id, album_key, user_id, username, summary, created_at"
            " FROM album_revisions WHERE album_key = ? ORDER BY id DESC LIMIT ?",
            (str(album_key or ""), max(1, int(limit))),
        )
        return [dict(row) for row in rows]

    def get_revision(self, revision_id: Any) -> dict[str, Any] | None:
        try:
            rid = int(revision_id or 0)
        except (TypeError, ValueError):
            return None
        rows = self.query("SELECT * FROM album_revisions WHERE id = ?", (rid,))
        return dict(rows[0]) if rows else None

    def revert_to_revision(self, revision_id: Any, *, user_id: Any = 0,
                           username: Any = "", now: float | None = None) -> tuple[bool, str]:
        """把专辑**整体还原**到某个修订版本(会产生一条新的修订记录)."""
        rev = self.get_revision(revision_id)
        if rev is None:
            return False, "没有这个修订版本"
        key = str(rev.get("album_key") or "")
        try:
            album = Album.from_json(rev.get("payload") or "")
        except (ValueError, TypeError) as exc:
            return False, f"修订内容损坏: {exc}"
        self._write_album(album, time.time() if now is None else float(now))
        self.add_revision(key, user_id=user_id, username=username,
                          summary=f"还原到修订 #{int(revision_id)}", now=now)
        return True, ""

    def restore_field_from_revision(self, revision_id: Any, field: Any, *,
                                    user_id: Any = 0, username: Any = "",
                                    now: float | None = None) -> tuple[bool, str]:
        """只把**某个字段**还原成该修订里的值(萌娘百科式"还原此字段")."""
        rev = self.get_revision(revision_id)
        if rev is None:
            return False, "没有这个修订版本"
        name = str(field or "").strip()
        if name not in _EDITABLE_FIELDS:
            return False, f"不支持还原的字段: {name}"
        key = str(rev.get("album_key") or "")
        current = self.get_album(key)
        if current is None:
            return False, "找不到这张专辑"
        try:
            snapshot = Album.from_json(rev.get("payload") or "")
        except (ValueError, TypeError) as exc:
            return False, f"修订内容损坏: {exc}"
        value = getattr(snapshot, name, None)
        ok, message = self.update_album_fields(key, {name: value})
        if not ok:
            return False, message
        self.add_revision(key, user_id=user_id, username=username,
                          summary=f"字段 {name} 还原自修订 #{int(revision_id)}", now=now)
        return True, ""

    def add_comment(
        self,
        *,
        target_kind: Any,
        target_key: Any,
        user_id: Any,
        username: Any,
        body: Any,
        ip_hash: Any = "",
        max_length: int = COMMENT_BODY_LIMIT,
        now: float | None = None,
    ) -> tuple[bool, str, int]:
        """发表一条评论. 返回 ``(成功?, 原因, 评论 id)``."""
        kind, key, problem = self._comment_target(target_kind, target_key)
        if problem:
            return False, problem, 0
        try:
            uid = int(user_id or 0)
        except (TypeError, ValueError):
            uid = 0
        name = str(username or "").strip()[:COMMENT_USERNAME_LIMIT]
        if uid <= 0:
            return False, "请先登录后再发表评论", 0
        if not name:
            return False, "缺少评论人署名", 0
        problem = comment_body_problem(body, limit=max_length)
        if problem:
            return False, problem, 0
        text = clean_comment_body(body)
        stamp = time.time() if now is None else float(now)
        cursor = self.execute(
            "INSERT INTO comments(target_kind, target_key, user_id, username, body,"
            " created_at, status, hidden_by, hidden_at, ip_hash)"
            " VALUES (?,?,?,?,?,?,'visible','',0,?)",
            (kind, key, uid, name, text, stamp, str(ip_hash or "")[:64]),
        )
        try:
            comment_id = int(cursor.lastrowid or 0)
        except (TypeError, ValueError):  # pragma: no cover - 防御性
            comment_id = 0
        return True, "", comment_id


    def make_album_key(self, title: Any, circle: Any = "", date: Any = "") -> str:
        """按 标题|社团|年份 生成不冲突的专辑 key(已存在则加序号)."""
        import re as _re

        def slug(text: Any) -> str:
            value = _re.sub(r"\s+", "-", str(text or "").strip().lower())
            value = _re.sub(r"[|/\\]+", "-", value)
            return value.strip("-")[:80]

        year = ""
        match = _re.search(r"(\d{4})", str(date or ""))
        if match:
            year = match.group(1)
        key = "|".join(p for p in (slug(title), slug(circle), year) if p) or "new-album"
        candidate, index = key, 2
        while self.get_album(candidate) is not None:
            candidate = f"{key}-{index}"
            index += 1
        return candidate

    def find_duplicate_of(self, title: Any, circle: Any) -> str:
        """按 归一化标题+社团 找已有专辑, 返回其 key(没有返回空串)."""
        want_title = self._norm_dup(title)
        want_circle = self._norm_dup(circle)
        if not want_title:
            return ""
        for row in self.query("SELECT key, title, circle FROM albums"):
            if (self._norm_dup(row["title"]) == want_title
                    and self._norm_dup(row["circle"]) == want_circle):
                return str(row["key"])
        return ""

    # -- 重复专辑检测与合并 -----------------------------------------------

    @staticmethod
    def _norm_dup(text: Any) -> str:
        """判重用的归一化: 去空白/标点/大小写."""
        import re as _re
        value = str(text or "").lower()
        return _re.sub(r"[\s\-_·・.,，、!！?？'\"()（）\[\]【】]+", "", value)

    def find_duplicate_albums(self) -> list[dict[str, Any]]:
        """找出疑似重复的专辑组(按 标题+社团 归一化, 或同一外部来源 ID)."""
        groups: dict[str, list[str]] = {}
        by_source: dict[str, list[str]] = {}
        rows = self.query("SELECT key, title, circle, payload FROM albums")
        meta: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = str(row["key"])
            meta[key] = {"title": str(row["title"] or ""), "circle": str(row["circle"] or "")}
            signature = "title|" + self._norm_dup(row["title"]) + "|" + self._norm_dup(row["circle"])
            groups.setdefault(signature, []).append(key)
            try:
                data = json.loads(row["payload"] or "{}")
            except ValueError:
                continue
            for source, external in (data.get("source_ids") or {}).items():
                if external:
                    by_source.setdefault(f"{source}:{external}", []).append(key)
        out: list[dict[str, Any]] = []
        for signature, keys in groups.items():
            if len(keys) > 1:
                out.append({"reason": "标题与社团相同", "keys": keys,
                            "albums": [{"key": k, **meta.get(k, {})} for k in keys]})
        seen = {tuple(sorted(item["keys"])) for item in out}
        for source, keys in by_source.items():
            uniq = sorted(set(keys))
            if len(uniq) > 1 and tuple(uniq) not in seen:
                out.append({"reason": f"同一来源 {source}", "keys": uniq,
                            "albums": [{"key": k, **meta.get(k, {})} for k in uniq]})
        return out

    def merge_duplicate_albums(self, keep_key: str, drop_keys: Sequence[Any], *,
                               apply: bool = False) -> dict[str, Any]:
        """合并重复专辑: 保留 keep_key, 把 drop 里有用的字段补到它上面再删除.

        合并策略: **填空不覆盖** —— 保留项为空的字段用被删项补齐; 曲目更多的一方留下。
        """
        keep = str(keep_key or "")
        drops = [str(k) for k in drop_keys if str(k) and str(k) != keep]
        if not keep or not drops:
            return {"ok": False, "reason": "参数不完整"}
        keep_row = self.query_one("SELECT payload FROM albums WHERE key = ?", (keep,))
        if keep_row is None:
            return {"ok": False, "reason": "保留项不存在"}
        try:
            target = json.loads(keep_row["payload"] or "{}")
        except ValueError:
            target = {}
        track_source = keep
        for drop in drops:
            row = self.query_one("SELECT payload FROM albums WHERE key = ?", (drop,))
            if row is None:
                continue
            try:
                other = json.loads(row["payload"] or "{}")
            except ValueError:
                continue
            for field, value in other.items():
                if field in ("key", "source_ids", "ids"):
                    continue
                if not target.get(field) and value:
                    target[field] = value
            if len(other.get("tracks") or []) > len(target.get("tracks") or []):
                target["tracks"] = other.get("tracks") or []
                track_source = drop
            merged_ids = dict(target.get("source_ids") or {})
            merged_ids.update(other.get("source_ids") or {})
            if merged_ids:
                target["source_ids"] = merged_ids
        result = {"ok": True, "keep": keep, "dropped": drops,
                  "tracks_from": track_source, "applied": bool(apply)}
        if not apply:
            return result
        for drop in drops:
            for table, column in (("album_artists", "album_key"),
                                  ("album_circles", "album_key"),
                                  ("album_events", "album_key"),
                                  ("album_tag_ids", "album_key")):
                try:
                    self.execute(f"DELETE FROM {table} WHERE {column} = ?", (drop,))
                except Exception:            # noqa: BLE001
                    pass
            for table in ("comments", "album_revisions"):
                try:
                    self.execute(f"DELETE FROM {table} WHERE target_key = ?", (drop,))
                except Exception:            # noqa: BLE001
                    pass
            self.execute("DELETE FROM albums WHERE key = ?", (drop,))
        self.execute(
            "UPDATE albums SET payload = ?, updated_at = ? WHERE key = ?",
            (json.dumps(target, ensure_ascii=False, sort_keys=True), time.time(), keep))
        return result

    # -- 实体 ID(专辑/社团/艺人/展会/标签统一编号) -------------------------

    ENTITY_KINDS = ("album", "circle", "artist", "event", "tag")

    def entity_id(self, kind: Any, name: Any) -> int:
        kind_text = str(kind or "").strip().lower()
        title = str(name or "").strip()
        if kind_text not in self.ENTITY_KINDS or not title:
            return 0
        row = self.query_one(
            "SELECT id FROM entities WHERE kind = ? AND name = ? COLLATE NOCASE",
            (kind_text, title))
        return int(row["id"]) if row is not None else 0

    def entity_name(self, kind: Any, entity_id: Any) -> str:
        kind_text = str(kind or "").strip().lower()
        try:
            number = int(entity_id or 0)
        except (TypeError, ValueError):
            return ""
        if number <= 0:
            return ""
        row = self.query_one(
            "SELECT name FROM entities WHERE kind = ? AND id = ?",
            (kind_text, number))
        return str(row["name"]) if row is not None else ""

    def ensure_entity(self, kind: Any, name: Any) -> int:
        """取 id, 没有就分配下一个序号."""
        kind_text = str(kind or "").strip().lower()
        title = str(name or "").strip()
        if kind_text not in self.ENTITY_KINDS or not title:
            return 0
        found = self.entity_id(kind_text, title)
        if found:
            return found
        row = self.query_one(
            "SELECT COALESCE(MAX(id), 0) AS m FROM entities WHERE kind = ?", (kind_text,))
        next_id = int(row["m"] or 0) + 1
        try:
            self.execute("INSERT INTO entities(kind, id, name) VALUES (?,?,?)",
                         (kind_text, next_id, title))
        except Exception:
            return self.entity_id(kind_text, title)
        return next_id

    def resolve_entity(self, kind: Any, value: Any) -> tuple[int, str]:
        """把"名称 / 编号 / #编号 / kindId:编号"解析成 ``(id, 正式名称)``."""
        kind_text = str(kind or "").strip().lower()
        text = str(value or "").strip()
        if not text:
            return 0, ""
        number = 0
        if text.isdigit():
            number = int(text)
        elif text.startswith("#") and text[1:].strip().isdigit():
            number = int(text[1:].strip())
        else:
            prefix = f"{kind_text}id:"
            lowered = text.lower()
            if lowered.startswith(prefix) and lowered[len(prefix):].strip().isdigit():
                number = int(lowered[len(prefix):].strip())
        if number:
            name = self.entity_name(kind_text, number)
            return (number, name) if name else (0, "")
        found = self.entity_id(kind_text, text)
        return found, (str(text) if found else "")

    def entity_list(self, kind: Any, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        kind_text = str(kind or "").strip().lower()
        if kind_text not in self.ENTITY_KINDS:
            return []
        needle = f"%{str(query or '').strip()}%"
        rows = self.query(
            "SELECT id, name FROM entities WHERE kind = ? AND name LIKE ?"
            " ORDER BY id LIMIT ?", (kind_text, needle, int(limit)))
        return [{"id": int(r["id"]), "name": str(r["name"])} for r in rows]

    def _link_rows(self, table: str, column: str, album_key: str, ids: list[int]) -> None:
        self.execute(f"DELETE FROM {table} WHERE album_key = ?", (album_key,))
        for entity_id in ids:
            self.execute(
                f"INSERT OR IGNORE INTO {table}(album_key, {column}) VALUES (?,?)",
                (album_key, int(entity_id)))

    def link_album_entities(self, album_key: str, *,
                            circle: str = "", artists: Sequence[str] = (),
                            event: str = "", tags: Sequence[str] = ()) -> dict[str, Any]:
        """按名称写入专辑的实体关联, 并把 id 列表返回(供 payload.ids 用)."""
        key = str(album_key or "")
        if not key:
            return {}
        circle_id = self.ensure_entity("circle", circle) if circle else 0
        event_id = self.ensure_entity("event", event) if event else 0
        artist_ids = [i for i in (self.ensure_entity("artist", a) for a in artists) if i]
        tag_ids = [i for i in (self.ensure_entity("tag", t) for t in tags) if i]
        if circle_id:
            self._link_rows("album_circles", "circle_id", key, [circle_id])
        else:
            self.execute("DELETE FROM album_circles WHERE album_key = ?", (key,))
        if event_id:
            self._link_rows("album_events", "event_id", key, [event_id])
        else:
            self.execute("DELETE FROM album_events WHERE album_key = ?", (key,))
        self._link_rows("album_artists", "artist_id", key, artist_ids)
        self._link_rows("album_tag_ids", "tag_id", key, tag_ids)
        album_entity = self.ensure_entity("album", key)
        result = {"album_id": album_entity, "circle_id": circle_id,
                  "artist_ids": artist_ids, "event_id": event_id, "tag_ids": tag_ids}
        # 把绑定关系以 id 形式嵌进 payload(名称仍保留用于显示)
        try:
            row = self.query_one("SELECT payload FROM albums WHERE key = ?", (key,))
            if row is not None:
                data = json.loads(row["payload"] or "{}")
                data["ids"] = result
                self.execute(
                    "UPDATE albums SET payload = ? WHERE key = ?",
                    (json.dumps(data, ensure_ascii=False, sort_keys=True), key))
        except (ValueError, TypeError):
            pass
        return result

    def build_entity_ids(self) -> dict[str, int]:
        """给库里已有的主要数据分配 id(幂等), 并重建专辑关联表."""
        counts: dict[str, int] = {k: 0 for k in self.ENTITY_KINDS}
        for row in self.query("SELECT key, payload FROM albums ORDER BY key"):
            counts["album"] += 1
            self.ensure_entity("album", str(row["key"]))
            try:
                data = json.loads(row["payload"] or "{}")
            except ValueError:
                continue
            circle = str(data.get("circle") or "")
            event = str(data.get("event") or "")
            artists = [str(a) for a in data.get("artists") or [] if str(a).strip()]
            tags = [str(t) for t in data.get("tags") or [] if str(t).strip()]
            if circle:
                counts["circle"] += 1
                self.ensure_entity("circle", circle)
            if event:
                counts["event"] += 1
                self.ensure_entity("event", event)
            for artist in artists:
                self.ensure_entity("artist", artist)
            for tag in tags:
                self.ensure_entity("tag", tag)
            self.link_album_entities(str(row["key"]), circle=circle, artists=artists,
                                     event=event, tags=tags)
        for row in self.query("SELECT name FROM circles"):
            name = str(row["name"] or "")
            if name:
                counts["circle"] += 1
                self.ensure_entity("circle", name)
        for row in self.query("SELECT name FROM tags"):
            name = str(row["name"] or "")
            if name:
                self.ensure_entity("tag", name)
        for row in self.query("SELECT name FROM events"):
            name = str(row["name"] or "")
            if name:
                self.ensure_entity("event", name)
        for row in self.query("SELECT name FROM artists"):
            name = str(row["name"] or "")
            if name:
                self.ensure_entity("artist", name)
        return counts

    # -- 标签(预置/说明) ---------------------------------------------------

    def save_tag(self, name: Any, *, kind: str = "", note: str = "",
                 now: float | None = None) -> tuple[bool, str]:
        title = str(name or "").strip()
        if not title:
            return False, "标签名不能为空"
        stamp = time.time() if now is None else float(now)
        self.execute(
            "INSERT INTO tags(name, kind, note, created_at, updated_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET"
            " kind=excluded.kind, note=excluded.note, updated_at=excluded.updated_at",
            (title, str(kind or "").strip(), str(note or "").strip(), stamp, stamp))
        return True, ""

    def list_tags(self) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM tags ORDER BY name COLLATE NOCASE")
        return [{"name": str(r["name"]), "kind": str(r["kind"] or ""),
                 "note": str(r["note"] or "")} for r in rows]

    # -- 艺术家档案(自动聚合之外的"正式字段"覆盖层) -----------------------

    def get_artist(self, name: Any) -> dict[str, Any] | None:
        row = self.query_one(
            "SELECT * FROM artists WHERE name = ? COLLATE NOCASE",
            (str(name or "").strip(),))
        if row is None:
            return None
        try:
            payload = json.loads(row["payload"] or "{}")
        except ValueError:
            payload = {}
        payload.setdefault("name", str(row["name"]))
        payload["updated_at"] = float(row["updated_at"] or 0)
        return payload

    def upsert_artist(self, name: Any, payload: Mapping[str, Any],
                      *, now: float | None = None) -> tuple[bool, str]:
        title = str(name or "").strip()
        if not title:
            return False, "艺名不能为空"
        stamp = time.time() if now is None else float(now)
        body = dict(payload or {})
        body["name"] = title
        try:
            blob = json.dumps(body, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError) as exc:
            return False, f"资料无法序列化: {exc}"
        self.execute(
            "INSERT INTO artists(name, payload, created_at, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET payload=excluded.payload,"
            " updated_at=excluded.updated_at",
            (title, blob, stamp, stamp))
        return True, ""

    def list_artists(self) -> list[dict[str, Any]]:
        rows = self.query("SELECT name, payload, updated_at FROM artists"
                          " ORDER BY name COLLATE NOCASE")
        out = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except ValueError:
                payload = {}
            payload.setdefault("name", str(row["name"]))
            payload["updated_at"] = float(row["updated_at"] or 0)
            out.append(payload)
        return out

    # -- 评论点赞 ---------------------------------------------------------

    def toggle_comment_like(self, comment_id: Any, username: Any) -> tuple[bool, bool, int, str]:
        """切换点赞. 返回 ``(成功?, 现在是否已赞, 点赞数, 原因)``; **不能给自己点赞**."""
        try:
            cid = int(comment_id or 0)
        except (TypeError, ValueError):
            return False, False, 0, "评论编号不对"
        name = str(username or "").strip()
        if not name:
            return False, False, 0, "请先登录"
        row = self.query_one("SELECT id, username FROM comments WHERE id = ?", (cid,))
        if row is None:
            return False, False, 0, "评论不存在"
        if str(row["username"] or "").lower() == name.lower():
            return False, False, 0, "不能给自己的评论点赞"
        existing = self.query_one(
            "SELECT 1 FROM comment_likes WHERE comment_id = ? AND username = ? COLLATE NOCASE",
            (cid, name))
        if existing is not None:
            self.execute("DELETE FROM comment_likes WHERE comment_id = ? AND username = ? COLLATE NOCASE",
                          (cid, name))
            liked = False
        else:
            self.execute(
                "INSERT OR REPLACE INTO comment_likes(comment_id, username, created_at)"
                " VALUES (?, ?, ?)", (cid, name, time.time()))
            liked = True
        count = int(self.query_one(
            "SELECT COUNT(*) AS c FROM comment_likes WHERE comment_id = ?", (cid,))["c"])
        return True, liked, count, ""

    def comment_like_counts(self, comment_ids: Sequence[Any]) -> dict[int, int]:
        ids = [int(i) for i in comment_ids if str(i).isdigit()]
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = self.query(
            f"SELECT comment_id, COUNT(*) AS c FROM comment_likes"
            f" WHERE comment_id IN ({marks}) GROUP BY comment_id", tuple(ids))
        return {int(r["comment_id"]): int(r["c"]) for r in rows}

    def liked_comment_ids(self, username: Any, comment_ids: Sequence[Any]) -> set[int]:
        ids = [int(i) for i in comment_ids if str(i).isdigit()]
        name = str(username or "").strip()
        if not ids or not name:
            return set()
        marks = ",".join("?" for _ in ids)
        rows = self.query(
            f"SELECT comment_id FROM comment_likes WHERE username = ? COLLATE NOCASE"
            f" AND comment_id IN ({marks})", (name, *ids))
        return {int(r["comment_id"]) for r in rows}

    def get_comment(self, comment_id: Any) -> dict[str, Any] | None:
        try:
            cid = int(comment_id)
        except (TypeError, ValueError):
            return None
        row = self.query_one("SELECT * FROM comments WHERE id = ?", (cid,))
        return self._comment_dict(row) if row is not None else None

    def list_comments(
        self,
        target_kind: Any,
        target_key: Any,
        *,
        limit: int = 20,
        offset: int = 0,
        include_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        """按时间倒序取评论. ``include_hidden`` 只给编辑者用."""
        kind, key, problem = self._comment_target(target_kind, target_key)
        if problem:
            return []
        take = max(1, min(int(limit or 20), 200))
        start = max(0, int(offset or 0))
        sql = "SELECT * FROM comments WHERE target_kind = ? AND target_key = ?"
        params: list[Any] = [kind, key]
        if not include_hidden:
            sql += " AND status = 'visible'"
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend([take, start])
        return [self._comment_dict(row) for row in self.query(sql, params)]

    def count_comments(
        self,
        target_kind: Any,
        target_key: Any,
        *,
        include_hidden: bool = False,
    ) -> int:
        kind, key, problem = self._comment_target(target_kind, target_key)
        if problem:
            return 0
        sql = "SELECT COUNT(*) AS n FROM comments WHERE target_kind = ? AND target_key = ?"
        params: list[Any] = [kind, key]
        if not include_hidden:
            sql += " AND status = 'visible'"
        row = self.query_one(sql, params)
        return int(row["n"]) if row is not None else 0

    def hide_comment(self, comment_id: Any, *, hidden_by: str = "", now: float | None = None) -> bool:
        """编辑者隐藏评论(软删除: 行保留, 可恢复). 评论不存在返回 ``False``."""
        try:
            cid = int(comment_id)
        except (TypeError, ValueError):
            return False
        if self.query_one("SELECT id FROM comments WHERE id = ?", (cid,)) is None:
            return False
        stamp = time.time() if now is None else float(now)
        self.execute(
            "UPDATE comments SET status = 'hidden', hidden_by = ?, hidden_at = ? WHERE id = ?",
            (str(hidden_by or "")[:COMMENT_USERNAME_LIMIT], stamp, cid),
        )
        return True

    def restore_comment(self, comment_id: Any, *, hidden_by: str = "") -> bool:
        """恢复被隐藏的评论(``hidden_by`` 只是记录最后操作人, 可留空)."""
        try:
            cid = int(comment_id)
        except (TypeError, ValueError):
            return False
        if self.query_one("SELECT id FROM comments WHERE id = ?", (cid,)) is None:
            return False
        self.execute(
            "UPDATE comments SET status = 'visible', hidden_by = '', hidden_at = 0 WHERE id = ?",
            (cid,),
        )
        return True

    def delete_own_comment(self, comment_id: Any, *, user_id: Any) -> bool:
        """作者删除自己的评论(物理删除). 不是作者 / 不存在 -> ``False``."""
        try:
            cid = int(comment_id)
            uid = int(user_id or 0)
        except (TypeError, ValueError):
            return False
        if cid <= 0 or uid <= 0:
            return False
        cursor = self.execute(
            "DELETE FROM comments WHERE id = ? AND user_id = ?", (cid, uid)
        )
        return bool(cursor.rowcount)

    def delete_comment(self, comment_id: Any) -> bool:
        """无条件删除一条评论(**站方维护用**, 用户端不暴露这个能力)."""
        try:
            cid = int(comment_id)
        except (TypeError, ValueError):
            return False
        cursor = self.execute("DELETE FROM comments WHERE id = ?", (cid,))
        return bool(cursor.rowcount)

    def recent_duplicate_comment(
        self,
        *,
        user_id: Any,
        body: Any,
        target_kind: Any = "album",
        target_key: Any = "",
        since: float = 60.0,
        now: float | None = None,
    ) -> dict[str, Any] | None:
        """同一用户 ``since`` 秒内发过**同一段内容** -> 返回那条(去重用)."""
        try:
            uid = int(user_id or 0)
        except (TypeError, ValueError):
            return None
        text = clean_comment_body(body)
        if uid <= 0 or not text:
            return None
        stamp = time.time() if now is None else float(now)
        since_stamp = stamp - max(1.0, float(since))
        kind, key, problem = self._comment_target(target_kind, target_key)
        sql = (
            "SELECT * FROM comments WHERE user_id = ? AND body = ? AND status = 'visible'"
            " AND created_at >= ?"
        )
        params: list[Any] = [uid, text, since_stamp]
        if not problem:
            sql += " AND target_kind = ? AND target_key = ?"
            params.extend([kind, key])
        sql += " ORDER BY created_at DESC, id DESC LIMIT 1"
        row = self.query_one(sql, params)
        return self._comment_dict(row) if row is not None else None

    def recent_comments_count(
        self,
        *,
        user_id: Any = None,
        ip_hash: Any = "",
        since: float = 60.0,
        now: float | None = None,
    ) -> int:
        """最近 ``since`` 秒内的评论数(按用户或按来源), 限速的库级兜底."""
        stamp = time.time() if now is None else float(now)
        clauses = ["created_at >= ?"]
        params: list[Any] = [stamp - max(1.0, float(since))]
        if user_id not in (None, ""):
            try:
                clauses.append("user_id = ?")
                params.append(int(user_id or 0))
            except (TypeError, ValueError):
                return 0
        elif ip_hash:
            clauses.append("ip_hash = ?")
            params.append(str(ip_hash)[:64])
        else:
            return 0
        row = self.query_one(
            f"SELECT COUNT(*) AS n FROM comments WHERE {' AND '.join(clauses)}", params
        )
        return int(row["n"]) if row is not None else 0

    def comment_counts(self) -> dict[str, int]:
        """``{visible, hidden, total}`` —— 工作队列/报告用."""
        out = {"visible": 0, "hidden": 0, "total": 0}
        for row in self.query("SELECT status, COUNT(*) AS n FROM comments GROUP BY status"):
            status = str(row["status"] or "visible")
            count = int(row["n"])
            out[status] = count
            out["total"] += count
        return out


    def _migrate_events_series_fk(self) -> None:
        """给老库的 ``events.series`` 补外键(ON UPDATE CASCADE).

        SQLite 不能 ``ALTER TABLE ADD CONSTRAINT``, 只能重建表:
        建新表 -> 拷数据 -> 删旧表 -> 改名 -> 重建索引。

        两个坑:
            * 旧表的 ``series`` 是 ``NOT NULL DEFAULT ''`` —— **不能**
              先 ``UPDATE ... SET NULL``(会 IntegrityError), 要在拷数据
              时用 ``CASE`` 就地转换;
            * 期间必须 ``PRAGMA foreign_keys = OFF``, 否则拷数据时
              旧行引用孤儿系列会直接报错。
        """
        ddl = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()
        if ddl is None:
            return                                   # 表还没有, 建表语句会带上
        if "REFERENCES event_series" in str(ddl[0] or ""):
            return                                   # 已经有外键, 幂等

        self._conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self._conn.executescript("""
            CREATE TABLE events_fk_new (
                name       TEXT PRIMARY KEY,
                kind       TEXT NOT NULL DEFAULT '',
                year       INTEGER NOT NULL DEFAULT 0,
                season     TEXT NOT NULL DEFAULT '',
                start_date TEXT NOT NULL DEFAULT '',
                end_date   TEXT NOT NULL DEFAULT '',
                tags       TEXT NOT NULL DEFAULT '[]',
                logo_url   TEXT NOT NULL DEFAULT '',
                logo_local TEXT NOT NULL DEFAULT '',
                links_json TEXT NOT NULL DEFAULT '{}',
                series     TEXT DEFAULT NULL
                           REFERENCES event_series(name)
                           ON UPDATE CASCADE ON DELETE SET NULL,
                vol        INTEGER NOT NULL DEFAULT 0,
                vol_label  TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            );
            INSERT INTO events_fk_new
                (name, kind, year, season, start_date, end_date, tags,
                 logo_url, logo_local, links_json, series, vol, vol_label,
                 updated_at)
            SELECT name, kind, year, season, start_date, end_date, tags,
                   logo_url, logo_local, links_json,
                   CASE
                     WHEN series IS NULL OR series = '' THEN NULL
                     WHEN series NOT IN (SELECT name FROM event_series)
                       THEN NULL
                     ELSE series
                   END,
                   vol, vol_label, updated_at
            FROM events;
            DROP TABLE events;
            ALTER TABLE events_fk_new RENAME TO events;
            CREATE INDEX IF NOT EXISTS idx_events_series ON events(series);
            """)
            bad = self._conn.execute("PRAGMA foreign_key_check").fetchall()
            if bad:
                logger.warning("events.series 外键检查异常: %s", bad)
            self._conn.commit()
        finally:
            self._conn.execute("PRAGMA foreign_keys = ON")

