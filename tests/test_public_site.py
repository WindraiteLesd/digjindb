"""公开站点测试: 路由 / 转义 / 分页 / 筛选 / 模板纯函数 / 只读查询 / HTTP 集成.

分四层覆盖:

1. 模板与工具函数(纯函数, 不碰数据库) —— 转义、URL 白名单、分页边界、筛选 chip;
2. 路由与参数解析(纯函数);
3. :class:`dnh.public.data.PublicData` 对一个**真实临时 SQLite 库**的只读查询;
4. 真起 HTTP 服务(端口 0)用 ``urlopen`` 打所有页面 —— 顺带验证写方法被拒、
   目录穿越被拒、安全响应头存在。

测试库用 :func:`tests._tmp.temp_dir` 建在仓库内(Windows 沙箱下系统 TEMP 不可写)。
"""

from __future__ import annotations

import sqlite3
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dnh.db import Store
from dnh.htmlutil import parse_html, select_nodes
from dnh.models import Album, Credit, Track
from dnh.public import templates as T
from dnh.public.data import (
    DEFAULT_PER,
    NEEDS_KEYS,
    PublicData,
    PublicDataError,
    ReadOnlyStore,
    group_credits,
    human_time,
    missing_fields,
    paginate,
    parse_filters,
)
from dnh.public.site import (
    Response,
    parse_query,
    parse_route,
    sniff_image_type,
)
from tests._tmp import temp_dir

#: 一个 1x1 的 PNG —— 用来伪造"本地已缓存封面"
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)

#: 用来验证分页的凑数专辑数量(总专辑数 = 5 张功能专辑 + 这个数)
PAGINATED = 30

#: 恶意字符串(所有模板测试都用它当输入)
XSS_TITLE = '<script>alert("xss")</script>'
XSS_CIRCLE = '<img src=x onerror=alert(1)>'
XSS_DESC = '第一行\n<script>alert("desc")</script>\n<img src=x onerror="alert(2)">'


# --------------------------------------------------------------------------
# fixture
# --------------------------------------------------------------------------


def make_album(title: str, **over: object) -> Album:
    payload: dict = {
        "title": title,
        "title_original": title,
        "title_romaji": "",
        "circle": "",
        "artists": [],
        "release_date": "",
        "event": "",
        "catalog_no": "",
        "tags": [],
        "genres": [],
        "cover_url": "",
        "cover_local": "",
        "tracks": [],
        "credits": [],
        "description": "",
        "price": "",
        "urls": {},
        "source_ids": {},
        "trust": 0,
        "edited": False,
    }
    payload.update(over)
    album = Album(
        title=payload["title"],
        title_original=payload["title_original"],
        title_romaji=payload["title_romaji"],
        circle=payload["circle"],
        artists=list(payload["artists"]),
        release_date=payload["release_date"],
        event=payload["event"],
        catalog_no=payload["catalog_no"],
        genres=list(payload["genres"]),
        tags=list(payload["tags"]),
        cover_url=payload["cover_url"],
        cover_local=payload["cover_local"],
        tracks=list(payload["tracks"]),
        credits=list(payload["credits"]),
        description=payload["description"],
        price=payload["price"],
        urls=dict(payload["urls"]),
        source_ids=dict(payload["source_ids"]),
        trust=int(payload["trust"]),
        edited=bool(payload["edited"]),
    )
    album.key = album.compute_key()
    return album


