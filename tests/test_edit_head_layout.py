"""测试: 编辑页封面与标题卡片等宽并排."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _render():
    from dnh.public import templates as T

    album = {
        "key": "d", "title": "T", "title_original": "T", "title_alt": "",
        "romaji": "", "circle": "", "artists": [], "credits": [], "urls": {},
        "tags": [], "genres": [], "missing": [], "cover": "", "has_cover": False,
        "cover_local": "", "date": "", "event": "", "catalog_no": "", "price": "",
        "hall": "", "booth": "", "updated_text": "", "event_kind": "", "year": "",
        "description": "", "track_count": 0, "tracks": [],
    }
    ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                        user={"id": 1, "username": "ed", "role": "editor"})
    return T.edit_album_page(ctx, album=album,
                             values=T.edit_values_from_album(album))


class TestEditHeadGrid(unittest.TestCase):
    """封面卡片与标题卡片一左一右且等宽."""

    def setUp(self) -> None:
        self.html = _render()
        self.css = (ROOT / "dnh/public/static/public.css").read_text(
            encoding="utf-8")

    def test_cover_and_title_same_card(self) -> None:
        """封面与标题合并成**同一张卡片**(重构后)。"""
        self.assertIn('data-card="cover"', self.html)
        self.assertIn("edit-cards", self.html)
        # 封面和标题在同一卡片里
        import re as _re
        m = _re.search(r'data-card="cover"([\s\S]*?)</section>', self.html)
        self.assertIsNotNone(m)
        inner = m.group(1)
        self.assertIn("cover-input", inner, "封面上传要在该卡片里")
        self.assertIn('name="title_original"', inner, "标题也要在该卡片里")

    def test_two_column_grid(self) -> None:
        """卡片固定两列, 版面位置由 grid-column/grid-row 写死。"""
        m = re.search(r"\.edit-cards \{([^}]*)\}", self.css)
        self.assertIsNotNone(m, "找不到 .edit-cards")
        body = m.group(1)
        self.assertIn("grid-template-columns: repeat(2", body)
        # 位置规则存在
        for name in ("cover", "category", "info", "description",
                     "credits", "links"):
            self.assertRegex(
                self.css,
                r"\.edit-card-{}\s*\{{[^}}]*grid-column".format(name),
                "{} 缺 grid 定位".format(name))
        # 旧的宽/半卡类已移除
        self.assertNotIn(".edit-card-wide", self.css)
        self.assertNotIn(".edit-card-half", self.css)

    def test_side_card_single_column(self) -> None:
        """左/右两栏里的卡片(封面/分类/出品信息)表单**单列**。"""
        for name in ("cover", "category", "info"):
            self.assertIn(".edit-card-{} .form-grid".format(name), self.css)

    def test_wide_card_form_grid(self) -> None:
        """整行宽的卡片(简介/制作人员)表单可多列。"""
        self.assertIn(".edit-card-description .form-grid", self.css)
        self.assertIn(".edit-card-credits .form-grid", self.css)

    def test_cover_img_adapts(self) -> None:
        """封面图有尺寸约束(不能撑破卡片)。"""
        m = re.search(r"\.edit-cover-row \.edit-cover-img \{([^}]*)\}", self.css)
        self.assertIsNotNone(m, "缺少封面尺寸规则")
        body = m.group(1)
        self.assertIn("max-width", body)
        self.assertIn("object-fit", body)

    def test_stacks_on_mobile(self) -> None:
        """窄屏卡片单列堆叠。"""
        self.assertRegex(
            self.css,
            r"@media \(max-width: 980px\)[\s\S]{0,900}?"
            r"\.edit-cards \{ grid-template-columns: minmax\(0, 1fr\)")

    def test_required_star_style(self) -> None:
        self.assertIn(".req-star", self.css)


class TestNoLeftoverGridOnRows(unittest.TestCase):
    """曲目表的 .track-row 不能再有 grid 布局(会跟 table 冲突)."""

    def test_track_row_no_grid(self) -> None:
        css = (ROOT / "dnh/public/static/public.css").read_text(
            encoding="utf-8")
        for m in re.finditer(r"\.track-row[^{,]*\{([^}]*)\}", css):
            body = m.group(1)
            self.assertNotIn("grid-template-columns", body,
                             f"track-row 不该有 grid: {body[:80]}")
            self.assertNotIn("display: grid", body,
                             f"track-row 不该是 grid: {body[:80]}")


if __name__ == "__main__":
    unittest.main()
