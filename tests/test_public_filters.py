"""浏览页筛选递进(展会类型 → 具体展会次 → 年份) + 标签规则 的测试.

两条主线:

1. **筛选递进**: 选项按"展会类型"收窄; 选定具体展会次后不再渲染年份;
   旧 URL(``?event=`` / ``?year=`` / ``?kind=``)全部保持兼容, 且
   ``event`` 与 ``year`` 同时出现时以 ``event`` 为准;
2. **标签**: 展示层用 :mod:`dddb.tagrules` 的共享判据过滤"没有检索意义"的标签
   (规则本体不在这里定义 —— 这里只断言它的**行为**, 避免两份词表分叉)。

测试库是临时 SQLite(经 :class:`dddb.db.Store` 建), 不碰真实库。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from dddb.db import Store
from dddb.models import Album
from dddb.public import auth
from dddb.public import templates as T
from dddb.public.data import PublicData, ReadOnlyStore, parse_filters
from dddb.public.site import PublicSite
from dddb.public.users import UserStore
from dddb.tagrules import USELESS_TAG_RULES, is_useless_tag
from tests._tmp import temp_dir

#: 筛选面板里三个下拉的先后顺序(递进)
FIELD_ORDER = ('name="kind"', 'name="event"', 'name="year"')

#: 共享规则判定为"无用"的样例(逐类)
USELESS_SAMPLES = (
    # ① 展会名 / 会次 —— 结构化字段 event 已存
    "M3-2026秋", "M3-2026春", "M3-57", "C108", "例大祭", "M3-2026秋 (Resale)",
    # ② 纯年份 —— 结构化字段 year 已存
    "2026", "1999",
    # ③ 平台/载体/格式 —— 不是内容属性
    "Download Code", "DL", "DTM", "Mini CD", "CD-R", "CD", "Digital", "HiRes",
    # ④ 虚拟歌姬 / 歌声合成 —— 不是流派
    "VOCALOID", "Synthesizer V", "初音ミク", "可不", "AIボーカル", "合成音声", "CeVIO",
    # ⑤ 杂词
    "誤登録",
)

#: 必须**保留**的真流派 / 题材 / 分级
USEFUL_SAMPLES = (
    "女性ボーカル", "インスト", "ポップス", "ロック", "Electronic", "J-Core",
    "Touhou", "R18", "Vocal", "Rock", "ジャズ", "DnB", "アレンジ",
)


# --------------------------------------------------------------------------
# fixture
# --------------------------------------------------------------------------


def make_album(title: str, **over: object) -> Album:
    album = Album(
        title=title,
        title_original=title,
        circle=str(over.get("circle", "")),
        release_date=str(over.get("release_date", "")),
        event=str(over.get("event", "")),
        tags=list(over.get("tags", []) or []),
        genres=list(over.get("genres", []) or []),
        source_ids={"test": title},
    )
    album.key = album.compute_key()
    return album


class FilterFixture:
    """4 张覆盖不同展会/类型/年份的专辑 + 一批"老库残留"的无用标签."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.db_path = root / "data" / "dddb.sqlite3"
        covers = root / "data" / "covers"
        covers.mkdir(parents=True, exist_ok=True)
        store = Store(self.db_path)
        albums = [
            make_album("秋の記憶", event="M3-2026秋", release_date="2026-10-25",
                       tags=["女性ボーカル", "Rock"], genres=["Rock"]),
            make_album("春の風", event="M3-2026春", release_date="2026-04-26",
                       tags=["ポップス"], genres=["Pop"]),
            make_album("夏の特典", event="C108", release_date="2026-08-16",
                       tags=["R18"], genres=["Vocal"]),
            make_album("古い名盤", release_date="2025-03-01", tags=["Classic"]),
        ]
        for album in albums:
            store.upsert_album(album)
        self.keys = {album.title: album.key for album in albums}
        # 手工塞回"老库残留"的无用标签 —— 采集端旧版本确实写过这些;
        # 新版 db.py 写库时会过滤, 所以这里是**直接 INSERT** 模拟历史数据。
        legacy = ("M3-2026秋", "2026", "Synthesizer V", "Download Code", "誤登録")
        for tag in legacy:
            store.execute(
                "INSERT OR IGNORE INTO album_tags(album_key, tag) VALUES (?, ?)",
                (self.keys["秋の記憶"], tag),
            )
        store.close()
        self.data = PublicData(ReadOnlyStore(self.db_path), root=root, covers_dir=covers)
        self.users = UserStore(root / "data" / "public_users.sqlite3")
        self.sessions = auth.SessionManager()
        self.site = PublicSite(self.data, users=self.users, sessions=self.sessions,
                               db_path=self.db_path)

    def page(self, path: str, query: dict | None = None) -> str:
        resp = self.site.handle("GET", path, query or {}, client_ip="127.0.0.1")
        return resp.body.decode("utf-8")

    def hits(self, path: str, query: dict | None = None) -> int:
        match = re.search(r"命中 <b>(\d+)</b>", self.page(path, query))
        return int(match.group(1)) if match else -1

    def status(self, path: str, query: dict | None = None) -> int:
        return self.site.handle("GET", path, query or {}, client_ip="127.0.0.1").status

    def close(self) -> None:
        self.site.close()
        self.users.close()


class FilterCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._ctx = temp_dir("filters-")
        cls.fixture = FilterFixture(cls._ctx.__enter__())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture.close()
        cls._ctx.__exit__(None, None, None)

    # -- 面板工具 -----------------------------------------------------------

    def panel(self, filters: dict | None = None) -> str:
        events = self.fixture.data.events()
        return T.filter_panel(
            filters or {},
            action="/albums",
            events=events,
            kinds=self.fixture.data.event_kinds(),
            years=["2026", "2025"],
            tags=self.fixture.data.tags(limit=60),
            sorts=(("updated", "最近更新"),),
        )

    @staticmethod
    def select_block(html: str, name: str) -> str:
        match = re.search(rf'<select name="{name}">(.*?)</select>', html, re.S)
        return match.group(1) if match else ""

    @classmethod
    def option_values(cls, html: str, name: str) -> list[str]:
        block = cls.select_block(html, name)
        return re.findall(r'<option value="([^"]*)"', block)

    @staticmethod
    def selected_value(html: str, name: str) -> str:
        match = re.search(rf'<select name="{name}">.*?<option value="([^"]*)" selected>', html, re.S)
        return match.group(1) if match else ""


# --------------------------------------------------------------------------
# 1. 筛选面板: 顺序 / 收窄 / 年份隐藏
# --------------------------------------------------------------------------


