"""测试: 详情页布局重排 + 词条/外链增删 + 分碟可见 + 折叠."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _album(**overrides):
    base = {
        "key": "k", "title": "测试专辑", "title_original": "测试专辑",
        "title_alt": "", "romaji": "", "circle": "测试社团",
        "circle_href": "/circle/x", "artists": ["艺人甲", "艺人乙"],
        "credits": [
            {"role": "作曲", "name": "甲", "label": "作曲", "names": ["甲"]},
            {"role": "编曲", "name": "乙", "label": "编曲", "names": ["乙"]},
        ],
        "urls": [
            {"name": "circle_site", "url": "https://a.com/",
             "href": "/link/k/1", "position": 1},
        ],
        "tags": [], "genres": [],
        "tracks": [
            {"disc": 1, "index": 1, "title": "曲1-1", "artists": ["A"],
             "duration": 180, "duration_text": "3:00"},
            {"disc": 1, "index": 2, "title": "曲1-2", "artists": [],
             "duration": 200, "duration_text": "3:20"},
            {"disc": 2, "index": 1, "title": "曲2-1", "artists": ["B"],
             "duration": 210, "duration_text": "3:30"},
        ],
        "date": "2026-10-25", "event": "M3-2026秋", "catalog_no": "ABC-1",
        "price": "1000yen", "hall": "", "booth": "A-01",
        "description": "简介内容", "missing": [],
    }
    base.update(overrides)
    return base


def _detail(album):
    from dddb.public import templates as T

    ctx = T.SiteContext(csrf_token="t", is_editor=False, lang="zh")
    return T.album_detail_page(ctx, album=album)


def _edit(album):
    from dddb.public import templates as T

    ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                        user={"id": 1, "username": "ed", "role": "editor"})
    return T.edit_album_page(ctx, album=album,
                             values=T.edit_values_from_album(album))


class TestDetailSectionOrder(unittest.TestCase):
    """详情页区块顺序: 社团 -> 艺人/社团+制作人员 -> 简介 -> 曲目表 -> 外链."""

    def setUp(self) -> None:
        self.html = _detail(_album())

    def test_section_order(self) -> None:
        order = []
        for m in re.finditer(
                r'<h2 class="section-title">([^<]*)</h2>'
                r'|<summary class="section-title fold-title">\s*([^<\s]+)',
                self.html):
            label = (m.group(1) or m.group(2) or "").strip()
            if label:
                order.append("曲目表" if "fold-title" in m.group(0) else label)
        # 制作人员(合并区块) -> 简介 -> 曲目表 -> 外链
        self.assertIn("简介", order)
        self.assertIn("曲目表", order)
        self.assertLess(order.index("简介"), order.index("曲目表"),
                        "简介要在曲目表之前")

    def test_circle_first(self) -> None:
        """社团显示在**标题后**(detail-circle), 不进制作人员区块."""
        self.assertIn('class="detail-circle"', self.html,
                      "社团要有独立的标题后位置")
        pos_circle = self.html.find("detail-circle")
        pos_title = self.html.find("detail-title")
        pos_section = self.html.find('<section class="panel"')
        self.assertGreater(pos_circle, pos_title, "社团要在标题之后")
        self.assertLess(pos_circle, pos_section, "社团要在首个区块之前")

    def test_merged_people_section(self) -> None:
        """制作人员区块: 艺人 + 词条, **纯 chip 按钮**, 不含社团."""
        self.assertIn('class="people-chips"', self.html)
        m = re.search(r'<div class="people-chips">([\s\S]*?)</div>\s*</section>',
                      self.html)
        self.assertIsNotNone(m, "找不到制作人员区块")
        inner = m.group(1)
        self.assertIn("艺人", inner)
        self.assertIn("作曲", inner, "制作人员要合并进来")
        self.assertNotIn("社团", inner, "社团不进制作者区块")
        # 不能有旧的文字列表(会与按钮重复)
        self.assertNotIn("<dt>", inner)
        self.assertNotIn("people-grid", self.html)


    def test_circle_not_in_people_even_from_credits(self) -> None:
        """即使 credits 里有 circle 角色词条, 也不在制作人员里显示."""
        html = _detail(_album(
            circle="",  # album.circle 为空
            artists=[],
            credits=[{"role": "circle", "label": "社团",
                      "names": ["某社团"]},
                     {"role": "编曲", "label": "编曲", "names": ["乙"]}],
        ))
        m = re.search(r'<div class="people-chips">([\s\S]*?)</div>\s*</section>',
                      html)
        self.assertIsNotNone(m, "找不到制作人员区块")
        inner = m.group(1)
        self.assertNotIn("某社团", inner, "circle 词条不该进制作人员")
        self.assertIn("乙", inner, "普通制作人员要保留")

    def test_no_separate_artist_section(self) -> None:
        """不该再有独立的"艺人与社团"区块."""
        h2s = re.findall(r'<h2 class="section-title">([^<]*)</h2>', self.html)
        self.assertFalse(any("艺人" in h for h in h2s),
                         f"不该有独立艺人区块: {h2s}")


class TestTrackFolding(unittest.TestCase):
    """曲目表整个容器可折叠."""

    def setUp(self) -> None:
        self.html = _detail(_album())

    def test_uses_details_tag(self) -> None:
        self.assertIn("<details", self.html)
        self.assertIn("</details>", self.html)

    def test_fold_panel_class(self) -> None:
        self.assertIn("fold-panel", self.html)
        self.assertIn("fold-title", self.html)
        self.assertIn("fold-body", self.html)

    def test_open_by_default(self) -> None:
        self.assertRegex(self.html, r'<details class="panel fold-panel" open')

    def test_shows_count(self) -> None:
        self.assertIn("3 首", self.html)

    def test_empty_tracks_ok(self) -> None:
        html = _detail(_album(tracks=[]))
        self.assertIn("<details", html)
        self.assertIn("还没有曲目表", html)


class TestDiscVisibleInDetail(unittest.TestCase):
    """详情页也要能看出分碟."""

    def test_disc_subtitles(self) -> None:
        html = _detail(_album())
        discs = re.findall(r'class="disc-subtitle">Disc (\d+)', html)
        self.assertEqual(discs, ["01", "02"])

    def test_track_numbers_with_disc(self) -> None:
        html = _detail(_album())
        self.assertIn("2-1", html, "多碟时序号显示 碟-曲")

    def test_single_disc_no_subtitle(self) -> None:
        """单碟不显示碟标题(保持简洁)."""
        html = _detail(_album(tracks=[
            {"disc": 1, "index": 1, "title": "唯一", "artists": [],
             "duration": 100, "duration_text": "1:40"},
        ]))
        self.assertNotIn("disc-subtitle", html)


class TestCreditAndUrlButtons(unittest.TestCase):
    """词条与外链都要有增删按钮."""

    def setUp(self) -> None:
        self.html = _edit(_album())

    def test_credit_add_button(self) -> None:
        self.assertIn('data-pair-add="credit"', self.html)
        self.assertIn("添加词条", self.html)

    def test_url_add_button(self) -> None:
        self.assertIn('data-pair-add="url"', self.html)
        self.assertIn("添加外链", self.html)

    def test_credit_delete_buttons(self) -> None:
        self.assertIn('data-pair-del="credit"', self.html)

    def test_url_delete_buttons(self) -> None:
        self.assertIn('data-pair-del="url"', self.html)

    def test_no_inline_onclick(self) -> None:
        for m in re.finditer(r"<button[^>]*>", self.html):
            self.assertNotIn("onclick", m.group(0))

    def test_js_has_pair_setup(self) -> None:
        js = (ROOT / "dddb/public/static/public.js").read_text(
            encoding="utf-8")
        self.assertIn("function setupPairRows(", js)
        self.assertIn("function buildPairRow(", js)
        self.assertIn("setupPairRows()", js)

    def test_css_has_pair_styles(self) -> None:
        css = (ROOT / "dddb/public/static/public.css").read_text(
            encoding="utf-8")
        for token in (".pair-del", ".pair-add", ".fold-panel",
                      ".detail-circle", ".people-grid", ".disc-subtitle"):
            self.assertIn(token, css, f"缺样式 {token}")


class TestPeopleFallback(unittest.TestCase):
    """没有社团/艺人/词条时不能崩."""

    def test_empty_people(self) -> None:
        """什么都没有时退回普通 empty 提示(不渲染空 people-grid)."""
        html = _detail(_album(circle="", artists=[], credits=[]))
        self.assertNotIn("people-grid", html)
        self.assertIn('<div class="empty">', html)

    def test_circle_only_still_shows_circle(self) -> None:
        """只有社团时: 社团在标题后显示, 制作人员区块为空提示."""
        html = _detail(_album(artists=[], credits=[]))
        self.assertIn('class="detail-circle"', html)
        self.assertIn("测试社团", html)
        self.assertNotIn("people-chips", html)


    def test_only_circle(self) -> None:
        """只有社团: 标题后有社团, 制作人员区块为空提示."""
        html = _detail(_album(artists=[], credits=[]))
        self.assertIn('class="detail-circle"', html)
        self.assertIn("测试社团", html)
        self.assertNotIn("people-chips", html)



if __name__ == "__main__":
    unittest.main()
