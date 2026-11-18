"""测试: 编辑页封面尺寸固定, 上传后不会撑大."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestEditCoverSize(unittest.TestCase):
    def setUp(self) -> None:
        self.css = (ROOT / "dddb/public/static/public.css").read_text(encoding="utf-8")
        self.templates = (ROOT / "dddb/public/templates.py").read_text(encoding="utf-8")

    def test_image_has_fixed_size(self) -> None:
        block = re.search(r"\.edit-cover-row \.edit-cover-img\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(block, "缺少编辑页封面图规则")
        body = block.group(1)
        self.assertIn("width: 132px", body)
        self.assertIn("height: 132px", body)

    def test_image_has_max_size_guard(self) -> None:
        """必须有 max-width/max-height, 否则大图会被拉伸."""
        block = re.search(r"\.edit-cover-row \.edit-cover-img\s*\{([^}]*)\}", self.css)
        body = block.group(1)
        self.assertIn("max-width: 132px", body, "需要 max-width 兜底")
        self.assertIn("max-height: 132px", body, "需要 max-height 兜底")
        self.assertIn("object-fit: cover", body, "保持比例不变形")

    def test_no_percent_width_on_edit_cover(self) -> None:
        """编辑页封面不能用百分比宽度(会被容器撑大)."""
        offenders = re.findall(
            r"\.edit-cover-img[^{]*\{[^}]*width:\s*\d+%", self.css)
        self.assertEqual(offenders, [], "编辑页封面不能按百分比宽度")

    def test_thumbnail_container_sized(self) -> None:
        block = re.search(r"\.edit-cover-row \.edit-cover-thumb\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(block)
        body = block.group(1)
        self.assertIn("width: 132px", body)
        self.assertIn("height: 132px", body)

    def test_selector_specificity(self) -> None:
        """封面图规则要挂在编辑页容器下, 优先级才够压过通用 .cover。"""
        blocks = re.findall(r"([^{}]*edit-cover-img[^{]*)\{", self.css)
        self.assertGreater(len(blocks), 0, "找不到封面图规则")
        for selector in blocks:
            self.assertTrue(
                ".edit-cover-row" in selector
                or ".edit-cover-thumb" in selector
                or ".edit-cover-zoom" in selector,
                f"选择器优先级不足: {selector.strip()}")

    def test_mobile_override(self) -> None:
        """窄屏下封面要缩到 104px(找包含 edit-cover-row 的那段媒体查询)."""
        import re

        blocks = re.findall(
            r"@media \(max-width: 640px\)\s*\{([\s\S]*?)\n\}", self.css)
        cover_blocks = [b for b in blocks if "edit-cover-row" in b]
        self.assertTrue(cover_blocks, "缺少封面相关的窄屏媒体查询")
        self.assertIn("104px", cover_blocks[0], "窄屏要缩到 104px")

    def test_single_image_in_edit_page(self) -> None:
        """编辑页只能有一张封面图(避免重复渲染成大图)."""
        from dddb.public import templates as TP

        ctx = TP.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                             user={"id": 1, "username": "ed", "role": "editor"})
        album = {"key": "k", "title": "T", "title_original": "T", "title_alt": "",
                 "romaji": "", "circle": "", "artists": [], "tracks": [],
                 "credits": [], "urls": [], "tags": [], "genres": [], "missing": [],
                 "cover": "/cover/k", "has_cover": True,
                 "cover_local": "/tmp/k.img", "date": "", "event": "",
                 "catalog_no": "", "price": "", "hall": "", "booth": "",
                 "updated_text": "", "event_kind": "", "year": "", "description": "",
                 "track_count": 0}
        html = TP.edit_album_page(ctx, album=album,
                                  values=TP.edit_values_from_album(album))
        cover_imgs = re.findall(r'<img[^>]*class="[^"]*edit-cover-img[^"]*"', html)
        self.assertEqual(len(cover_imgs), 1, "编辑页只能有一张封面图")
        self.assertIn('class="edit-cover-img"', html)


if __name__ == "__main__":
    unittest.main()