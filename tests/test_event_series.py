"""展会两级结构(系列 + 会次)测试.

覆盖用户全部要求:
  * 名称展示 "[缩写] 全称 (会次)"
  * 索引页两列卡片, 每卡默认 5 条 + 展开
  * 系列详情页: LOGO 大图 + MD wiki(图片/外链) + 会次图片网格
  * 编辑系列(上传图片 + MD wiki)
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests._tmp import temp_path

ROOT = Path(__file__).resolve().parents[1]


def _ctx(editor: bool = True):
    from dddb.public import templates as T

    return T.SiteContext(csrf_token="tok", is_editor=editor, lang="zh",
                         user={"id": 1, "username": "ed",
                               "role": "editor" if editor else "member"})


class TestNameFormat(unittest.TestCase):
    """用户给的 5 个示例必须逐字对上。"""

    CASES = (
        # M3 的系列全称是 Music Media-Mix Market(用户指定)
        ("M3-2026秋", "[M3] Music Media-Mix Market (M3-58)"),
        ("C108", "[CM] Comic Market (C108)"),
        ("例大祭21", "[例大祭] 博丽神社例大祭 (例大祭21)"),
        ("秋例13", "[例大祭] 博丽神社秋季例大祭 (秋例13)"),
        ("DM03", "[DM] DM (DM3)"),
    )

    def test_examples(self) -> None:
        from dddb.public.eventseries import display_name, parse_event_name

        for raw, expect in self.CASES:
            with self.subTest(raw=raw):
                info = parse_event_name(raw)
                got = display_name(info["abbr"], info["full_name"],
                                   info["vol_label"], raw)
                self.assertEqual(got, expect)

    def test_resale_suffix_ignored(self) -> None:
        from dddb.public.eventseries import parse_event_name

        info = parse_event_name("M3-2026秋(resale)")
        self.assertEqual(info["vol"], 58)

    def test_unknown_passthrough(self) -> None:
        from dddb.public.eventseries import display_name, parse_event_name

        info = parse_event_name("某个奇怪的展会")
        got = display_name(info["abbr"], info["full_name"],
                           info["vol_label"], "某个奇怪的展会")
        self.assertIn("某个奇怪的展会", got)


class TestSeriesStorage(unittest.TestCase):
    def setUp(self) -> None:
        from dddb.db import Store

        self.tmp = temp_path("series-store")
        self.db = self.tmp / "t.sqlite3"
        self.store = Store(self.db)

    def tearDown(self) -> None:
        import shutil

        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_series_crud(self) -> None:
        self.store.save_series("M3", abbr="M3", full_name="M3",
                               wiki="# M3", links={"官网": "https://m3.example.com/"})
        got = self.store.get_series("M3")
        self.assertIsNotNone(got)
        self.assertEqual(got["abbr"], "M3")
        self.assertEqual(got["wiki"], "# M3")
        self.assertEqual(got["links"], {"官网": "https://m3.example.com/"})

    def test_events_of_series(self) -> None:
        self.store.save_series("M3", abbr="M3", full_name="M3")
        self.store.save_event("M3-2026秋", series="M3", vol=58,
                              vol_label="M3-58")
        self.store.save_event("M3-2026春", series="M3", vol=57,
                              vol_label="M3-57")
        self.store.save_event("C108", series="Comic Market", vol=108,
                              vol_label="C108")
        evs = self.store.events_of_series("M3")
        self.assertEqual(len(evs), 2)
        # 会次号倒序
        self.assertEqual(evs[0]["name"], "M3-2026秋")
        self.assertEqual(evs[0]["vol"], 58)

    def test_delete_series_keeps_events(self) -> None:
        self.store.save_series("M3", abbr="M3")
        self.store.save_event("M3-2026秋", series="M3", vol=58)
        self.store.delete_series("M3")
        self.assertIsNone(self.store.get_series("M3"))
        # 会次还在, 只是 series 清空
        names = [e["name"] for e in self.store.list_events()]
        self.assertIn("M3-2026秋", names)

    def test_migration_adds_columns(self) -> None:
        cols = {r["name"] for r in self.store.query(
            "PRAGMA table_info(events)")}
        for name in ("series", "vol", "vol_label"):
            self.assertIn(name, cols, f"events 缺 {name}")


class TestIndexCards(unittest.TestCase):
    def _series(self, n_editions: int = 7):
        editions = [
            {"value": f"M3-2026-{i}", "display": f"[M3] 第{i}回 (M3-{i})",
             "count": i} for i in range(n_editions)
        ]
        return [{"name": "M3", "abbr": "M3", "full_name": "M3", "logo": "",
                 "has_logo": False, "editions": editions}]

    def test_two_column_grid(self) -> None:
        from dddb.public import templates as T

        html = T.events_index_page(_ctx(), events=[], kinds=[],
                                   series=self._series())
        self.assertIn("series-grid", html)
        css = (ROOT / "dddb/public/static/public.css").read_text(encoding="utf-8")
        self.assertIn("grid-template-columns: repeat(2", css.split(".series-grid")[1][:200])

    def test_card_has_logo_title_editions(self) -> None:
        from dddb.public import templates as T

        html = T.events_index_page(_ctx(), events=[], kinds=[],
                                   series=self._series())
        self.assertIn("series-card-logo", html)
        self.assertIn("[M3] M3", html)
        self.assertEqual(html.count('class="series-edition"'), 7)

    def test_expand_button_when_over_five(self) -> None:
        from dddb.public import templates as T

        html = T.events_index_page(_ctx(), events=[], kinds=[],
                                   series=self._series(7))
        self.assertIn("data-series-more", html)
        self.assertIn("展开 2 条", html)

    def test_no_expand_when_five_or_less(self) -> None:
        from dddb.public import templates as T

        html = T.events_index_page(_ctx(), events=[], kinds=[],
                                   series=self._series(4))
        self.assertNotIn("data-series-more", html)

    def test_five_visible_by_css(self) -> None:
        """默认只露 5 条靠 CSS max-height 实现。"""
        css = (ROOT / "dddb/public/static/public.css").read_text(encoding="utf-8")
        block = css.split(".series-editions {")[1].split("}")[0]
        self.assertIn("max-height", block)
        self.assertIn("overflow: hidden", block)
        open_block = css.split(".series-editions.is-open {")[1].split("}")[0]
        self.assertIn("overflow-y: auto", open_block)


class TestSeriesDetail(unittest.TestCase):
    def _series(self):
        return {"name": "M3", "abbr": "M3", "full_name": "M3",
                "logo": "https://e.com/m3.png", "poster": "",
                "wiki": "# M3\n\n正文 **粗体**\n\n"
                        "![图](https://e.com/h.jpg)\n\n"
                        "[官网](https://m3.example.com/)"}

    def test_hero_and_wiki(self) -> None:
        from dddb.public import templates as T

        html = T.event_series_page(_ctx(), series=self._series())
        self.assertIn("series-hero", html)
        self.assertIn("md-body", html)
        self.assertIn("<h2>M3</h2>", html)
        self.assertIn("<strong>粗体</strong>", html)
        self.assertIn('class="md-img"', html)
        self.assertIn('href="https://m3.example.com/"', html)

    def test_edition_image_grid(self) -> None:
        from dddb.public import templates as T

        html = T.event_series_page(_ctx(), series=self._series(), editions=[
            {"value": "M3-2026秋", "display": "[M3] M3-2026秋 (M3-58)",
             "count": 100, "logo": "https://e.com/a.png"},
            {"value": "M3-2026春", "display": "[M3] M3-2026春 (M3-57)",
             "count": 50, "logo": ""},
        ])
        self.assertIn("edition-grid", html)
        self.assertEqual(html.count('class="edition-card"'), 2)
        self.assertIn("/event/M3-2026%E7%A7%8B", html)
        # 没图的会次用文字占位
        self.assertIn("edition-thumb-text", html)

    def test_edit_button_for_editor_only(self) -> None:
        from dddb.public import templates as T

        editor_html = T.event_series_page(_ctx(True), series=self._series(),
                                          can_edit=True)
        self.assertIn("编辑系列", editor_html)
        member_html = T.event_series_page(_ctx(False), series=self._series(),
                                          can_edit=False)
        self.assertNotIn("编辑系列", member_html)

    def test_routes_registered(self) -> None:
        from dddb.public import site as SITE

        # event-series 用基础名, 分支再细分 /<name> /<name>/edit /new
        self.assertEqual(SITE._ROUTE_MAP.get("event-series"),
                         "event_series")
        self.assertEqual(SITE._ROUTE_MAP.get("event-series-logo"),
                         "event_series_logo")
        for fn in ("page_event_series", "serve_series_image",
                   "page_event_series_edit", "_do_series_save",
                   "_save_series_image"):
            self.assertTrue(hasattr(SITE.PublicSite, fn), f"缺 {fn}")


class TestSeriesEdit(unittest.TestCase):
    def test_image_upload_fields(self) -> None:
        from dddb.public import templates as T

        html = T.event_series_edit_page(_ctx(), series={"name": "M3"})
        self.assertIn("multipart/form-data", html)
        self.assertIn('name="logo"', html)
        self.assertIn('name="poster"', html)
        self.assertIn('type="file"', html)

    def test_wiki_and_links_fields(self) -> None:
        from dddb.public import templates as T

        html = T.event_series_edit_page(_ctx(), series={"name": "M3"})
        self.assertIn('name="wiki"', html)
        self.assertIn('name="links"', html)
        self.assertIn("Markdown", html)

    def test_creating_mode(self) -> None:
        from dddb.public import templates as T

        html = T.event_series_edit_page(_ctx(), series={}, creating=True)
        self.assertIn('action="/event-series/new"', html)


class TestMarkdown(unittest.TestCase):
    def test_images_and_links(self) -> None:
        from dddb.public.markdown import markdown_to_html

        out = markdown_to_html(
            "![说明](https://e.com/a.png)\n\n[文字](https://e.com/)\n\n"
            "**粗** *斜* `码`")
        self.assertIn('<img class="md-img" src="https://e.com/a.png"', out)
        self.assertIn('<a href="https://e.com/"', out)
        self.assertIn("<strong>粗</strong>", out)
        self.assertIn("<em>斜</em>", out)
        self.assertIn("<code>码</code>", out)

    def test_escapes_html(self) -> None:
        from dddb.public.markdown import markdown_to_html

        out = markdown_to_html('<script>alert(1)</script>')
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_rejects_dangerous_urls(self) -> None:
        from dddb.public.markdown import markdown_to_html

        out = markdown_to_html("[x](javascript:alert(1))")
        self.assertNotIn("javascript:", out)
        out2 = markdown_to_html("![x](data:text/html,evil)")
        self.assertNotIn("src=\"data:", out2)

    def test_headings_and_lists(self) -> None:
        from dddb.public.markdown import markdown_to_html

        out = markdown_to_html("# 标题\n\n- a\n- b\n\n1. x\n2. y")
        self.assertIn("<h2>标题</h2>", out)
        self.assertIn("<ul>", out)
        self.assertIn("<ol>", out)


class TestExpandJs(unittest.TestCase):
    def test_setup_registered(self) -> None:
        js = (ROOT / "dddb/public/static/public.js").read_text(encoding="utf-8")
        self.assertIn("function setupSeriesCards(", js)
        boot = js.split("function boot()")[1].split("\n  }")[0]
        self.assertIn("setupSeriesCards()", boot, "展开逻辑要在 boot 里注册")
        self.assertIn("is-open", js)


if __name__ == "__main__":
    unittest.main()