class Fixture:
    """一个装好数据的临时库 + 站点对象(数据层与 HTTP 测试共用同一份造法)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.covers = root / "data" / "covers"
        self.covers.mkdir(parents=True, exist_ok=True)
        self.cover_file = self.covers / "local-cover.img"
        self.cover_file.write_bytes(TINY_PNG)
        self.store = Store(root / "data" / "dnh.sqlite3")
        self.keys = self._seed()
        self._order_updated()
        self.data = PublicData(self.store, root=root, covers_dir=self.covers)

    def _seed(self) -> dict[str, str]:
        albums = [
            make_album(
                "幻想郷の記憶",
                title_romaji="Gensoukyou no Kioku",
                circle="ShibayanRecords",
                artists=["坂上なち"],
                release_date="2026-10-25",
                event="M3-2026秋",
                catalog_no="SBCD-042",
                tags=["東方Project", "Vocal"],
                genres=["Vocal"],
                cover_url="https://example.com/a.jpg",
                description="一张用来测试的专辑。",
                tracks=[Track(title="Opening", index=1, duration=83),
                        Track(title="幻想郷の記憶", index=2, duration=245)],
                credits=[Credit(name="ShibayanRecords", role="circle"),
                         Credit(name="坂上なち", role="vocal")],
                urls={"某站点": "https://某站点.net/album/101"},
                source_ids={"某站点": "101"},
                trust=95,
            ),
            make_album(
                "Neon Drive",
                circle="ROUNDEX RECORD",
                release_date="2026-04-26",
                event="M3-2026春",
                tags=["Hardcore"],
                source_ids={"dojinmusic": "202"},
            ),
            make_album(
                "Crimson Lotus",
                circle="蝶々結び",
                release_date="2026-08-16",
                event="C108",
                catalog_no="CL-01",
                tags=["東方Project"],
                cover_local=str(self.cover_file),
                cover_url="https://example.com/should-not-be-used.jpg",
                description="第二张测试碟。",
                tracks=[Track(title="Lotus", index=1, duration=100)],
                credits=[Credit(name="みょん", role="vocal")],
                urls={"special": "https://crimson.example.com/"},
                source_ids={"某站点": "303"},
                trust=60,
            ),
            make_album(
                "Second Wind",
                circle="Roundex Record",
                release_date="2026-05-05",
                event="M3-2026春",
                tags=["Hardcore"],
                cover_url="https://example.com/b.jpg",
                source_ids={"dojinmusic": "204"},
            ),
            # 恶意输入: 标题/社团/简介/标签/外链/曲名里全是 XSS 载荷
            make_album(
                XSS_TITLE,
                circle=XSS_CIRCLE,
                title_romaji="<b>romaji</b>",
                event="M3-2026秋",
                tags=["<script>alert('tag')</script>"],
                genres=["<img src=x>"],
                cover_url="javascript:alert('cover')",
                description=XSS_DESC,
                tracks=[Track(title="<svg onload=alert(1)>", index=1, duration=60)],
                credits=[Credit(name="<b>bad</b>", role="artist")],
                urls={"evil": "javascript:alert('link')", "某站点": "https://某站点.net/album/999"},
                source_ids={"某站点": "999"},
            ),
        ]
        for index in range(1, PAGINATED + 1):
            albums.append(make_album(
                f"Paginated {index:02d}",
                circle="Pager Circle",
                tags=["PageTag"],
                release_date=f"2026-01-{index:02d}",
                source_ids={"manual": f"p{index}"},
            ))
        keys: dict[str, str] = {}
        names = ["alpha", "beta", "gamma", "variant", "evil"]
        for index, album in enumerate(albums):
            self.store.upsert_album(album)
            if index < len(names):
                keys[names[index]] = album.key
        keys["total"] = str(len(albums))
        keys["paginated"] = str(PAGINATED)
        return keys

    def _order_updated(self) -> None:
        """把 ``updated_at`` 设成确定的递增序列, 让排序测试不依赖插入耗时."""
        rows = self.store.query("SELECT key FROM albums ORDER BY key")
        for index, row in enumerate(rows):
            self.store.execute("UPDATE albums SET updated_at = ? WHERE key = ?",
                               (1_700_000_000 + index, row["key"]))

    def close(self) -> None:
        self.store.close()


def make_site(fixture: Fixture, **kwargs: object):
    from dnh.public.site import PublicSite

    return PublicSite(fixture.data, **kwargs)


def sample_view(**over: object) -> dict:
    """给纯模板测试用的视图字典."""
    view = {
        "key": "key|one",
        "href": "/album/key%7Cone",
        "title": "Sample Album",
        "title_alt": "",
        "romaji": "",
        "circle": "Sample Circle",
        "circle_href": "/circle/Sample%20Circle",
        "event": "M3-2026秋",
        "event_href": "/event/M3-2026%E7%A7%8B",
        "event_kind": "m3",
        "date": "2026-10-25",
        "year": "2026",
        "catalog_no": "SC-001",
        "price": "1000 JPY",
        "booth": "A-12",
        "hall": "第一展示場",
        "artists": ["Artist A"],
        "tags": ["Tag1", "Tag2"],
        "genres": ["Vocal"],
        "has_cover": True,
        "cover": "/cover/key%7Cone",
        "cover_local": "",
        "cover_url": "https://example.com/c.jpg",
        "track_count": 2,
        "tracks": [
            {"index": 1, "disc": 1, "title": "T1", "artists": ["Artist A"], "duration": 83, "duration_text": "1:23"},
            {"index": 2, "disc": 1, "title": "T2", "artists": [], "duration": 245, "duration_text": "4:05"},
        ],
        "credits": [
            {"role": "circle", "label": "社团", "names": ["Sample Circle"], "count": 1},
            {"role": "vocal", "label": "演唱", "names": ["Artist A"], "count": 1},
        ],
        "description": "line one\nline two",
        "urls": [{"url": "https://某站点.net/album/1"}],
        "edited": False,
        "updated_at": 1_700_000_000.0,
        "updated_text": "2023-11-15 06:13",
        "missing": [],
    }
    view.update(over)
    return view


def empty_pager(total: int = 0, *, page: int = 1, per: int = 24) -> dict:
    return paginate(total, page, per)


# --------------------------------------------------------------------------
# 1. 模板 / 工具纯函数
# --------------------------------------------------------------------------


class TestTemplateHelpers(unittest.TestCase):
    def test_esc_escapes_tags_and_quotes(self) -> None:
        self.assertEqual(T.esc('<a href="x">&\'</a>'),
                         "&lt;a href=&quot;x&quot;&gt;&amp;&#x27;&lt;/a&gt;")
        self.assertEqual(T.esc(None), "")
        self.assertEqual(T.esc(42), "42")

    def test_safe_url_rejects_dangerous_schemes(self) -> None:
        for evil in (
            "javascript:alert(1)",
            "JAVASCRIPT:alert(1)",
            "java\tscript:alert(1)",
            "java\nscript:alert(1)",
            "data:text/html;base64,PHNjcmlwdD4=",
            "vbscript:msgbox(1)",
            "file:///etc/passwd",
            "  javascript:alert(1)  ",
            "",
            None,
        ):
            self.assertEqual(T.safe_url(evil), "", f"应当拒绝: {evil!r}")
        for ok in (
            "https://example.com/a.jpg",
            "http://example.com/a.jpg",
            "//example.com/a.jpg",
            "/cover/abc",
        ):
            self.assertEqual(T.safe_url(ok), ok, f"应当放行: {ok!r}")

    def test_safe_url_blocks_relative_non_path(self) -> None:
        self.assertEqual(T.safe_url("example.com/a.jpg"), "example.com/a.jpg")
        self.assertEqual(T.safe_url("question?", allow_relative=False), "")

    def test_quote_segment_encodes_pipe_and_cjk(self) -> None:
        self.assertEqual(T.quote_segment("a|b c"), "a%7Cb%20c")
        self.assertTrue(T.quote_segment("幻想郷").startswith("%"))

    def test_cover_html_placeholder_and_sanitizing(self) -> None:
        fallback = T.cover_html(sample_view(has_cover=False, cover=""))
        self.assertIn("cover-fallback", fallback)
        self.assertIn("S", fallback)  # 标题首字
        self.assertNotIn("<img", fallback)
        evil = T.cover_html(sample_view(cover="javascript:alert(1)"))
        self.assertNotIn("<img", evil)
        good = T.cover_html(sample_view())
        self.assertIn('src="/cover/key%7Cone"', good)
        self.assertIn('alt="Sample Album 封面"', good)

    def test_album_card_escapes_all_fields(self) -> None:
        card = T.album_card(sample_view(
            title=XSS_TITLE, circle=XSS_CIRCLE, tags=[XSS_TITLE],
            circle_href="/circle/" + urllib.parse.quote(XSS_CIRCLE), href="/album/x",
        ))
        self.assertNotIn("<script>", card)
        self.assertNotIn("<img src=x", card)      # 恶意属性没有落成真标签
        self.assertNotIn("<img src=x onerror", card)
        self.assertIn("&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;", card)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", card)   # 文本里也只是转义后的字面量

    def test_album_card_drops_javascript_href(self) -> None:
        card = T.album_card(sample_view(href="javascript:alert(1)"))
        self.assertNotIn("javascript:", card)
        self.assertIn('href=""', card)

    def test_detail_page_escapes_and_renders_blocks(self) -> None:
        page = T.album_detail_page(
            T.SiteContext(), album=sample_view(description=XSS_DESC, title=XSS_TITLE),
        )
        self.assertIn("<h2 class=\"detail-title\">", page)
        self.assertIn("曲目表", page)
        self.assertIn("1:23", page)
        self.assertIn("4:05", page)
        self.assertNotIn("<script>", page)
        self.assertNotIn("<img src=x", page)     # 简介里的 <img ...> 只是转义后的文本
        self.assertIn("&lt;img src=x onerror=&quot;alert(2)&quot;&gt;", page)
        self.assertIn("第一行", page)

    def test_detail_page_edit_link_and_missing_notice(self) -> None:
        page = T.album_detail_page(
            T.SiteContext(is_editor=True),
            album=sample_view(missing=["no_cover", "no_desc"]),
        )
        self.assertIn("待补充信息", page)
        self.assertIn("缺封面", page)
        self.assertIn("补充这些信息", page)
        # 编辑入口是站内的 /album/<key>/edit, 不是任何外部面板
        self.assertIn("/album/key%7Cone/edit", page)
        self.assertNotIn("管理面板", page)
        # 访客看到的是登录引导, 没有编辑按钮
        guest = T.album_detail_page(T.SiteContext(), album=sample_view(missing=["no_desc"]))
        self.assertIn("登录后可补充", guest)
        self.assertNotIn("编辑这张专辑", guest)
        self.assertNotIn("/album/key%7Cone/edit", guest)

    def test_detail_page_edit_href_follows_album_path(self) -> None:
        """详情页编辑入口只由 album 的站内路径拼出来(模板里不再接受外部 URL)."""
        page = T.album_detail_page(T.SiteContext(is_editor=True), album=sample_view(href="/album/x"))
        self.assertIn('href="/album/x/edit"', page)
        # 没有 href 时不给按钮, 绝不渲染空链接
        bare = T.album_detail_page(T.SiteContext(is_editor=True), album=sample_view(href=""))
        self.assertNotIn("编辑这张专辑", bare)
        self.assertNotIn('href="/album/edit"', bare)

    def test_duration_and_date_formatting(self) -> None:
        self.assertEqual(T.duration_text(83), "1:23")
        self.assertEqual(T.duration_text(3725), "1:02:05")
        self.assertEqual(T.duration_text(0), "")
        self.assertEqual(T.duration_text(None), "")
        self.assertEqual(T.duration_text("x"), "")
        self.assertEqual(T.total_duration_text([{"duration": 83}, {"duration": 245}]), "5 分 28 秒")
        self.assertEqual(T.total_duration_text([{"duration": None}]), "")
        self.assertEqual(T.total_duration_text([{"duration": 7200}]), "2 小时 0 分")
        self.assertEqual(T.format_date("2026-10-25"), "2026-10-25")
        self.assertEqual(T.format_date(""), "—")
        self.assertEqual(T.format_date("2026-10"), "2026-10")

    def test_missing_labels_and_roles(self) -> None:
        self.assertEqual(T.role_label("arrange"), "编曲")
        self.assertEqual(T.role_label("unknown_role"), "unknown_role")
        # 数据源显示名整表删除: 用户端不再有"来源"这种概念
        self.assertFalse(hasattr(T, "SOURCE_LABELS"))
        self.assertFalse(hasattr(T, "source_label"))
        self.assertEqual(T.percentage(1, 4), 25.0)
        self.assertEqual(T.percentage(1, 0), 0.0)

    def test_build_url_orders_and_drops_empty(self) -> None:
        url = T.build_url("/albums", {"q": "ab", "page": 2, "empty": "", "none": None,
                                      "flag": True, "off": False, "tags": ["a", "b"]})
        self.assertEqual(url, "/albums?flag=1&off=0&page=2&q=ab&tags=a&tags=b")
        self.assertEqual(T.build_url("/albums", {}), "/albums")
        self.assertEqual(T.build_url("/albums", {"page": None, "q": ""}), "/albums")

    def test_make_url_for_overrides_and_removes(self) -> None:
        url_for = T.make_url_for("/albums", {"q": "ab", "page": 3, "tag": "x"})
        self.assertEqual(url_for(page=1), "/albums?page=1&q=ab&tag=x")
        self.assertEqual(url_for(q=None), "/albums?page=3&tag=x")
        self.assertEqual(url_for(page=2, tag="y"), "/albums?page=2&q=ab&tag=y")

    def test_badges_for_missing_and_edited(self) -> None:
        badges = T.badges_for(sample_view(missing=["no_cover", "no_tracks"], edited=True,
                                          has_cover=False, track_count=0))
        self.assertIn("待补充 2", badges)
        self.assertIn("已校订", badges)
        self.assertIn("无封面", badges)
        self.assertIn("无曲目", badges)
        self.assertEqual(T.badges_for(sample_view(track_count=3)), "")

    def test_pagination_html_marks_current_and_disabled(self) -> None:
        url_for = T.make_url_for("/albums", {})
        html = T.pagination(empty_pager(10, page=1, per=5), url_for)
        self.assertIn("is-current", html)
        self.assertIn("is-disabled", html)
        self.assertNotIn("rel=\"prev\"", html)
        html2 = T.pagination(empty_pager(10, page=2, per=5), url_for)
        self.assertIn('rel="prev"', html2)
        self.assertNotIn("下一页</a>", html2.split("is-disabled")[0])
        self.assertEqual(T.pagination(empty_pager(3, page=1, per=24), url_for), "")

    def test_filter_panel_preserves_selection_and_escapes(self) -> None:
        panel = T.filter_panel(
            {"q": XSS_TITLE, "event": "M3-2026春", "sort": "date", "has_cover": True},
            action="/albums",
            events=[{"value": "M3-2026春", "label": "M3-2026春"}],
            kinds=[{"value": "m3", "label": "M3"}],
            years=["2026"],
            tags=[{"value": "東方Project", "count": 2}],
            sorts=(("updated", "最近更新"), ("date", "发售日")),
        )
        self.assertNotIn("<script>", panel)
        self.assertIn('value="&lt;script&gt;', panel)
        self.assertIn('<option value="M3-2026春" selected>', panel)
        self.assertIn('<option value="date" selected>', panel)
        self.assertIn("checked", panel)
        self.assertIn('action="/albums"', panel)
        # 来源筛选器整块消失
        self.assertNotIn("来源", panel)
        self.assertNotIn('name="source"', panel)

    def test_active_filter_chips_have_remove_links(self) -> None:
        url_for = T.make_url_for("/albums", {"q": "ab", "tag": "x", "page": 2})
        chips = T.active_filter_chips({"q": "ab", "tag": "x"}, url_for)
        self.assertIn("搜索", chips)
        self.assertIn("标签", chips)
        self.assertIn("清空", chips)
        # “点掉 q” 会回到第 1 页, 同时保留其它筛选
        self.assertIn('href="/albums?tag=x"', chips)
        self.assertIn('href="/albums?q=ab"', chips)
        self.assertIn("href=\"/albums\"", chips)     # 清空 = 什么都不带

    def test_active_filter_chips_renders_list_values(self) -> None:
        url_for = T.make_url_for("/albums", {"needs": ["no_cover", "no_desc"]})
        chips = T.active_filter_chips({"needs": ["no_cover", "no_desc"]}, url_for)
        self.assertIn("缺封面、缺简介", chips)
        self.assertNotIn("[", chips)

    def test_active_filter_chips_skip_page_and_empty(self) -> None:
        url_for = T.make_url_for("/albums", {})
        self.assertEqual(T.active_filter_chips({"page": 3, "q": ""}, url_for), "")

    def test_stats_page_renders_cards_and_bars(self) -> None:
        stats = {
            "albums": 11, "circles": 5, "events": 3, "tags": 4, "tracks": 4, "edited": 0,
            "has_profile": 3,
            "coverage": {"local": 1, "remote": 3, "none": 7, "rate": 36.4},
            "completeness": {"no_tracks": 8, "no_desc": 8, "no_cover": 7, "no_circle": 0, "no_date": 1},
            "years": [{"value": "2026", "count": 10}],
            "event_top": [{"value": "M3-2026秋", "label": "M3-2026秋", "count": 2}],
            "kinds": [{"value": "m3", "label": "M3", "count": 10}],
            "genres": [{"value": "Vocal", "count": 1}],
            "tags_top": [{"value": "東方Project", "count": 2}],
            "circles_top": [{"value": "Pager Circle", "count": 6}],
        }
        page = T.stats_page(T.SiteContext(is_editor=True), stats=stats)
        self.assertEqual(page.count("stat-card"), 6)
        self.assertIn("封面覆盖率", page)
        self.assertIn("36.4%", page)
        self.assertIn("bar-fill", page)
        self.assertIn("曲风 Top 20", page)
        # 来源构成整块删除; 也不再提"歌单/后台任务"
        self.assertNotIn("来源构成", page)
        self.assertNotIn("歌单", page)
        self.assertNotIn("后台任务", page)

    def test_event_page_toggles_grouping(self) -> None:
        url_for = T.make_url_for("/event/M3-2026%E7%A7%8B", {"group": "circle"})
        page = T.event_page(
            T.SiteContext(), name="M3-2026秋", kind="m3", items=[],
            groups=[{"label": "ROUNDEX RECORD", "count": 2, "items": [sample_view()]}],
            group_by="circle", total=2, url_for=url_for,
        )
        self.assertIn("按社团", page)
        self.assertIn("按日期", page)
        self.assertIn('href="/event/M3-2026%E7%A7%8B?group=date"', page)
        self.assertIn("M3", page)  # 展会类型
        self.assertIn("album-card", page)

    def test_inbox_page_lists_badges_and_tabs(self) -> None:
        page = T.inbox_page(
            T.SiteContext(is_editor=True),
            kinds=[{"value": "no_cover", "label": "缺封面", "count": 7}],
            items=[sample_view(circle="", missing=["no_cover", "no_desc"], event="")],
            pager=empty_pager(7, page=1, per=24),
            active_kind="no_cover",
            url_for=T.make_url_for("/edit/no_cover", {}),
            edit_url_for=lambda key: f"/album/{T.quote_segment(key)}/edit",
        )
        self.assertIn("缺封面", page)
        self.assertIn("queue-row", page)
        self.assertIn("/album/key%7Cone/edit", page)     # 站内编辑入口
        self.assertNotIn("管理面板", page)
        # 访客/普通用户看队列时不出现编辑按钮
        guest = T.inbox_page(
            T.SiteContext(),
            kinds=[{"value": "no_cover", "label": "缺封面", "count": 7}],
            items=[sample_view(circle="", missing=["no_cover"], event="")],
            pager=empty_pager(7, page=1, per=24),
            active_kind="no_cover",
            url_for=T.make_url_for("/edit/no_cover", {}),
            edit_url_for=lambda key: f"/album/{key}/edit",
        )
        self.assertNotIn("编辑 →", guest)
        # 队列总览页只保留「工作队列」的描述文字
        overview = T.inbox_page(T.SiteContext(), kinds=[], items=[], pager=empty_pager(0),
                                url_for=T.make_url_for("/edit", {}))
        self.assertIn("工作队列", overview)
        self.assertNotIn("只读", overview)
        self.assertNotIn("管理面板", overview)
        self.assertNotIn("补数据", overview)

    def test_layout_escapes_title_and_marks_active_nav(self) -> None:
        html = T.layout(T.SiteContext(totals={"albums": 1}, search_value=XSS_TITLE),
                        title=XSS_TITLE, body="<p>body</p>", active="stats")
        self.assertNotIn("<script>", html)
        self.assertIn("nav-link is-active", html)
        self.assertIn('value="&lt;script&gt;', html)

    def test_error_page_shows_status(self) -> None:
        page = T.error_page(T.SiteContext(), status=404, message="页面不存在: /nope")
        self.assertIn("404", page)
        self.assertIn("/nope", page)

    def test_rendered_pages_are_parseable_html(self) -> None:
        """结构化校验: 用项目自带的 htmlutil 解析输出, 而不是只做子串断言."""
        cards = "".join(T.album_card(sample_view(title=f"Album {i}")) for i in range(3))
        page = T.layout(T.SiteContext(), title="浏览专辑", body=f'<div class="grid">{cards}</div>')
        doc = parse_html(page)
        self.assertEqual(len(select_nodes(doc, ".album-card")), 3)
        self.assertEqual(len(select_nodes(doc, ".card-title")), 3)
        self.assertIn("Album 0", select_nodes(doc, ".card-title")[0].text)
        self.assertEqual(len(select_nodes(doc, "header.topbar")), 1)

    def test_suggestions_html_escapes_and_sanitizes(self) -> None:
        html = T.suggestions_html({
            "tags": [{"value": XSS_TITLE, "count": 3, "href": "/tag/x"}],
            "events": [{"value": "M3", "count": 1, "href": "javascript:alert(1)"}],
            "circles": [],
        })
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("javascript:", html)
        self.assertEqual(T.suggestions_html({}), "")
        self.assertEqual(T.suggestions_html({"tags": [{"value": ""}]}), "")

    def test_album_grid_empty_state_with_suggestions(self) -> None:
        grid = T.album_grid([], empty_text="没找到", suggestions={"tags": [{"value": "T", "count": 2, "href": "/tag/T"}]})
        self.assertIn("没找到", grid)
        self.assertIn("也许你想找", grid)
        self.assertNotIn("也许你想找", T.album_grid([]))

    def test_group_credits_dedupes_and_orders(self) -> None:
        groups = group_credits([
            {"name": "A", "role": "vocal"},
            {"name": "A", "role": "vocal"},
            {"name": "B", "role": "circle"},
            {"name": "", "role": "artist"},
            {"name": "C", "role": "arrange"},
        ])
        self.assertEqual([g["role"] for g in groups], ["circle", "arrange", "vocal"])
        self.assertEqual(groups[0]["names"], ["B"])
        self.assertEqual(groups[2]["names"], ["A"])
        self.assertEqual(groups[2]["label"], "演唱")

    def test_human_time(self) -> None:
        # trust_tier 已随"不暴露来源"一起删除; 这里只测时间格式化
        self.assertEqual(human_time(0), "")
        self.assertRegex(human_time(1_700_000_000), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_missing_fields_detection(self) -> None:
        self.assertEqual(
            missing_fields({"cover_url": "https://x/y.jpg", "tracks": [{}], "description": "d",
                            "circle": "c", "release_date": "2026-01-01", "catalog_no": "n",
                            "credits": [{"name": "x"}], "urls": {"某站点": "https://v"}}),
            [],
        )
        self.assertEqual(
            missing_fields({"cover_url": "", "tracks": [], "description": "", "circle": "",
                            "release_date": "", "catalog_no": "", "credits": [], "urls": {}}),
            list(NEEDS_KEYS),
        )
        # javascript: 封面不算封面(与"按封面筛选"的 SQL 判据保持一致)
        self.assertIn("no_cover", missing_fields({"cover_url": "javascript:alert(1)"}))


# --------------------------------------------------------------------------
# 2. 路由与参数解析
# --------------------------------------------------------------------------


class TestRouting(unittest.TestCase):
    def test_parse_route_basic_pages(self) -> None:
        self.assertEqual(parse_route("/"), {"name": "home", "arg": ""})
        self.assertEqual(parse_route(""), {"name": "home", "arg": ""})
        self.assertEqual(parse_route("/stats"), {"name": "stats", "arg": ""})
        self.assertEqual(parse_route("/albums/"), {"name": "albums", "arg": ""})
        self.assertEqual(parse_route("/events"), {"name": "events", "arg": ""})
        self.assertEqual(parse_route("/edit"), {"name": "edit", "arg": ""})
        self.assertEqual(parse_route("/edit/no_cover"), {"name": "edit", "arg": "no_cover"})

    def test_parse_route_entity_percent_decoding(self) -> None:
        route = parse_route("/album/%E5%B9%BB%E6%83%B3%E9%83%B7%7C2026")
        self.assertEqual(route["name"], "album")
        self.assertEqual(route["arg"], "幻想郷|2026")
        self.assertEqual(parse_route("/circle/ShibayanRecords")["arg"], "ShibayanRecords")
        self.assertEqual(parse_route("/event/M3-2026%E7%A7%8B")["arg"], "M3-2026秋")
        self.assertEqual(parse_route("/tag/%E6%9D%B1%E6%96%B9Project")["arg"], "東方Project")

    def test_parse_route_index_fallback_and_unknown(self) -> None:
        # 实体路由缺少路径段 -> 退化到索引页
        self.assertEqual(parse_route("/album"), {"name": "albums", "arg": ""})
        self.assertEqual(parse_route("/circle"), {"name": "circles", "arg": ""})
        self.assertEqual(parse_route("/event"), {"name": "events", "arg": ""})
        self.assertEqual(parse_route("/tag"), {"name": "tags", "arg": ""})
        self.assertEqual(parse_route("/nope")["name"], "not_found")
        self.assertEqual(parse_route("/a/b/c")["name"], "not_found")

    def test_parse_route_strips_query_and_case(self) -> None:
        self.assertEqual(parse_route("/albums?q=x&page=2"), {"name": "albums", "arg": ""})
        self.assertEqual(parse_route("/STATS")["name"], "stats")
        self.assertEqual(parse_route("/static/public.css"), {"name": "static", "arg": "public.css"})
        self.assertEqual(parse_route("/cover/abc%7Cdef")["arg"], "abc|def")
        self.assertEqual(parse_route("/favicon.ico")["name"], "favicon")
        self.assertEqual(parse_route("/search")["name"], "search")

    def test_parse_query_helpers(self) -> None:
        parsed = parse_query("q=ab&tag=x&tag=y&empty=")
        self.assertEqual(parsed["q"], ["ab"])
        self.assertEqual(parsed["tag"], ["x", "y"])
        self.assertNotIn("empty", parsed)
        self.assertEqual(parse_query(""), {})

    def test_parse_filters_whitelist(self) -> None:
        filters = parse_filters({"q": "ab", "sort": "date", "kind": "m3", "has_cover": "1",
                                 "page": "3", "needs": "no_cover,bogus no_desc", "per": "48"})
        self.assertEqual(filters["q"], "ab")
        self.assertEqual(filters["sort"], "date")
        self.assertEqual(filters["kind"], "m3")
        self.assertTrue(filters["has_cover"])
        self.assertEqual(filters["needs"], ["no_cover", "no_desc"])
        self.assertNotIn("page", filters)   # 分页单独处理, 不进筛选
        self.assertNotIn("per", filters)
        self.assertNotIn("bogus", str(filters))
        # 非法排序被丢弃而不是报错
        self.assertNotIn("sort", parse_filters({"sort": "'; DROP TABLE albums; --"}))
        self.assertNotIn("has_cover", parse_filters({"has_cover": "0"}))

    def test_parse_filters_list_values_and_extra(self) -> None:
        filters = parse_filters({"q": ["first", "second"], "event": ["M3-2026秋"]},
                                extra={"circle": "X", "empty": ""})
        self.assertEqual(filters["q"], "first")
        self.assertEqual(filters["event"], "M3-2026秋")
        self.assertEqual(filters["circle"], "X")
        self.assertNotIn("empty", filters)

    def test_sniff_image_type(self) -> None:
        self.assertEqual(sniff_image_type(TINY_PNG), "image/png")
        self.assertEqual(sniff_image_type(b"\xff\xd8\xff\xe0junk"), "image/jpeg")
        self.assertEqual(sniff_image_type(b"GIF89ajunk"), "image/gif")
        self.assertEqual(sniff_image_type(b"RIFF" + b"\x00" * 4 + b"WEBPVP8 "), "image/webp")
        self.assertEqual(sniff_image_type(b"<svg xmlns='...'></svg>"), "image/svg+xml")
        self.assertEqual(sniff_image_type(b"\x00\x01\x02"), "application/octet-stream")


class TestPagination(unittest.TestCase):
    def test_zero_total(self) -> None:
        pager = paginate(0, 1, 24)
        self.assertEqual(pager["pages"], 1)
        self.assertEqual(pager["total"], 0)
        self.assertEqual(pager["window"], [1])
        self.assertFalse(pager["has_prev"])
        self.assertFalse(pager["has_next"])

    def test_exact_multiple_does_not_add_empty_page(self) -> None:
        pager = paginate(48, 2, 24)
        self.assertEqual(pager["pages"], 2)
        self.assertFalse(pager["has_next"])
        self.assertEqual(pager["offset"], 24)
        self.assertEqual(pager["window"], [1, 2])

    def test_clamps_out_of_range_pages(self) -> None:
        self.assertEqual(paginate(100, 999, 10)["page"], 10)
        self.assertEqual(paginate(100, 0, 10)["page"], 1)
        self.assertEqual(paginate(100, -5, 10)["page"], 1)
        self.assertEqual(paginate(100, "abc", 10)["page"], 1)
        self.assertEqual(paginate(100, 3, 0)["per"], 1)
        self.assertEqual(paginate(100, 3, 100000)["per"], 500)

    def test_window_uses_gaps(self) -> None:
        window = paginate(1000, 50, 10, window=1)["window"]
        self.assertEqual(window[0], 1)
        self.assertEqual(window[1], "...")
        self.assertEqual(window[-2], "...")
        self.assertEqual(window[-1], 100)
        self.assertEqual([n for n in window if isinstance(n, int)], [1, 49, 50, 51, 100])
        mid = paginate(1000, 2, 10, window=2)["window"]
        self.assertEqual(mid[0], 1)          # 首页始终显示
        self.assertNotIn("...", mid[:1])


# --------------------------------------------------------------------------
# 3. 只读数据层
# --------------------------------------------------------------------------


class DataCase(unittest.TestCase):
    fixture: Fixture

    @classmethod
    def setUpClass(cls) -> None:
        cls._ctx = temp_dir("public-data-")
        cls.fixture = Fixture(cls._ctx.__enter__())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture.close()
        cls._ctx.__exit__(None, None, None)

    @property
    def data(self) -> PublicData:
        return self.fixture.data


class TestPublicData(DataCase):
    def test_columns_and_totals(self) -> None:
        cols = self.data.columns()
        self.assertIn("payload", cols)
        self.assertIn("norm_circle", cols)
        totals = self.data.totals()
        self.assertEqual(totals["albums"], int(self.fixture.keys["total"]))
        self.assertEqual(totals["tracks"], 4)
        self.assertEqual(totals["circles"], 5)
        self.assertEqual(totals["events"], 3)

    def test_query_albums_pagination(self) -> None:
        pages = (PAGINATED + 11) // 12
        views, pager = self.data.query_albums(q="Paginated", page=1, per=12, sort="title")
        self.assertEqual(pager["total"], PAGINATED)
        self.assertEqual(pager["pages"], pages)
        self.assertEqual([v["title"] for v in views][:2], ["Paginated 01", "Paginated 02"])
        self.assertEqual(len(views), 12)
        last, pager2 = self.data.query_albums(q="Paginated", page=pages, per=12, sort="title")
        self.assertEqual([v["title"] for v in last],
                         [f"Paginated {n:02d}" for n in range(25, PAGINATED + 1)])
        self.assertFalse(pager2["has_next"])
        beyond, pager3 = self.data.query_albums(q="Paginated", page=99, per=12, sort="title")
        self.assertEqual(pager3["page"], pages)
        self.assertEqual(len(beyond), PAGINATED - (pages - 1) * 12)
        empty, _ = self.data.query_albums(q="zzz-nothing", page=1, per=12)
        self.assertEqual(empty, [])

    def test_query_albums_per_page_is_whitelisted(self) -> None:
        for per in (0, 7, 100000):
            views, pager = self.data.query_albums(page=1, per=per, limit=None)
            self.assertEqual(pager["per"], DEFAULT_PER)
            self.assertLessEqual(len(views), DEFAULT_PER)

    def test_query_albums_sorting(self) -> None:
        by_title, _ = self.data.query_albums(q="Paginated", page=1, per=6, sort="title")
        self.assertEqual([v["title"] for v in by_title][0], "Paginated 01")
        by_tracks, _ = self.data.query_albums(page=1, per=3, sort="tracks")
        self.assertEqual(by_tracks[0]["title"], "幻想郷の記憶")
        self.assertEqual(by_tracks[0]["track_count"], 2)
        by_date, _ = self.data.query_albums(page=1, per=3, sort="date")
        self.assertEqual(by_date[0]["date"], "2026-10-25")

    def test_query_albums_filters(self) -> None:
        keys = self.fixture.keys
        event, pager = self.data.query_albums(event="M3-2026秋", per=24)
        self.assertEqual({v["key"] for v in event}, {keys["alpha"], keys["evil"]})
        self.assertEqual(pager["total"], 2)
        year, _ = self.data.query_albums(year="2026", per=48)
        self.assertEqual(len(year), int(self.fixture.keys["total"]) - 1)
        tag, _ = self.data.query_albums(tag="東方Project", per=48)
        self.assertEqual({v["key"] for v in tag}, {keys["alpha"], keys["gamma"]})
        cover, _ = self.data.query_albums(has_cover=True, per=48)
        self.assertEqual(len(cover), 3)          # javascript: 封面不算封面
        nocover, _ = self.data.query_albums(has_cover=False, per=48)
        self.assertEqual(len(nocover), int(self.fixture.keys["total"]) - 3)
        kind, _ = self.data.query_albums(kind="m3", per=48)
        self.assertEqual(len(kind), 4)

    def test_source_filter_is_gone(self) -> None:
        """用户端不再有"按来源筛选" —— 传 source 会被当成非法参数直接报错."""
        with self.assertRaises(TypeError):
            self.data.query_albums(source="dojinmusic", per=48)
        self.assertFalse(hasattr(self.data, "sources"))

    def test_query_albums_circle_filter_matches_variants(self) -> None:
        views, pager = self.data.query_albums(circle="ROUNDEX RECORD", per=50)
        self.assertEqual(pager["total"], 2)
        self.assertEqual({v["title"] for v in views}, {"Neon Drive", "Second Wind"})

    def test_ids_never_leak_sql(self) -> None:
        """筛选值里的 SQL 片段只会被当成字面量."""
        views, pager = self.data.query_albums(q="'; DROP TABLE albums; --", per=5)
        self.assertEqual(views, [])
        self.assertEqual(pager["total"], 0)
        self.assertEqual(self.data.album_count(), int(self.fixture.keys["total"]))

    def test_search_multi_field(self) -> None:
        keys = self.fixture.keys
        for query in ("Shibayan", "坂上なち", "Opening", "SBCD-042", "幻想郷", "neon drive"):
            views, _ = self.data.query_albums(q=query, per=10)
            found = {v["key"] for v in views}
            self.assertIn(keys["alpha"] if query != "neon drive" else keys["beta"],
                          found, f"搜索 {query!r} 没有命中预期专辑")

    def test_search_suggestions_when_zero_hits(self) -> None:
        """标签/展会/社团名搜不到专辑时, 给出入口(而不是空白页)."""
        views, pager = self.data.query_albums(q="東方Project", per=10)
        self.assertEqual((views, pager["total"]), ([], 0))       # 搜索不含标签, 与管理面板一致
        suggestions = self.data.search_suggestions("東方Project")
        self.assertEqual([t["value"] for t in suggestions["tags"]], ["東方Project"])
        self.assertEqual(suggestions["tags"][0]["count"], 2)
        self.assertTrue(suggestions["tags"][0]["href"].startswith("/tag/"))
        events = self.data.search_suggestions("M3-2026春")
        self.assertEqual([e["value"] for e in events["events"]], ["M3-2026春"])
        circles = self.data.search_suggestions("pager")
        self.assertEqual([c["value"] for c in circles["circles"]], ["Pager Circle"])
        self.assertEqual(self.data.search_suggestions(""), {})
        self.assertEqual(self.data.search_suggestions("zzz-nothing-at-all"), {})

    def test_needs_filter_and_counts(self) -> None:
        counts = {row["value"]: row["count"] for row in self.data.needs_counts()}
        total = int(self.fixture.keys["total"])
        self.assertEqual(counts["no_cover"], total - 3)
        self.assertEqual(counts["no_desc"], total - 3)
        self.assertEqual(counts["no_tracks"], PAGINATED + 2)
        self.assertEqual(counts["no_date"], 1)
        self.assertEqual(counts["no_circle"], 0)
        self.assertEqual(counts["no_source_url"], PAGINATED + 2)
        views, pager = self.data.query_albums(needs=["no_cover"], per=48)
        self.assertEqual(pager["total"], total - 3)
        self.assertTrue(all("no_cover" in v["missing"] for v in views))
        both, pager2 = self.data.query_albums(needs=["no_cover", "no_tracks"], per=48)
        self.assertGreaterEqual(pager2["total"], pager["total"])

    def test_get_album_detailed(self) -> None:
        album = self.data.get_album(self.fixture.keys["alpha"])
        assert album is not None
        self.assertEqual(album["title"], "幻想郷の記憶")
        self.assertEqual(album["title_alt"], "")
        self.assertEqual(album["romaji"], "Gensoukyou no Kioku")
        self.assertEqual(len(album["tracks"]), 2)
        self.assertEqual(album["tracks"][0]["duration_text"], "1:23")
        self.assertEqual([g["role"] for g in album["credits"]], ["circle", "vocal"])
        self.assertEqual(album["credits"][1]["label"], "演唱")
        # 视图里不再有来源/可信度字段; 外链只剩 URL 本身
        self.assertNotIn("sources", album)
        self.assertNotIn("trust", album)
        self.assertNotIn("trust_tier", album)
        self.assertEqual(album["urls"][0]["url"], "https://某站点.net/album/101")
        # 页面只拿到站内中转地址 /link/<key>/<n>, 不写外部站名
        self.assertEqual(album["urls"][0]["href"],
                         "/link/" + T.quote_segment(self.fixture.keys["alpha"]) + "/1")
        # name 是**用户填的链接名**(显示为 circle_site / x / youtube),
        # 它不是来源站名; 外部 URL 本身仍然只走 /link 中转、不写进 HTML。
        # embed / embed_height: 能嵌入播放的平台(SoundCloud / YouTube);
        # 只给平台 key 和高度, **不含外部地址** —— 页面不留外部 URL。
        # embed_src: iframe 直连平台用的播放器地址(仅 embed code 才有)
        self.assertEqual(sorted(album["urls"][0]),
                         ["embed", "embed_height", "embed_src", "href", "name",
                          "position", "url"])
        # 某站点 是被禁站, 不参与嵌入
        self.assertEqual(album["urls"][0]["embed"], "")
        self.assertEqual(album["missing"], [])   # 字段齐全 -> 没有缺口
        self.assertEqual(album["href"], "/album/" + T.quote_segment(self.fixture.keys["alpha"]))
        self.assertIsNone(self.data.get_album("does-not-exist"))
        self.assertIsNone(self.data.get_album(""))

    def test_view_cover_prefers_local_and_sanitizes_remote(self) -> None:
        gamma = self.data.get_album(self.fixture.keys["gamma"])
        assert gamma is not None
        self.assertTrue(gamma["cover"].startswith("/cover/"))
        evil = self.data.get_album(self.fixture.keys["evil"])
        assert evil is not None
        self.assertEqual(evil["cover"], "")      # javascript: 不是可用封面
        self.assertFalse(evil["has_cover"])
        self.assertIn("no_cover", evil["missing"])

    def test_related_albums(self) -> None:
        beta = self.data.get_album(self.fixture.keys["beta"])
        assert beta is not None
        related = self.data.related_albums(beta)
        self.assertEqual([v["title"] for v in related], ["Second Wind"])
        alpha = self.data.get_album(self.fixture.keys["alpha"])
        assert alpha is not None
        self.assertEqual(self.data.related_albums(alpha), [])

    def test_events_tags_years(self) -> None:
        events = {e["value"]: e["count"] for e in self.data.events()}
        self.assertEqual(events, {"M3-2026秋": 2, "M3-2026春": 2, "C108": 1})
        kinds = {k["value"]: k["count"] for k in self.data.event_kinds()}
        self.assertEqual(kinds["m3"], 4)
        self.assertEqual(kinds["comiket"], 1)
        tags = {t["value"]: t["count"] for t in self.data.tags()}
        self.assertEqual(tags["東方Project"], 2)
        self.assertEqual(tags["Hardcore"], 2)
        # 展会名/会次属于"结构化字段 event 已存"的重复标签, 公开列表里被规则过滤掉
        # (规则本体在 dnh/tagrules.py, 见 tests/test_public_filters.py 的逐条断言)
        self.assertNotIn("M3-2026春", tags)
        self.assertEqual(tags["PageTag"], PAGINATED)
        years = self.data.years()
        self.assertEqual([y["value"] for y in years], ["2026"])
        self.assertEqual(years[0]["count"], int(self.fixture.keys["total"]) - 1)

    def test_circles_grouping_merges_variants(self) -> None:
        circles = self.data.circles_all()
        by_count = {c["value"]: c["count"] for c in circles}
        self.assertEqual(by_count["Pager Circle"], PAGINATED)
        roundex = [c for c in circles if c["count"] == 2][0]
        self.assertEqual(roundex["count"], 2)
        self.assertEqual(len(roundex["variants"]), 2)
        self.assertIn("ROUNDEX RECORD", roundex["variants"])
        self.assertIn("Roundex Record", roundex["variants"])
        self.assertEqual(self.data.circle_variants("ROUNDEX RECORD"), roundex["variants"])

        page, pager = self.data.circles(query="pager", page=1, per=10)
        self.assertEqual(pager["total"], 1)
        self.assertEqual(page[0]["value"], "Pager Circle")
        self.assertEqual(self.data.circles(query="zzz")[0], [])

    def test_inbox(self) -> None:
        total = int(self.fixture.keys["total"])
        items, pager = self.data.inbox("no_cover", page=1, per=12)
        self.assertEqual(pager["total"], total - 3)
        self.assertEqual(len(items), 12)
        self.assertTrue(all("no_cover" in i["missing"] for i in items))
        # per 只接受白名单值 —— 传 5 会被夹回默认的 24
        all_items, pager_odd = self.data.inbox("no_cover", page=1, per=5)
        self.assertEqual(pager_odd["per"], DEFAULT_PER)
        self.assertEqual(pager_odd["total"], total - 3)
        self.assertEqual(len(all_items), DEFAULT_PER)
        everything, pager2 = self.data.inbox("")
        self.assertEqual(pager2["total"], total)
        self.assertEqual(len(everything), min(total, DEFAULT_PER))
        # 未知种类退化成"全部"
        _, pager3 = self.data.inbox("bogus", page=1, per=12)
        self.assertEqual(pager3["total"], total)
        self.assertGreaterEqual(self.data.inbox_total(), 32)          # 每种缺口的并集
        self.assertLessEqual(self.data.inbox_total(), pager2["total"])

    def test_event_groups(self) -> None:
        groups, pager, total = self.data.event_groups("M3-2026春", group_by="circle")
        self.assertEqual(total, 2)
        self.assertEqual(pager["total"], 2)
        labels = {g["label"]: g["count"] for g in groups}
        self.assertEqual(labels["ROUNDEX RECORD"], 1)
        self.assertEqual(labels["Roundex Record"], 1)
        date_groups, _, _ = self.data.event_groups("M3-2026春", group_by="date")
        self.assertEqual([g["label"] for g in date_groups], ["2026-05-05", "2026-04-26"])
        empty_groups, _, empty_total = self.data.event_groups("nope")
        self.assertEqual((empty_groups, empty_total), ([], 0))

    def test_event_groups_caps_items_per_group(self) -> None:
        groups, pager, total = self.data.event_groups("2026", group_by="circle")
        self.assertEqual((groups, total), ([], 0))
        # 用真实存在的一个小组验证 per_group 上限
        groups2, _, _ = self.data.event_groups("M3-2026秋", group_by="circle", per_group=1)
        self.assertTrue(all(len(g["items"]) <= 1 for g in groups2))

    def test_stats_numbers(self) -> None:
        stats = self.data.stats()
        total = int(self.fixture.keys["total"])
        self.assertEqual(stats["albums"], total)
        self.assertEqual(stats["tracks"], 4)
        self.assertEqual(stats["coverage"]["local"], 1)
        self.assertEqual(stats["coverage"]["remote"], 2)    # javascript: 封面不算
        self.assertEqual(stats["coverage"]["none"], total - 3)
        self.assertEqual(stats["completeness"]["no_tracks"], PAGINATED + 2)
        self.assertEqual(stats["completeness"]["no_desc"], total - 3)
        self.assertEqual(stats["completeness"]["no_date"], 1)
        self.assertEqual(stats["event_top"][0]["count"], 2)
        self.assertIn("Vocal", {g["value"] for g in stats["genres"]})
        self.assertEqual(stats["circles_top"][0]["value"], "Pager Circle")
        self.assertEqual(stats["has_profile"], 3)
        self.assertEqual(stats["edited"], 0)
        self.assertEqual(stats["events"], 3)
        self.assertEqual(stats["circles"], 5)

    def test_stats_cached_until_invalidated(self) -> None:
        first = self.data.stats()
        self.assertIs(first, self.data.stats())     # TTL 缓存命中(同一对象)
        self.data.invalidate()
        self.assertIsNot(first, self.data.stats())

    def test_cover_lookup_and_path_guard(self) -> None:
        local, remote = self.data.cover_lookup(self.fixture.keys["gamma"])
        self.assertEqual(Path(local), self.fixture.cover_file)
        self.assertEqual(remote, "https://example.com/should-not-be-used.jpg")
        self.assertEqual(self.data.cover_lookup("nope"), ("", ""))
        resolved = self.data.resolve_cover_path(str(self.fixture.cover_file))
        assert resolved is not None
        self.assertTrue(resolved.is_file())
        for evil in ("../../config/config.toml", "/etc/passwd",
                     str(self.fixture.root / "config" / "config.toml"),
                     str(self.fixture.root / "data" / "dnh.sqlite3"), ""):
            self.assertIsNone(self.data.resolve_cover_path(evil), f"应当拒绝: {evil!r}")

    def test_columns_detection_reports_missing_table(self) -> None:
        empty = Store(self.fixture.root / "empty.sqlite3")
        empty.execute("DROP TABLE albums")
        broken = PublicData(empty, root=self.fixture.root)
        with self.assertRaises(PublicDataError):
            broken.columns()
        empty.close()


class TestReadOnlyStore(unittest.TestCase):
    def test_open_existing_db_and_read(self) -> None:
        with temp_dir("public-ro-") as root:
            store = Store(root / "data" / "dnh.sqlite3")
            album = make_album("Read Only", circle="RO")
            store.upsert_album(album)
            store.close()
            ro = ReadOnlyStore(root / "data" / "dnh.sqlite3")
            row = ro.query_one("SELECT COUNT(*) AS n FROM albums")
            assert row is not None
            self.assertEqual(row["n"], 1)
            self.assertTrue(ro.readonly)
            ro.close()

    def test_write_is_rejected_by_sqlite(self) -> None:
        with temp_dir("public-ro-write-") as root:
            store = Store(root / "data" / "dnh.sqlite3")
            store.upsert_album(make_album("Keep"))
            store.close()
            ro = ReadOnlyStore(root / "data" / "dnh.sqlite3")
            with self.assertRaises(sqlite3.OperationalError):
                ro.query("INSERT INTO albums(key) VALUES ('x')")
            with self.assertRaises(sqlite3.OperationalError):
                ro.query("DELETE FROM albums")
            ro.close()

    def test_missing_file_raises_clear_error(self) -> None:
        with temp_dir("public-ro-missing-") as root:
            with self.assertRaises(PublicDataError):
                ReadOnlyStore(root / "nope.sqlite3")

    def test_readonly_store_does_not_create_schema(self) -> None:
        """空文件(不是合法库)也必须报错, 而不是被"顺手"建表."""
        with temp_dir("public-ro-empty-") as root:
            path = root / "empty.sqlite3"
            path.write_bytes(b"")
            store = ReadOnlyStore(path)
            with self.assertRaises(sqlite3.DatabaseError):
                store.query("SELECT COUNT(*) FROM albums")
            self.assertEqual(path.read_bytes(), b"")   # 没有被写入
            store.close()


# --------------------------------------------------------------------------
# 4. 页面渲染 + HTTP 集成
# --------------------------------------------------------------------------


class TestPages(DataCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.site = make_site(cls.fixture)

    def render(self, path: str, query: dict | None = None) -> Response:
        return self.site.render(path, query or {})

    def body(self, path: str, query: dict | None = None) -> str:
        return self.render(path, query).body.decode("utf-8")

    def test_all_pages_render(self) -> None:
        for path in ("/", "/albums", "/circles", "/events", "/tags", "/stats", "/about"):
            resp = self.render(path)
            self.assertEqual(resp.status, 200, path)
            self.assertIn("<!doctype html>", resp.body.decode("utf-8"))
        # 工作队列只对编辑者开放(匿名访问一律 403, 不再渲染页面)
        self.assertEqual(self.render("/edit").status, 403)

    def test_list_pages_with_query(self) -> None:
        cases = [
            ("/albums", {"q": "Pager", "page": "2", "per": "12"}),
            ("/albums", {"event": "M3-2026春", "sort": "date"}),
            ("/albums", {"tag": "Hardcore", "has_cover": "1"}),
            ("/albums", {"needs": "no_cover", "sort": "title"}),
            ("/tag/" + T.quote_segment("東方Project"), {}),
            ("/circle/" + T.quote_segment("ROUNDEX RECORD"), {}),
            ("/event/" + T.quote_segment("M3-2026春"), {"group": "date"}),
        ]
        for path, query in cases:
            resp = self.render(path, query)
            self.assertEqual(resp.status, 200, f"{path} {query}")
            html = resp.body.decode("utf-8")
            self.assertIn("album-card", html, f"{path} {query}")
            self.assertIn("<!doctype html>", html)

    def test_index_pages_render_without_cards(self) -> None:
        for path, query in (("/circles", {"q": "pager"}), ("/circles", {}), ("/events", {}), ("/tags", {})):
            resp = self.render(path, query)
            self.assertEqual(resp.status, 200, path)
            html = resp.body.decode("utf-8")
            self.assertIn("<!doctype html>", html)
            self.assertTrue("index-row" in html or "tagcloud" in html, path)
        self.assertIn("Pager Circle", self.body("/circles", {"q": "pager"}))
        self.assertIn("/event/", self.body("/events"))
        self.assertIn("tagcloud", self.body("/tags"))

    def test_edit_queue_page_lists_rows(self) -> None:
        """工作队列只对编辑者开放: 匿名访问 403; 编辑者视角的渲染在
        ``tests/test_public_filters.py`` 里用登录会话覆盖."""
        resp = self.render("/edit/no_desc", {"page": "2"})
        self.assertEqual(resp.status, 403)
        self.assertIn("工作队列只对编辑者开放", resp.body.decode("utf-8"))

    def test_album_detail_and_404(self) -> None:
        resp = self.render("/album/" + T.quote_segment(self.fixture.keys["alpha"]))
        self.assertEqual(resp.status, 200)
        html = resp.body.decode("utf-8")
        self.assertIn("幻想郷の記憶", html)
        self.assertIn("tracklist", html)
        self.assertIn("制作人员", html)
        self.assertEqual(self.render("/album/nope").status, 404)
        self.assertEqual(self.render("/definitely/not/here").status, 404)

    def test_pagination_links_preserve_filters(self) -> None:
        html = self.body("/albums", {"q": "Paginated", "per": "12"})
        self.assertIn("q=Paginated", html)
        self.assertIn("page=3", html)
        self.assertIn(f"命中 <b>{PAGINATED}</b>", html)

    def test_out_of_range_page_is_clamped_not_empty(self) -> None:
        html = self.body("/albums", {"q": "Paginated", "page": "999", "per": "12", "sort": "title"})
        self.assertIn("Paginated 30", html)
        self.assertEqual(html.count('class="album-card"'), 6)   # 最后一页只剩 6 张

    def test_empty_state_renders_suggestions(self) -> None:
        html = self.body("/albums", {"q": "東方Project"})
        self.assertIn("没有匹配的专辑", html)
        self.assertIn("也许你想找", html)
        self.assertIn("标签", html)
        self.assertIn("/tag/" + T.quote_segment("東方Project"), html)
        # 有结果时不该出现建议块
        hit = self.body("/albums", {"q": "Shibayan"})
        self.assertNotIn("也许你想找", hit)

    def test_source_links_allowed(self) -> None:
        """页面**可以**出现来源站名(已解禁); 只禁止管理面板字样与内部路径."""
        banned = ("管理面板", "/webui", "dnh.webui")
        paths = ["/", "/albums", "/stats", "/edit", "/about", "/circles", "/events", "/tags",
                 "/album/" + T.quote_segment(self.fixture.keys["alpha"]),
                 "/circle/" + T.quote_segment("ShibayanRecords"),
                 "/event/" + T.quote_segment("M3-2026春"),
                 "/tag/" + T.quote_segment("東方Project")]
        for path in paths:
            with self.subTest(path=path):
                html = self.body(path).lower()
                hits = [word for word in banned if word.lower() in html]
                self.assertEqual(hits, [], f"{path} 泄漏了来源信息: {hits}")

    def test_evil_album_page_has_no_injection(self) -> None:
        html = self.body("/album/" + T.quote_segment(self.fixture.keys["evil"]))
        self.assertNotIn("<script>", html)
        self.assertNotIn("javascript:", html)
        self.assertNotIn("<img src=x", html)
        self.assertNotIn("<svg onload", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("cover-fallback", html)     # 恶意封面被丢弃 -> 占位块

    def test_no_external_panel_links(self) -> None:
        """任何页面都不该出现外部面板地址 / "管理面板" 字样(用户端彻底独立)."""
        for path in ("/", "/about", "/edit", "/stats"):
            page = self.body(path)
            self.assertNotIn("管理面板", page, path)
            self.assertNotIn("8787", page, path)
            self.assertNotIn("admin-link", page, path)
            self.assertNotIn("admin_url", page, path)
        self.assertFalse(hasattr(self.site, "admin_url"))
        self.assertFalse(hasattr(self.site, "edit_url"))

    def test_static_and_cover_routes(self) -> None:
        css = self.render("/static/public.css")
        self.assertEqual(css.status, 200)
        self.assertIn("text/css", css.content_type)
        self.assertIn(b"--bg:", css.body)
        self.assertEqual(self.render("/favicon.ico").status, 200)
        self.assertEqual(self.render("/static/../server.py").status, 404)
        self.assertEqual(self.render("/static/nope.css").status, 404)

        cover = self.render("/cover/" + T.quote_segment(self.fixture.keys["gamma"]))
        self.assertEqual(cover.status, 200)
        self.assertEqual(cover.content_type, "image/png")
        self.assertEqual(cover.body, TINY_PNG)
        # **政策: 封面不得 hotlink 到源站**。旧断言固化的是被废止的行为
        # (未命中本地缓存就 302 到源站), 用户明确要求"仅限本地缓存或外接对象存储"。
        # 这张 fixture 只有远程 URL 且源站取不到 -> 允许 404/占位图, 但绝不能是 302。
        remote = self.render("/cover/" + T.quote_segment(self.fixture.keys["alpha"]))
        self.assertNotEqual(remote.status, 302, "封面不得 302 到源站(hotlink)")
        self.assertNotIn("example.com", remote.headers.get("Location", ""))
        for evil in ("../../config/config.toml", "..%2F..%2Fconfig.toml", "C:/Windows/win.ini"):
            self.assertEqual(self.render("/cover/" + evil.replace("/", "%2F")).status, 404, evil)

    def test_search_route_redirects(self) -> None:
        resp = self.render("/search", {"q": "東方"})
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.headers["Location"], "/albums?q=" + urllib.parse.quote("東方"))
        resp2 = self.render("/search")
        self.assertEqual(resp2.headers["Location"], "/albums")

    def test_data_error_renders_500_page(self) -> None:
        """库读不动时给一个 500 页面, 而不是抛穿到 socket 层."""
        broken = PublicData(self.fixture.store, root=self.fixture.root,
                            covers_dir=self.fixture.covers)

        def boom(**kwargs: object) -> None:
            raise sqlite3.OperationalError("boom")

        broken.events = boom  # type: ignore[method-assign]
        site = make_site(self.fixture)
        site.data = broken
        resp = site.render("/albums")
        self.assertEqual(resp.status, 500)
        self.assertIn("读取数据失败", resp.body.decode("utf-8"))
        self.assertIn("<!doctype html>", resp.body.decode("utf-8"))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class TestHttpServer(DataCase):
    """真起一个服务(端口 0), 用 urlopen 打所有页面."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from dnh.public.site import make_server

        cls.site = make_site(cls.fixture)
        cls.httpd = make_server(cls.site, host="127.0.0.1", port=0)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.httpd.server_address[:2]
        cls.base = f"http://127.0.0.1:{port}"
        cls.opener = urllib.request.build_opener(NoRedirect)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        super().tearDownClass()

    def fetch(self, path: str, *, raw: bool = False):
        return urllib.request.urlopen(self.base + path, timeout=10)

    def test_all_pages_200(self) -> None:
        paths = [
            "/", "/albums", "/albums?q=Pager&page=2&per=12", "/circles", "/circles?q=pager",
            "/events", "/tags", "/stats", "/about",
            "/album/" + urllib.parse.quote(self.fixture.keys["alpha"], safe=""),
            "/album/" + urllib.parse.quote(self.fixture.keys["evil"], safe=""),
            "/circle/" + urllib.parse.quote("ROUNDEX RECORD", safe=""),
            "/event/" + urllib.parse.quote("M3-2026春", safe=""),
            "/event/" + urllib.parse.quote("M3-2026春", safe="") + "?group=date",
            "/tag/" + urllib.parse.quote("東方Project", safe=""),
        ]
        for path in paths:
            with self.subTest(path=path):
                with self.fetch(path) as resp:
                    body = resp.read()
                    self.assertEqual(resp.status, 200)
                    self.assertIn("<!doctype html>", body.decode("utf-8"))
                    self.assertIn("text/html", resp.headers["Content-Type"])
        with self.fetch("/favicon.ico") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("image/svg+xml", resp.headers["Content-Type"])
            self.assertIn(b"<svg", resp.read())
        with self.fetch("/static/public.css") as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/css", resp.headers["Content-Type"])
            self.assertIn(b"--bg:", resp.read())
        # 匿名访问工作队列 -> 403(不再渲染页面)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.fetch("/edit")
        self.assertEqual(ctx.exception.code, 403)

    def test_security_headers(self) -> None:
        with self.fetch("/") as resp:
            self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(resp.headers["Referrer-Policy"], "no-referrer")
            csp = resp.headers["Content-Security-Policy"]
            self.assertIn("default-src 'none'", csp)
            # 现在有一份同源脚本(public.js): 只允许 'self', 不给 unsafe-inline
            self.assertIn("script-src 'self'", csp)
            script_part = csp.split("script-src")[1].split(";")[0]
            self.assertNotIn("unsafe-inline", script_part)
            self.assertIn("img-src 'self' https: data:", csp)
            self.assertEqual(resp.headers["Cache-Control"], "no-store")

    def test_cover_bytes_and_redirect(self) -> None:
        with self.fetch("/cover/" + urllib.parse.quote(self.fixture.keys["gamma"], safe="")) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["Content-Type"], "image/png")
            self.assertEqual(resp.read(), TINY_PNG)
        # 同上: 远程图不得 302 到源站。允许 200(拉取成功/占位) 或 404(取不到)。
        try:
            with self.fetch("/cover/" +
                            urllib.parse.quote(self.fixture.keys["alpha"], safe="")) as resp:
                self.assertNotEqual(resp.status, 302, "封面不得 302 到源站(hotlink)")
                self.assertNotIn("example.com", resp.headers.get("Location", ""))
        except urllib.error.HTTPError as exc:
            self.assertNotEqual(exc.code, 302, "封面不得 302 到源站(hotlink)")
            self.assertNotIn("example.com", exc.headers.get("Location", ""))

    def test_write_methods_rejected_without_editor(self) -> None:
        """未登录 -> 非 GET/HEAD 一律 403(不再是 405)."""
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                req = urllib.request.Request(self.base + "/albums", data=b"{}", method=method)
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=10)
                self.assertEqual(ctx.exception.code, 403)
                body = ctx.exception.read().decode("utf-8")
                self.assertIn("需要编辑者身份", body)

    def test_head_request_has_no_body(self) -> None:
        req = urllib.request.Request(self.base + "/albums", method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")
            self.assertGreater(int(resp.headers["Content-Length"]), 0)

    def test_404_and_traversal_over_http(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.fetch("/no-such-page")
        self.assertEqual(ctx.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as ctx2:
            self.fetch("/static/..%2F..%2Fconfig%2Fconfig.toml")
        self.assertEqual(ctx2.exception.code, 404)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()