"""测试: 详情页外链显示用户填的链接名."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests._tmp import temp_dir

ROOT = Path(__file__).resolve().parents[1]


def _render(album):
    from dddb.public import templates as T

    ctx = T.SiteContext(csrf_token="t", is_editor=False, lang="zh")
    return T.album_detail_page(ctx, album=album)


class TestLinkLabel(unittest.TestCase):
    """外链按钮显示实际链接名, 而不是"外部链接 N"."""

    def _album(self, urls):
        return {
            "key": "k", "title": "标题", "urls": urls,
            "tracks": [], "credits": [], "tags": [], "genres": [],
        }

    def test_shows_user_names(self) -> None:
        html = _render(self._album([
            {"name": "circle_site", "url": "https://a.com/", "href": "/link/k/1"},
            {"name": "x", "url": "https://x.com/b", "href": "/link/k/2"},
            {"name": "youtube", "url": "https://y.com/w?v=1", "href": "/link/k/3"},
        ]))
        labels = re.findall(r'<a class="link-pill"[^>]*>([^<]*)</a>', html)
        self.assertEqual(labels, ["circle_site", "x", "youtube"])

    def test_no_generic_label_when_named(self) -> None:
        html = _render(self._album([
            {"name": "bandcamp", "url": "https://b.com/", "href": "/link/k/1"},
        ]))
        self.assertNotIn("外部链接", html)
        self.assertIn("bandcamp", html)

    def test_falls_back_when_no_name(self) -> None:
        html = _render(self._album([
            {"url": "https://a.com/", "href": "/link/k/1"},
            {"url": "https://b.com/", "href": "/link/k/2"},
        ]))
        labels = re.findall(r'<a class="link-pill"[^>]*>([^<]*)</a>', html)
        self.assertEqual(labels, ["外部链接 1", "外部链接 2"])

    def test_any_source_is_shown(self) -> None:
        """**所有来源的链接都渲染** —— 不再有域名黑名单。"""
        html = _render(self._album([
            {"name": "circle_site", "url": "https://a.com/", "href": "/link/k/1"},
            {"name": "dojinmusic", "url": "https://www.dojin-music.info/cd/1",
             "href": "/link/k/2"},
            {"name": "vgmdb", "url": "https://vgmdb.net/album/1",
             "href": "/link/k/3"},
        ]))
        labels = re.findall(r'<a class="link-pill"[^>]*>([^<]*)</a>', html)
        self.assertEqual(labels, ["circle_site", "dojinmusic", "vgmdb"],
                         "所有来源都该渲染")

    def test_no_external_url_in_html(self) -> None:
        """合规: HTML 里不能出现外链地址."""
        html = _render(self._album([
            {"name": "circle_site", "url": "https://secret-host.example/",
             "href": "/link/k/1"},
        ]))
        self.assertNotIn("secret-host.example", html,
                         "不该把外链地址写进 HTML")
        self.assertIn("/link/k/1", html, "要走本站中转")

    def test_name_escaped(self) -> None:
        """链接名要转义(防 XSS)."""
        html = _render(self._album([
            {"name": "<script>alert(1)</script>",
             "url": "https://a.com/", "href": "/link/k/1"},
        ]))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_japanese_and_cjk_names(self) -> None:
        """用户可以用中文/日文命名."""
        html = _render(self._album([
            {"name": "社团官网", "url": "https://a.com/", "href": "/link/k/1"},
            {"name": "特設サイト", "url": "https://b.com/", "href": "/link/k/2"},
        ]))
        labels = re.findall(r'<a class="link-pill"[^>]*>([^<]*)</a>', html)
        self.assertEqual(labels, ["社团官网", "特設サイト"])

    def test_empty_name_falls_back(self) -> None:
        html = _render(self._album([
            {"name": "   ", "url": "https://a.com/", "href": "/link/k/1"},
        ]))
        labels = re.findall(r'<a class="link-pill"[^>]*>([^<]*)</a>', html)
        self.assertEqual(labels, ["外部链接 1"])


class TestNamedRowsOrder(unittest.TestCase):
    """带名称的行与 /link 序号必须同序."""

    def test_same_order_as_plain_rows(self) -> None:
        from dddb.public.data import _outbound_named_rows, _outbound_rows

        urls = {
            "youtube": "https://www.youtube.com/watch?v=1",
            "circle_site": "https://a.com/",
            "x": "https://x.com/b",
            "bandcamp": "https://c.com/",
        }
        named = _outbound_named_rows(urls)
        plain = _outbound_rows(urls)
        self.assertEqual([u for _n, u in named], plain,
                         "顺序必须一致, 否则点第 n 个跳到别的链接")
        self.assertEqual(sorted(n for n, _u in named), sorted(urls.keys()))

    def test_empty_urls(self) -> None:
        from dddb.public.data import _outbound_named_rows

        self.assertEqual(_outbound_named_rows({}), [])

    def test_blank_url_skipped(self) -> None:
        from dddb.public.data import _outbound_named_rows

        rows = _outbound_named_rows({"a": "https://a.com/", "b": "", "c": "  "})
        self.assertEqual([n for n, _u in rows], ["a"])


class TestDetailPageEndToEnd(unittest.TestCase):
    """真实数据: 库里存了名称, 详情页就要显示出来."""

    def test_db_names_rendered(self) -> None:
        from dddb.config import Config
        from dddb.db import Store
        from dddb.models import Album
        from dddb.public.data import PublicData, ReadOnlyStore

        ctx = temp_dir("linklabel-")
        root = Path(ctx.__enter__())
        try:
            config = Config.default(root=root)
            config.ensure_dirs()
            store = Store(config.db_path)
            store.upsert_album(Album.from_dict({
                "key": "lbl", "title": "链接名测试", "tracks": [],
                "credits": [], "source_ids": {},
                "urls": {
                    "circle_site": "https://a.example.com/",
                    "x": "https://x.example.com/b",
                    "youtube": "https://yt.example.com/watch?v=1",
                },
            }))
            store.close()

            rs = ReadOnlyStore(config.db_path)
            data = PublicData(rs, covers_dir=Path(config.data_dir) / "covers")
            album = data.get_album("lbl")
            html = _render(album)
            labels = re.findall(r'<a class="link-pill"[^>]*>([^<]*)</a>', html)
            self.assertEqual(sorted(labels),
                             ["circle_site", "x", "youtube"],
                             f"应显示真实名称, 实际: {labels}")
            # 外链地址不外泄
            self.assertNotIn("a.example.com", html)
            self.assertNotIn("x.example.com", html)
            if hasattr(rs, "close"):
                rs.close()
        finally:
            ctx.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()


class TestLinkPositionAlignment(unittest.TestCase):
    """过滤禁用站后, /link 序号必须仍指向同一条记录."""

    def test_position_preserved_after_filter(self) -> None:
        """所有条目都保留, 序号与 position 一一对应(不会 404)."""
        rows = [
            {"name": "circle_site", "url": "https://a.com/",
             "href": "/link/k/1", "position": 1},
            {"name": "dojinmusic",
             "url": "https://www.dojin-music.info/cd/1",
             "href": "/link/k/2", "position": 2},
            {"name": "soundcloud", "url": "https://s.com/b",
             "href": "/link/k/3", "position": 3},
            {"name": "x", "url": "https://x.com/c",
             "href": "/link/k/4", "position": 4},
        ]
        html = _render({"key": "k", "title": "T", "urls": rows,
                        "tracks": [], "credits": [], "tags": [], "genres": []})
        pills = re.findall(
            r'<a class="link-pill" href="([^"]*)"[^>]*>([^<]*)</a>', html)
        self.assertEqual([h for h, _l in pills],
                         ["/link/k/1", "/link/k/2", "/link/k/3",
                          "/link/k/4"],
                         "序号必须与原始 position 一致")
        self.assertEqual([label for _h, label in pills],
                         ["circle_site", "dojinmusic", "soundcloud", "x"])

    def test_data_layer_sets_position(self) -> None:
        """data 层 url_rows 要带 position(与 /link 序号一致)."""
        from dddb.config import Config
        from dddb.db import Store
        from dddb.models import Album
        from dddb.public.data import PublicData, ReadOnlyStore

        ctx = temp_dir("linkpos-")
        root = Path(ctx.__enter__())
        try:
            config = Config.default(root=root)
            config.ensure_dirs()
            store = Store(config.db_path)
            store.upsert_album(Album.from_dict({
                "key": "p", "title": "T", "tracks": [],
                "credits": [], "source_ids": {},
                "urls": {"circle_site": "https://a.com/",
                         "dojinmusic": "https://www.dojin-music.info/1",
                         "x": "https://x.com/b"},
            }))
            store.close()

            rs = ReadOnlyStore(config.db_path)
            data = PublicData(rs, covers_dir=Path(config.data_dir) / "covers")
            album = data.get_album("p")
            rows = album.get("urls") or []
            self.assertEqual(len(rows), 3)
            for i, row in enumerate(rows, 1):
                self.assertEqual(row.get("position"), i,
                                 f"第 {i} 行 position 不对")
                self.assertTrue(str(row.get("href", "")).endswith(str(i)))
            if hasattr(rs, "close"):
                rs.close()
        finally:
            ctx.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