class TestFilterPanelProgression(FilterCase):
    def test_field_order_is_kind_event_year(self) -> None:
        html = self.panel()
        positions = [html.index(needle) for needle in FIELD_ORDER]
        self.assertEqual(positions, sorted(positions), "顺序应为 展会类型 → 具体展会次 → 年份")
        self.assertIn("展会类型", html)
        self.assertIn("具体展会次", html)

    def test_all_events_listed_without_kind(self) -> None:
        values = self.option_values(self.panel(), "event")
        self.assertEqual(values[0], "")                       # "全部"
        self.assertIn("M3-2026秋", values)
        self.assertIn("M3-2026春", values)
        self.assertIn("C108", values)

    def test_kind_narrows_event_options(self) -> None:
        html = self.panel({"kind": "m3"})
        values = self.option_values(html, "event")
        self.assertIn("M3-2026秋", values)
        self.assertIn("M3-2026春", values)
        self.assertNotIn("C108", values, "选了 M3 就不该出现 Comiket 场次")
        # 类型下拉本身仍然列出全部类型(否则用户换不回去)
        self.assertIn("comiket", self.option_values(html, "kind"))

    def test_kind_comiket_narrows_to_comiket_events(self) -> None:
        values = self.option_values(self.panel({"kind": "comiket"}), "event")
        self.assertIn("C108", values)
        self.assertNotIn("M3-2026秋", values)

    def test_mismatched_current_event_is_kept_for_url_compat(self) -> None:
        """``?kind=comiket&event=M3-2026秋``(手改/旧链接): 当前项不能被静默筛掉."""
        values = self.option_values(self.panel({"kind": "comiket", "event": "M3-2026秋"}), "event")
        self.assertEqual(values[1], "M3-2026秋")              # 当前项排在最前
        self.assertIn("C108", values)
        self.assertEqual(self.selected_value(
            self.panel({"kind": "comiket", "event": "M3-2026秋"}), "event"), "M3-2026秋")

    def test_year_field_hidden_once_event_is_chosen(self) -> None:
        html = self.panel({"event": "M3-2026秋"})
        self.assertNotIn('name="year"', html)
        self.assertNotIn("<span>年份</span>", html)
        # 其它筛选器不受影响
        self.assertIn('name="tag"', html)
        self.assertIn('name="sort"', html)

    def test_year_field_present_without_event(self) -> None:
        for filters in ({}, {"kind": "m3"}, {"year": "2026"}):
            html = self.panel(filters)
            self.assertIn('name="year"', html, filters)
        self.assertEqual(self.selected_value(self.panel({"year": "2026"}), "year"), "2026")

    def test_current_selections_are_marked(self) -> None:
        html = self.panel({"kind": "m3", "event": "M3-2026秋"})
        self.assertEqual(self.selected_value(html, "kind"), "m3")
        self.assertEqual(self.selected_value(html, "event"), "M3-2026秋")

    def test_narrow_helper_handles_empty_inputs(self) -> None:
        self.assertEqual(T.narrow_events_by_kind([], "m3"), [])
        self.assertEqual(T.narrow_events_by_kind([{"value": "X"}], ""), [{"value": "X"}])
        self.assertEqual(T.narrow_events_by_kind([{"value": "X", "kind": "m3"}], "comiket"), [])
        # 没写 kind 的老数据条目不会被误删(向后兼容)
        self.assertEqual(len(T.narrow_events_by_kind([{"value": "X"}, {"value": "Y"}], "m3")), 0)

    def test_empty_filter_lists_render_without_error(self) -> None:
        html = T.filter_panel({}, action="/albums")
        self.assertIn('name="kind"', html)
        self.assertIn('name="event"', html)
        self.assertIn('name="year"', html)
        self.assertIn("应用筛选", html)


# --------------------------------------------------------------------------
# 2. 查询参数兼容(event 优先于 year)
# --------------------------------------------------------------------------


class TestParseFiltersCompat(FilterCase):
    def test_event_wins_over_year(self) -> None:
        filters = parse_filters({"event": ["M3-2026秋"], "year": ["1999"]})
        self.assertEqual(filters["event"], "M3-2026秋")
        self.assertNotIn("year", filters)

    def test_year_kept_without_event(self) -> None:
        self.assertEqual(parse_filters({"year": ["2026"]})["year"], "2026")

    def test_kind_and_event_are_both_kept(self) -> None:
        filters = parse_filters({"kind": ["m3"], "event": ["M3-2026秋"]})
        self.assertEqual(filters, {"kind": "m3", "event": "M3-2026秋"})

    def test_unknown_and_blank_params_are_ignored(self) -> None:
        self.assertEqual(parse_filters({"bogus": ["x"], "year": ["  "]}), {})

    def test_old_urls_still_filter_albums(self) -> None:
        self.assertEqual(self.fixture.hits("/albums", {"year": ["2026"]}), 3)
        self.assertEqual(self.fixture.hits("/albums", {"event": ["M3-2026秋"]}), 1)
        self.assertEqual(self.fixture.hits("/albums", {"kind": ["comiket"]}), 1)

    def test_old_url_with_event_and_year_uses_event(self) -> None:
        with_year = self.fixture.hits("/albums", {"event": ["M3-2026秋"], "year": ["1999"]})
        self.assertEqual(with_year, 1, "year 必须被忽略, 否则老链接会查出 0 条")
        self.assertEqual(self.fixture.hits("/albums", {"event": ["M3-2026秋"]}), 1)

    def test_event_page_does_not_render_year_selector(self) -> None:
        html = self.fixture.page("/albums", {"event": ["M3-2026秋"]})
        self.assertNotIn('name="year"', html)
        self.assertIn("命中 <b>1</b>", html)

    def test_kind_page_narrows_event_selector_in_html(self) -> None:
        html = self.fixture.page("/albums", {"kind": ["m3"]})
        values = self.option_values(html, "event")
        self.assertIn("M3-2026秋", values)
        self.assertNotIn("C108", values)

    def test_unknown_filter_values_stay_200(self) -> None:
        self.assertEqual(self.fixture.status("/albums", {"event": ["不存在的展会"]}), 200)
        self.assertEqual(self.fixture.hits("/albums", {"event": ["不存在的展会"]}), 0)


