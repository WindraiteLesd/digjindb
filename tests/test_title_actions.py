"""测试: 标题行按钮(新建/编辑资料)同排且紧跟标题、不重复."""

from __future__ import annotations

import unittest

from dddb.public import templates as T


class TestTitleActions(unittest.TestCase):
    def _editor(self):
        return T.SiteContext(csrf_token="t", is_editor=True,
                             user={"username": "ed", "role": "editor"})

    def _viewer(self):
        return T.SiteContext(csrf_token="t", is_editor=False,
                             user={"username": "v", "role": "viewer"})

    def _row(self, html: str) -> str:
        start = html.find('class="title-row"')
        if start < 0:
            return ""
        end = html.find("</div>", start)
        return html[start:end]

    def test_buttons_inside_title_row(self) -> None:
        """新建与编辑资料必须在同一个标题行容器里."""
        ctx = self._editor()
        for maker, label in (
            (lambda: T.artist_detail_page(ctx, artist={
                "name": "A", "album_count": 0, "albums": [], "circles": [],
                "occupations": [], "relations": []}), "artist"),
            (lambda: T.circle_detail_page(ctx, circle={"name": "C", "albums": []}), "circle"),
        ):
            html = maker()
            row = self._row(html)
            self.assertTrue(row, f"{label} 页应有 title-row")
            self.assertIn("新建", row, f"{label} 新建按钮应在标题行内")
            self.assertIn("编辑资料", row, f"{label} 编辑按钮应在标题行内")
            # 标题在按钮之前
            self.assertLess(row.index("page-title"), row.index("title-actions"))

    def test_no_duplicate_new_button(self) -> None:
        ctx = self._editor()
        html = T.artist_detail_page(ctx, artist={
            "name": "A", "album_count": 0, "albums": [], "circles": [],
            "occupations": [], "relations": []})
        self.assertEqual(html.count('href="/new/artist"'), 1, "新建艺人不应重复")
        self.assertEqual(html.count("编辑资料"), 1, "编辑资料不应重复")

    def test_viewer_sees_no_buttons(self) -> None:
        html = T.artist_detail_page(self._viewer(), artist={
            "name": "A", "album_count": 0, "albums": [], "circles": [],
            "occupations": [], "relations": []})
        row = self._row(html)
        self.assertNotIn("新建", row)
        self.assertNotIn("编辑资料", row)

    def test_list_pages_have_new_button_in_title_row(self) -> None:
        ctx = self._editor()
        for active, kind, label in (("artists", "artist", "新建艺人"),
                                    ("circles", "circle", "新建社团"),
                                    ("events", "event", "新建展会"),
                                    ("tags", "tag", "新建标签"),
                                    ("albums", "album", "新建专辑")):
            html = T.layout(ctx, title="T", body="<p>x</p>", active=active)
            row = self._row(html)
            self.assertIn(label, row, f"{active} 页的新建按钮应在标题行")
            self.assertEqual(html.count(f'href="/new/{kind}"'), 1)


if __name__ == "__main__":
    unittest.main()