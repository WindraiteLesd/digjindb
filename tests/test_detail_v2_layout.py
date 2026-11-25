"""测试: 详情页改版(按钮上移/社团合并/曲目展开)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _album(**overrides):
    base = {
        "key": "k", "href": "/album/k", "title": "测试专辑",
        "title_original": "测试专辑", "title_alt": "", "romaji": "",
        "circle": "测试社团", "circle_href": "/circle/x",
        "artists": ["ARForest"], "credits": [], "urls": [], "tags": [],
        "genres": [],
        "tracks": [
            {"disc": 1, "index": 1, "title": "曲1-1", "artists": ["A"],
             "duration": 100, "duration_text": "1:40", "lang": "日语",
             "genre": "Trance"},
            {"disc": 1, "index": 2, "title": "曲1-2", "artists": [],
             "duration": 200, "duration_text": "3:20"},
        ],
        "date": "", "event": "", "catalog_no": "", "price": "", "hall": "",
        "booth": "", "description": "", "missing": [], "track_count": 2,
    }
    base.update(overrides)
    return base


def _detail(album=None, **kwargs):
    from dddb.public import templates as T

    ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                        user={"id": 1, "username": "ed", "role": "editor"})
    kwargs.setdefault("can_edit", True)
    kwargs.setdefault("entities", [
        {"kind": "artist", "name": "ARForest",
         "href": "/albums?q=ARForest", "following": False},
    ])
    return T.album_detail_page(
        ctx, album=album or _album(), **kwargs)


class TestActionsInTitleRow(unittest.TestCase):
    """编辑/修改记录按钮跟「新建专辑」同一排(用户 m13899)。"""

    def setUp(self) -> None:
        self.html = _detail()

    def test_actions_in_title_row(self) -> None:
        """按钮在标题行的右侧组里, 不再单独占一行。"""
        self.assertIn('class="title-actions title-actions-end"', self.html)
        self.assertNotIn('class="detail-actions"', self.html)

    def test_actions_before_card(self) -> None:
        pos_act = self.html.find("title-actions-end")
        pos_card = self.html.find('<div class="detail">')
        self.assertGreaterEqual(pos_act, 0)
        self.assertLess(pos_act, pos_card, "按钮要在卡片之前")

    def test_buttons_share_title_row_with_new_album(self) -> None:
        """左侧「新建专辑」与右侧两个按钮属于同一行, 样式同为 btn-ghost btn-xs。"""
        pos_title = self.html.find('<div class="title-row">')
        pos_actions = self.html.find('class="title-actions title-actions-end"')
        self.assertGreaterEqual(pos_title, 0)
        self.assertGreater(pos_actions, pos_title)
        seg = self.html[pos_actions:self.html.find("</span>", pos_actions)]
        self.assertIn("编辑这张专辑", seg)
        self.assertIn("修改记录", seg)
        self.assertIn("btn-ghost", seg)
        self.assertIn("btn-xs", seg)

    def test_both_buttons_present(self) -> None:
        self.assertIn("编辑这张专辑", self.html)
        self.assertIn("修改记录", self.html)

    def test_no_actions_when_not_editor(self) -> None:
        html = _detail(can_edit=False)
        self.assertNotIn("编辑这张专辑", html)


class TestCircleInMergedSection(unittest.TestCase):
    """社团显示在合并区块, 且排第一."""

    def setUp(self) -> None:
        self.html = _detail()

    def test_circle_not_in_people(self) -> None:
        """社团不进制作人员区块."""
        m = re.search(r'<div class="people-chips">([\s\S]*?)</div>\s*</section>',
                      self.html)
        if m:
            self.assertNotIn("社团", m.group(1))

    def test_circle_after_title(self) -> None:
        """社团显示在标题后."""
        self.assertIn('class="detail-circle"', self.html)
        self.assertGreater(self.html.find("detail-circle"),
                           self.html.find("detail-title"))

    def test_circle_name_visible(self) -> None:
        self.assertIn("测试社团", self.html)

    def test_no_explanatory_note(self) -> None:
        self.assertNotIn("把光标移到名字上", self.html,
                         "解释说明已删除")
        self.assertNotIn("艺人与社团 ·", self.html)


class TestTrackExpansion(unittest.TestCase):
    """曲目点击展开显示语种/曲风(有值才显示)."""

    def setUp(self) -> None:
        self.html = _detail()

    def test_toggle_only_when_has_extra(self) -> None:
        """只有带补充信息的行才有展开按钮."""
        self.assertEqual(self.html.count("track-toggle"), 1,
                         "只有 1 行有语种/曲风")
        self.assertEqual(self.html.count('data-track-expand="1"'), 1)

    def test_extra_hidden_by_default(self) -> None:
        self.assertIn("track-extra-row", self.html)
        for m in re.finditer(r'<tr class="track-extra-row"([^>]*)>', self.html):
            self.assertIn("hidden", m.group(1), "展开区默认隐藏")

    def test_shows_lang_and_genre(self) -> None:
        self.assertIn("日语", self.html)
        self.assertIn("Trance", self.html)

    def test_extra_has_labels(self) -> None:
        m = re.search(r'<div class="track-extra">([\s\S]*?)</div>', self.html)
        self.assertIsNotNone(m)
        inner = m.group(1)
        self.assertIn("语种", inner)
        self.assertIn("曲风", inner)

    def test_no_extra_row_when_no_data(self) -> None:
        """没有补充信息的曲目不该有展开区."""
        album = _album(tracks=[
            {"disc": 1, "index": 1, "title": "纯曲名", "artists": [],
             "duration": 100, "duration_text": "1:40"},
        ])
        html = _detail(album)
        self.assertNotIn("track-toggle", html)
        self.assertNotIn("track-extra-row", html)

    def test_subtitle_and_translation(self) -> None:
        album = _album(tracks=[
            {"disc": 1, "index": 1, "title": "曲", "artists": [],
             "duration": 100, "duration_text": "1:40",
             "subtitle": "副标题X", "title_zh": "译名Y"},
        ])
        html = _detail(album)
        self.assertIn("副标题X", html)
        self.assertIn("译名Y", html)


class TestDiscVisibleInDetail(unittest.TestCase):
    """分碟时显示 disc 号."""

    def test_disc_subtitles(self) -> None:
        html = _detail(_album(tracks=[
            {"disc": 1, "index": 1, "title": "A", "artists": [],
             "duration": 100, "duration_text": "1:40"},
            {"disc": 2, "index": 1, "title": "B", "artists": [],
             "duration": 200, "duration_text": "3:20"},
        ]))
        self.assertEqual(re.findall(r"Disc (\d+)", html), ["01", "02"])
        self.assertIn("1-1", html)
        self.assertIn("2-1", html)

    def test_single_disc_no_subtitle(self) -> None:
        """单碟不显示碟标题, 序号只显示曲号(不是 碟-曲)."""
        html = _detail()
        self.assertNotIn("disc-subtitle", html)
        # 取曲目表里第一行的序号单元格
        m = re.search(
            r'<table class="tracklist"[\s\S]*?<td class="num">([^<]*)</td>',
            html)
        self.assertIsNotNone(m, "找不到曲目序号")
        number = m.group(1).strip()
        self.assertEqual(number, "1", "单碟只显示曲号")
        self.assertNotIn("-", number)


class TestJsAndCss(unittest.TestCase):
    def test_js_has_expand_handler(self) -> None:
        js = (ROOT / "dddb/public/static/public.js").read_text(
            encoding="utf-8")
        self.assertIn("function setupTrackExpand(", js)
        self.assertIn("setupTrackExpand()", js)
        self.assertIn("track-extra-row", js)

    def test_css_has_styles(self) -> None:
        css = (ROOT / "dddb/public/static/public.css").read_text(
            encoding="utf-8")
        for token in (".title-actions-end", ".track-toggle", ".track-extra",
                      ".track-extra-row"):
            self.assertIn(token, css, f"缺样式 {token}")
        # 旧的独立操作条已经并进标题行, 样式也清掉了
        self.assertNotIn(".detail-actions", css)


if __name__ == "__main__":
    unittest.main()
