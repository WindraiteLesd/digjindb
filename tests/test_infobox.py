"""dddb infobox: 导出 / 解析 / 线上弹窗.

用户要求:
    "把缓存的包括封面在内的所有线上需要的元数据导出为线上 infobox 格式，
     允许在线上新建专辑页面中放置一个编辑infobox按钮，会直接弹出一个
     输入框，复制现在的 dddb infobox 进去就可以一键填入所有词条，
     并在输入框下方额外放置帮助按钮弹出二级窗口的格式说明，
     编辑专辑页也可以点击按钮查看专辑的 infobox 并进行修改"
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IB_JS = ROOT / "dddb/public/static/infobox.js"
TPL = ROOT / "dddb/public/templates.py"
CSS = ROOT / "dddb/public/static/public.css"
SITE = ROOT / "dddb/public/site.py"


DRAFT = {
    "key": "Winter Bouquet|BLOOD SNOW|2026",
    "meta": {
        "title_original": "BLOOD SNOW", "title": "血雪",
        "title_romaji": "Blood Snow", "circle": "Winter Bouquet",
        "artists": ["Winter Bouquet", "N_dog"],
        "event": "M3-2026秋", "release_date": "2026-10-25",
        "catalog_no": "WB-001", "price": "1100",
        "hall": "第一展示場", "booth": "A-01a",
        "tags": ["东方", "同人"], "genres": ["J-Pop"],
        "description": "第一段\n第二段",
    },
    "cover_url": "https://e.com/c.jpg",
    "urls": {"官网": "https://example.com/"},
    "credits": [
        {"role": "compose", "name": "某人", "original": "Someone"},
        {"role": "instrument:Piano", "name": "某丙"},
    ],
    "tracks": [
        {"index": 1, "title": "Luciphobia", "duration": 237,
         "artists": ["MALVA."]},
        {"index": 2, "title": "Hallucination", "duration": 252},
    ],
    "warnings": [], "unrecognized_roles": [],
}


class TestBuild(unittest.TestCase):
    def test_mark_present(self) -> None:
        from dddb import infobox

        self.assertIn("[dddb]", infobox.build(DRAFT))

    def test_single_fields(self) -> None:
        from dddb import infobox

        text = infobox.build(DRAFT)
        for name in ("title_original", "circle", "event", "release_date",
                     "catalog_no", "price", "hall", "booth"):
            self.assertIn("{} || ".format(name), text, f"缺 {name}")

    def test_cover_included(self) -> None:
        from dddb import infobox

        self.assertIn("cover || https://e.com/c.jpg", infobox.build(DRAFT))

    def test_local_cover_prefixed(self) -> None:
        from dddb import infobox

        d = dict(DRAFT)
        d["cover_url"] = ""
        d["cover_file"] = "/tmp/x.jpg"
        self.assertIn("cover || local:/tmp/x.jpg", infobox.build(d))

    def test_credits_with_original(self) -> None:
        from dddb import infobox

        text = infobox.build(DRAFT)
        self.assertIn("credits || compose: 某人 (Someone)", text)

    def test_tracks_with_duration_and_artist(self) -> None:
        from dddb import infobox

        text = infobox.build(DRAFT)
        self.assertIn("tracks || 1. Luciphobia [3:57] / MALVA.", text)
        self.assertIn("tracks || 2. Hallucination [4:12]", text)

    def test_multiline_description(self) -> None:
        from dddb import infobox

        text = infobox.build(DRAFT)
        self.assertIn("description || 第一段", text)
        self.assertIn("|| 第二段", text)


class TestParse(unittest.TestCase):
    def test_roundtrip_single(self) -> None:
        from dddb import infobox

        back = infobox.parse(infobox.build(DRAFT))
        self.assertEqual(back["key"], DRAFT["key"])
        for name in ("title_original", "title", "circle", "event",
                     "release_date", "catalog_no", "price", "hall", "booth"):
            self.assertEqual(back["meta"][name], DRAFT["meta"][name],
                             f"{name} 往返不一致")

    def test_roundtrip_lists(self) -> None:
        from dddb import infobox

        back = infobox.parse(infobox.build(DRAFT))
        self.assertEqual(back["meta"]["artists"], ["Winter Bouquet", "N_dog"])
        self.assertEqual(back["meta"]["tags"], ["东方", "同人"])

    def test_roundtrip_cover(self) -> None:
        from dddb import infobox

        back = infobox.parse(infobox.build(DRAFT))
        self.assertEqual(back["cover_url"], "https://e.com/c.jpg")

    def test_roundtrip_credits(self) -> None:
        from dddb import infobox

        back = infobox.parse(infobox.build(DRAFT))
        self.assertEqual(len(back["credits"]), 2)
        self.assertEqual(back["credits"][0]["name"], "某人")
        self.assertEqual(back["credits"][0]["original"], "Someone")
        # 职位里带冒号的不能被切断
        self.assertEqual(back["credits"][1]["role"], "instrument:Piano")

    def test_roundtrip_tracks(self) -> None:
        from dddb import infobox

        back = infobox.parse(infobox.build(DRAFT))
        self.assertEqual(len(back["tracks"]), 2)
        self.assertEqual(back["tracks"][0]["duration"], 237)
        self.assertEqual(back["tracks"][0]["artists"], ["MALVA."])

    def test_roundtrip_description(self) -> None:
        from dddb import infobox

        back = infobox.parse(infobox.build(DRAFT))
        self.assertEqual(back["meta"]["description"], "第一段\n第二段")

    def test_roundtrip_links(self) -> None:
        from dddb import infobox

        back = infobox.parse(infobox.build(DRAFT))
        self.assertEqual(back["urls"].get("官网"), "https://example.com/")

    def test_ignores_garbage(self) -> None:
        from dddb import infobox

        back = infobox.parse("随便说点什么\n没格式的行\n[dddb]\ntitle_original || T\n")
        self.assertEqual(back["meta"]["title_original"], "T")

    def test_auto_key_when_missing(self) -> None:
        from dddb import infobox

        back = infobox.parse("[dddb]\ntitle_original || T\ncircle || C\n")
        self.assertEqual(back["key"], "C|T")

    def test_warns_missing(self) -> None:
        from dddb import infobox

        back = infobox.parse("[dddb]\ntitle_original || T\n")
        self.assertIn("缺发售日", back["warnings"])
        self.assertIn("缺展会", back["warnings"])

    def test_help_text_mentions_format(self) -> None:
        from dddb import infobox

        for token in ("||", "credits", "tracks", "links", "[dddb]"):
            self.assertIn(token, infobox.HELP_TEXT, f"帮助里没讲 {token}")


class TestOnlineWidget(unittest.TestCase):
    def _html(self):
        from dddb.public import templates as T

        ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                            user={"id": 1, "username": "e", "role": "editor"})
        album = {"key": "A|B|2026", "title_original": "B", "has_cover": False,
                 "cover_local": "", "cover": "", "meta": {}}
        return T.edit_album_page(ctx, album=album,
                                 values=T.edit_values_from_album(album),
                                 creating=False)

    def test_button_present(self) -> None:
        html = self._html()
        self.assertIn("编辑 infobox", html)
        self.assertIn("data-infobox-open", html)

    def test_textarea_present(self) -> None:
        html = self._html()
        self.assertIn("data-infobox-text", html)

    def test_apply_button(self) -> None:
        self.assertIn("data-infobox-apply", self._html())

    def test_help_button_and_second_modal(self) -> None:
        html = self._html()
        self.assertIn("data-infobox-help", html)
        self.assertIn("data-infobox-help-modal", html,
                      "帮助要弹**二级**窗口")

    def test_help_text_rendered(self) -> None:
        html = self._html()
        self.assertIn("infobox-help-text", html)
        self.assertIn("credits", html)

    def test_script_included(self) -> None:
        self.assertIn("infobox.js", self._html())

    def test_css_present(self) -> None:
        css = CSS.read_text(encoding="utf-8")
        for token in (".infobox-modal", ".infobox-panel", ".infobox-text",
                      ".infobox-help-text"):
            self.assertIn(token, css, f"缺样式 {token}")

    def test_static_whitelist(self) -> None:
        src = SITE.read_text(encoding="utf-8")
        self.assertIn("infobox.js", src, "静态白名单没加")


class TestFrontendParsing(unittest.TestCase):
    """前端也要能解析(粘贴后立刻填入)。"""

    def test_has_parser(self) -> None:
        js = IB_JS.read_text(encoding="utf-8")
        self.assertIn("function parseInfobox(", js)
        self.assertIn("function applyInfobox(", js)

    def test_fills_all_field_groups(self) -> None:
        js = IB_JS.read_text(encoding="utf-8")
        for name in ("title_original", "circle", "event", "release_date",
                     "artists", "tags", "genres", "description"):
            self.assertIn('"{}"'.format(name), js, f"没填 {name}")
        for picker in ("url_key", "credit_role", "track_paste"):
            self.assertIn(picker, js, f"没处理 {picker}")

    def test_exposes_global_hook(self) -> None:
        """站长端自动填入要能调。"""
        js = IB_JS.read_text(encoding="utf-8")
        self.assertIn("window.dddbApplyInfobox", js)

    def test_multiline_description(self) -> None:
        js = IB_JS.read_text(encoding="utf-8")
        self.assertIn("descParts", js)


class TestNoRegression(unittest.TestCase):
    def test_photo_still_works(self) -> None:
        src = TPL.read_text(encoding="utf-8")
        self.assertIn("_photos_html", src)
        self.assertIn("data-cover-token", src)


if __name__ == "__main__":
    unittest.main()