# --------------------------------------------------------------------------
# 3. 标签规则(共享模块)与展示层过滤
# --------------------------------------------------------------------------


class TestTagRules(FilterCase):
    def test_shared_rules_module_exposes_categories(self) -> None:
        self.assertEqual(
            set(USELESS_TAG_RULES),
            {"event", "year", "format", "voice_synth", "misc"},
        )

    def test_useless_samples_are_rejected(self) -> None:
        for tag in USELESS_SAMPLES:
            self.assertTrue(is_useless_tag(tag), f"应当过滤: {tag}")

    def test_useful_samples_are_kept(self) -> None:
        for tag in USEFUL_SAMPLES:
            self.assertFalse(is_useless_tag(tag), f"不应当过滤: {tag}")

    def test_empty_and_hash_prefixed_tags(self) -> None:
        self.assertTrue(is_useless_tag(""))
        self.assertTrue(is_useless_tag("  "))
        self.assertTrue(is_useless_tag("#M3-2026秋"))
        self.assertFalse(is_useless_tag("#Rock"))

    def test_tags_list_filters_legacy_rows(self) -> None:
        values = {t["value"] for t in self.fixture.data.tags()}
        for tag in ("M3-2026秋", "2026", "Synthesizer V", "Download Code", "誤登録"):
            self.assertNotIn(tag, values)
        for tag in ("女性ボーカル", "Rock", "ポップス", "R18", "Classic"):
            self.assertIn(tag, values)

    def test_include_useless_shows_raw_rows_for_debug(self) -> None:
        raw = {t["value"] for t in self.fixture.data.tags(include_useless=True)}
        filtered = {t["value"] for t in self.fixture.data.tags()}
        self.assertIn("M3-2026秋", raw)
        self.assertIn("Synthesizer V", raw)
        self.assertTrue(filtered < raw, "过滤后应当是子集")

    def test_limit_applies_after_filtering(self) -> None:
        limited = self.fixture.data.tags(limit=2)
        self.assertEqual(len(limited), 2)
        self.assertTrue(all(not is_useless_tag(t["value"]) for t in limited))
        # 顺序按出现次数降序
        counts = [t["count"] for t in self.fixture.data.tags()]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_stats_tag_count_matches_visible_list(self) -> None:
        self.assertEqual(self.fixture.data.stats()["tags"], len(self.fixture.data.tags()))

    def test_tags_page_does_not_show_useless_tags(self) -> None:
        html = self.fixture.page("/tags")
        for tag in ("M3-2026秋", "Synthesizer V", "Download Code", "誤登録"):
            self.assertNotIn(f">{tag}<", html)
        self.assertIn("女性ボーカル", html)

    def test_useless_tag_url_still_works_for_old_links(self) -> None:
        """展示层过滤不该让 /tag/<旧标签> 404 —— 旧链接与搜索引擎仍能进来."""
        self.assertEqual(self.fixture.status("/tag/" + T.quote_segment("M3-2026秋")), 200)
        self.assertEqual(self.fixture.status("/tag/" + T.quote_segment("女性ボーカル")), 200)

    def test_tag_page_lists_matching_albums(self) -> None:
        html = self.fixture.page("/tag/" + T.quote_segment("女性ボーカル"))
        self.assertIn("秋の記憶", html)
        self.assertNotIn("春の風", html)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()