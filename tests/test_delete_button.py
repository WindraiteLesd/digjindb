"""测试: 删除按钮必须真的可用(CSP 兼容 + 表单不嵌套)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _render() -> str:
    from dddb.public import templates as TP

    ctx = TP.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                         user={"id": 1, "username": "ed", "role": "editor"})
    album = {"key": "k", "title": "T", "title_original": "T", "title_alt": "",
             "romaji": "", "circle": "C", "artists": [], "tracks": [],
             "credits": [], "urls": {}, "tags": [], "genres": [], "missing": [],
             "cover": "", "has_cover": False, "cover_local": "", "date": "",
             "event": "", "catalog_no": "", "price": "", "hall": "", "booth": "",
             "updated_text": "", "event_kind": "", "year": "", "description": "",
             "track_count": 0}
    return TP.edit_album_page(ctx, album=album,
                              values=TP.edit_values_from_album(album))


class TestDeleteButton(unittest.TestCase):
    """点了没反应的两个历史原因: CSP 拦内联脚本 / form 嵌套被浏览器丢弃."""

    def setUp(self) -> None:
        self.html = _render()
        self.templates = (ROOT / "dddb/public/templates.py").read_text(encoding="utf-8")

    def test_delete_button_exists(self) -> None:
        self.assertIn("删除这张专辑", self.html)
        self.assertIn('class="btn-danger"', self.html)
        self.assertIn('form="delete-album-form"', self.html)

    def test_no_inline_handlers(self) -> None:
        """CSP 是 script-src 'self', 内联 onclick/onsubmit 会被直接拦掉."""
        for pattern in ("onclick=", "onsubmit=", "onchange="):
            self.assertNotIn(pattern, self.html,
                             f"不能有 {pattern}(CSP 会拦, 点了没反应)")

    def test_confirm_uses_data_attribute(self) -> None:
        """确认弹窗走 data-confirm + public.js 的 setupConfirm."""
        self.assertIn("data-confirm=", self.html)
        js = (ROOT / "dddb/public/static/public.js").read_text(encoding="utf-8")
        self.assertIn("form[data-confirm]", js, "JS 要处理 data-confirm")
        self.assertIn("function setupConfirm", js)

    def test_delete_form_not_nested(self) -> None:
        """HTML 不允许 form 嵌套, 内层会被浏览器丢弃 -> 按钮找不到表单."""
        depth = 0
        for match in re.finditer(r"<(/?)form\b[^>]*>", self.html):
            if match.group(1):
                depth -= 1
            else:
                self.assertEqual(depth, 0,
                                 f"form 嵌套了(位置 {match.start()}), 浏览器会丢弃内层")
                depth += 1
        self.assertEqual(depth, 0, "form 标签不成对")

    def test_delete_form_has_csrf(self) -> None:
        block = re.search(r'<form id="delete-album-form"[\s\S]{0,400}?</form>', self.html)
        self.assertIsNotNone(block, "找不到删除表单")
        self.assertIn('name="csrf_token"', block.group(0))
        self.assertIn('action="/album/k/delete"', block.group(0))

    def test_delete_route_wired_by_form_attribute(self) -> None:
        """按钮通过 form 属性关联外部表单, 该表单必须在文档里存在."""
        self.assertIn('id="delete-album-form"', self.html)


class TestDeleteBackend(unittest.TestCase):
    def test_route_and_handler(self) -> None:
        from dddb.public import site as SITE

        route = SITE.parse_route("/album/test/delete")
        self.assertEqual(route["name"], "album_delete")
        self.assertEqual(route["arg"], "test")
        self.assertIn("album_delete", SITE._MEMBER_WRITE_ROUTES)
        self.assertTrue(hasattr(SITE.PublicSite, "_do_album_delete"))

    def test_get_is_rejected(self) -> None:
        """GET 不能触发删除(应在 POST-only 拦截表里)."""
        source = (ROOT / "dddb/public/site.py").read_text(encoding="utf-8")
        self.assertIn('"album_delete"', source)
        start = source.find('if name in ("account_password"')
        block = source[start:start + 400]
        self.assertIn("album_delete", block, "album_delete 要在 GET 拦截列表里")


if __name__ == "__main__":
    unittest.main()