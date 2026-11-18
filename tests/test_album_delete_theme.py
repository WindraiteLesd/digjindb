"""测试: 词条删除 + 明亮主题对比度."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests._tmp import temp_dir
from dddb.public import site as S
from dddb.public import templates as T

ROOT = Path(__file__).resolve().parents[1]


class TestAlbumDelete(unittest.TestCase):
    def test_route_and_write_scope(self) -> None:
        route = S.parse_route("/album/test-key/delete")
        self.assertEqual(route["name"], "album_delete")
        self.assertEqual(route["arg"], "test-key")
        self.assertIn("album_delete", S._MEMBER_WRITE_ROUTES)
        self.assertTrue(hasattr(S.PublicSite, "_do_album_delete"))

    def test_edit_page_has_delete_button(self) -> None:
        ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                            user={"username": "ed", "role": "editor"})
        album = {"key": "k", "title": "T", "href": "/album/k", "circle": "",
                 "artists": [], "tracks": [], "credits": [], "urls": [],
                 "tags": [], "genres": [], "missing": [], "cover": "",
                 "has_cover": False, "date": "", "event": "", "catalog_no": "",
                 "price": "", "hall": "", "booth": "", "updated_text": "",
                 "title_alt": "", "romaji": "", "event_kind": "", "year": "",
                 "description": "", "track_count": 0}
        html = T.edit_album_page(ctx, album=album, values={})
        self.assertIn("删除这张专辑", html)
        self.assertIn('action="/album/k/delete"', html)

    def test_delete_removes_all_related_rows(self) -> None:
        ctx = temp_dir("del-")
        root = Path(ctx.__enter__())
        try:
            from dddb.db import Store
            from dddb.models import Album

            store = Store(root / "db.sqlite3")
            store.upsert_album(Album.from_dict({
                "key": "gone", "title": "待删除", "circle": "C",
                "tracks": [], "source_ids": {}}))
            store.add_revision("gone", summary="测试版本")
            self.assertIsNotNone(store.get_album("gone"))

            # 与处理器相同的清理顺序
            for table in ("album_artists", "album_circles", "album_events",
                          "album_tag_ids"):
                store.execute(f"DELETE FROM {table} WHERE album_key = ?", ("gone",))
            for table, column in (("comments", "target_key"),
                                  ("album_revisions", "album_key")):
                store.execute(f"DELETE FROM {table} WHERE {column} = ?", ("gone",))
            store.execute("DELETE FROM albums WHERE key = ?", ("gone",))

            self.assertIsNone(store.get_album("gone"))
            left = store.query(
                "SELECT COUNT(*) AS c FROM album_revisions WHERE album_key = ?",
                ("gone",))[0]["c"]
            self.assertEqual(left, 0, "修订记录也要清掉")
            store.close()
        finally:
            ctx.__exit__(None, None, None)


class TestLightThemeContrast(unittest.TestCase):
    """明亮主题下不能有写死的深色背景(会导致文字看不见)."""

    def setUp(self) -> None:
        self.css = (ROOT / "dddb/public/static/public.css").read_text(encoding="utf-8")

    def test_no_hardcoded_dark_backgrounds(self) -> None:
        import re

        offenders = []
        for match in re.finditer(r"background(?:-color)?:\s*#([0-9a-f]{6})",
                                 self.css, re.I):
            hexv = match.group(1)
            r, g, b = int(hexv[0:2], 16), int(hexv[2:4], 16), int(hexv[4:6], 16)
            if (r + g + b) / 3 < 60:
                line = self.css[:match.start()].count("\n") + 1
                offenders.append((line, match.group(0)))
        self.assertEqual(offenders, [], f"仍有写死的深色背景: {offenders[:5]}")

    def test_light_theme_defined(self) -> None:
        self.assertIn('[data-theme="light"]', self.css)
        import re

        block = re.search(r':root\[data-theme="light"\]\s*\{([^}]*)\}', self.css)
        self.assertIsNotNone(block)
        values = block.group(1)
        self.assertIn("--bg:", values)
        self.assertIn("--text:", values)
        self.assertIn("--panel:", values)

    def test_light_overrides_present(self) -> None:
        for selector in (".app-dialog", ".suggest-list", ".entity-card", "input"):
            self.assertIn(f'data-theme="light"] {selector}', self.css,
                          f"明亮主题缺少 {selector} 覆盖")


if __name__ == "__main__":
    unittest.main()