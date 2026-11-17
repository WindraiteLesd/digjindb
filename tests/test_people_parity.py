"""测试: 详情页与编辑页制作人员显示完全一致."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BASE = {
    "key": "d", "title": "T", "title_original": "T", "title_alt": "",
    "romaji": "", "circle": "", "urls": {}, "tags": [], "genres": [],
    "tracks": [], "date": "", "event": "", "catalog_no": "", "price": "",
    "company": "", "hall": "", "booth": "", "description": "",
    "missing": [], "track_count": 0,
}


def _edit_pairs(album):
    from dnh.public import templates as T

    ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                        user={"id": 1, "username": "ed", "role": "editor"})
    html = T.edit_album_page(ctx, album=album,
                             values=T.edit_values_from_album(album),
                             bind_options={"artist": []})
    m = re.search(r'data-people="artists">([\s\S]*?)</div>', html)
    if not m:
        return []
    return [(p, r or "艺人") for p, r in re.findall(
        r'data-person="([^"]*)" data-role="([^"]*)"', m.group(1))]


def _detail_pairs(album):
    from dnh.public import templates as T

    ctx = T.SiteContext(csrf_token="t", is_editor=False, lang="zh")
    view = dict(album)
    # 数据层给详情页的 credits 是 {label, names}
    view["credits"] = [{"label": c.get("role") or "",
                        "names": [c.get("name") or ""]}
                       for c in (album.get("credits") or [])]
    html = T.album_detail_page(ctx, album=view)
    m = re.search(r'<div class="people-chips">([\s\S]*?)</div>', html)
    if not m:
        return []
    return [(m2.group(2), m2.group(1).strip() or "艺人")
            for m2 in re.finditer(
                r'<span class="credit-chip-kind">([^<]*)</span>\s*'
                r'<b>([^<]*)</b>', m.group(1))]


class TestParity(unittest.TestCase):
    """详情页与编辑页的按钮列表必须**完全一致**(顺序+内容)。"""

    def _assert_same(self, artists, credits) -> None:
        album = dict(BASE, artists=artists, credits=credits)
        edit = _edit_pairs(album)
        detail = _detail_pairs(album)
        self.assertEqual(edit, detail,
                         f"不一致\n  编辑={edit}\n  详情={detail}")

    def test_artist_only(self) -> None:
        self._assert_same(["甲", "乙"], [])

    def test_artist_with_role(self) -> None:
        self._assert_same(["甲", "乙"], [{"role": "作曲", "name": "甲"}])

    def test_extra_artist_without_role(self) -> None:
        self._assert_same(["甲", "乙", "丙"], [{"role": "作曲", "name": "甲"}])

    def test_same_person_multiple_roles(self) -> None:
        """同一人多职责: 只保留第一条(不重复出按钮)."""
        self._assert_same(["甲"], [{"role": "作曲", "name": "甲"},
                                 {"role": "编曲", "name": "甲"}])

    def test_real_case(self) -> None:
        self._assert_same(
            ["MALVA.", "しおの", "すばる", "綾花", "CS4W"],
            [{"role": "Circle Logo", "name": "綾花"},
             {"role": "Collaboration Tr.5", "name": "CS4W"},
             {"role": "Jacket Illustration", "name": "しおの"},
             {"role": "Lyrics Tr.4", "name": "すばる"},
             {"role": "Produce / Compose / Lyrics CD Design / Web / Movie",
              "name": "MALVA."}])

    def test_person_not_in_artists(self) -> None:
        """只在词条里出现的人也要有按钮。"""
        self._assert_same([], [{"role": "作曲", "name": "丁"}])

    def test_empty(self) -> None:
        self._assert_same([], [])


class TestNoDuplicateArtists(unittest.TestCase):
    """旧 bug: 艺人单独列一遍 + 词条再列一遍 = 重复。"""

    def test_no_duplicate_person(self) -> None:
        album = dict(BASE, artists=["MALVA.", "しおの", "綾花"],
                     credits=[{"role": "Circle Logo", "name": "綾花"},
                              {"role": "Jacket Illustration", "name": "しおの"},
                              {"role": "Produce", "name": "MALVA."}])
        pairs = _detail_pairs(album)
        names = [p for p, _ in pairs]
        self.assertEqual(len(names), len(set(names)),
                         f"有人重复出现: {names}")
        self.assertEqual(len(names), 3, "3 个人就只该有 3 个按钮")

    def test_button_count_matches_people(self) -> None:
        album = dict(BASE, artists=["甲", "乙"],
                     credits=[{"role": "作曲", "name": "甲"}])
        pairs = _detail_pairs(album)
        self.assertEqual(len(pairs), 2, "甲(作曲) + 乙(艺人)")

    def test_only_buttons_no_artist_lines(self) -> None:
        """详情页制作人员只有按钮形式(没有单独的「艺人」文字行)。"""
        album = dict(BASE, artists=["甲"], credits=[])
        from dnh.public import templates as T

        ctx = T.SiteContext(csrf_token="t", is_editor=False, lang="zh")
        html = T.album_detail_page(ctx, album=album)
        m = re.search(r'<h2 class="section-title">制作人员</h2>([\s\S]{0,600}?)'
                      r'</section>', html)
        self.assertIsNotNone(m)
        section = m.group(1)
        self.assertIn("credit-chip", section)
        # 不再有 <dt>艺人</dt><dd>...</dd> 这种文字行
        self.assertNotIn("<dd>", section)


class TestRoleLabel(unittest.TestCase):
    """职责显示在人名前面, 没有职责显示「艺人」。"""

    def test_role_before_name(self) -> None:
        album = dict(BASE, artists=["甲"],
                     credits=[{"role": "mastering", "name": "Ryo"}])
        pairs = _detail_pairs(album)
        self.assertIn(("甲", "艺人"), pairs)
        self.assertIn(("Ryo", "mastering"), pairs)

    def test_chip_html_order(self) -> None:
        album = dict(BASE, artists=[], credits=[{"role": "母带", "name": "某人"}])
        from dnh.public import templates as T

        ctx = T.SiteContext(csrf_token="t", is_editor=False, lang="zh")
        view = dict(album, credits=[{"label": "母带", "names": ["某人"]}])
        html = T.album_detail_page(ctx, album=view)
        m = re.search(r'<span class="credit-chip">([\s\S]*?)</span>\s*'
                      r'</div>', html)
        self.assertIsNotNone(m)
        chip = m.group(1)
        # 职责在前, 人名在后
        self.assertLess(chip.find("母带"), chip.find("某人"))


if __name__ == "__main__":
    unittest.main()
