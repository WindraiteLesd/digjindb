"""回归测试: 编辑页必须渲染出真实外链名称.

真实事故: 编辑页 GET 用的是公开端视图 `data.get_album()`, 它的 urls 只有
`[{"url","href"}]`(合规要求: HTML 里不出现来源站名), **没有名称**。
于是表单渲染出空的「链接名」→ 用户一保存, parse_album_form 用
`f"link{n}"` 兜底 → 外链名永久退化成 link1/link2/link3。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests._tmp import temp_dir

ROOT = Path(__file__).resolve().parents[1]


class TestEditPageUrlNames(unittest.TestCase):
    """编辑页要能看到并保留外链名称。"""

    def setUp(self) -> None:
        self._ctx = temp_dir("urlnames-")
        self.root = Path(self._ctx.__enter__())
        from dddb.config import Config
        from dddb.db import Store
        from dddb.models import Album

        self.config = Config.default(root=self.root)
        self.config.ensure_dirs()
        self.key = "test|url-names|2026"
        self.urls = {
            "circle_site": "https://circle.example.com/",
            "x": "https://x.com/someone",
            "youtube": "https://www.youtube.com/watch?v=abc",
        }
        store = Store(self.config.db_path)
        store.upsert_album(Album.from_dict({
            "key": self.key, "title": "外链名测试", "circle": "测试社团",
            "event": "M3-2026秋", "hall": "第二展示場", "booth": "シ-31a",
            "catalog_no": "ABC-001", "price": "1000yen",
            "release_date": "2026-10-25",
            "artists": ["某人"], "tracks": [], "credits": [],
            "urls": dict(self.urls), "source_ids": {},
        }))
        store.close()

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def _site(self):
        from dddb.public.site import build_site

        return build_site(self.config)

    def test_raw_url_map_reads_payload(self) -> None:
        """_raw_url_map 能读到 payload 里的 {名称: url}."""
        site = self._site()
        mapping = site._raw_url_map(self.key)
        self.assertEqual(mapping, self.urls)

    def test_album_with_url_names(self) -> None:
        """_album_with_url_names 把名称补回视图."""
        site = self._site()
        album = site._album_with_url_names(self.key)
        self.assertIsNotNone(album)
        names = [link.get("key") for link in (album.get("urls") or [])]
        self.assertEqual(names, list(self.urls.keys()),
                         "外链名称必须保留(顺序按视图的稳定排序)")
        urls = [link.get("url") for link in (album.get("urls") or [])]
        self.assertEqual(urls, list(self.urls.values()))

    def test_edit_values_have_names(self) -> None:
        """edit_values_from_album 输出带 key 的 urls."""
        from dddb.public import templates as T

        site = self._site()
        album = site._album_with_url_names(self.key)
        values = T.edit_values_from_album(album)
        rows = values.get("urls") or []
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertTrue(row.get("key"), f"链接名为空: {row}")

    def test_rendered_form_has_names(self) -> None:
        """渲染出的 HTML 里 url_key 输入框有值."""
        import re

        from dddb.public import templates as T

        site = self._site()
        album = site._album_with_url_names(self.key)
        ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                            user={"id": 1, "username": "ed", "role": "editor"})
        html = T.edit_album_page(ctx, album=album,
                                 values=T.edit_values_from_album(album))
        keys = re.findall(r'name="url_key"[^>]*value="([^"]*)"', html)
        non_empty = [k for k in keys if k]
        self.assertEqual(len(non_empty), 3,
                         f"应有 3 个非空链接名, 实际: {non_empty}")
        for name in self.urls:
            self.assertIn(name, non_empty)

    def test_save_keeps_names(self) -> None:
        """完整往返: 保存后外链名称不变(不被 link1 覆盖)."""
        from dddb.db import Store
        from dddb.public import templates as T
        from dddb.public.site import parse_album_form

        site = self._site()
        album = site._album_with_url_names(self.key)
        values = T.edit_values_from_album(album)

        # 模拟浏览器提交: url_key/url_value 各 3 个 + 空行
        rows = values.get("urls") or []
        form = {
            "title": [values["title"]],
            "title_original": [values["title_original"]],
            "circle": [values["circle"]],
            "event": [values["event"]],
            "hall": [values["hall"]],
            "booth": [values["booth"]],
            "catalog_no": [values["catalog_no"]],
            "price": [values["price"]],
            "release_date": [values["release_date"]],
            "url_key": [row["key"] for row in rows] + [""],
            "url_value": [row["url"] for row in rows] + [""],
        }
        current = dict(album)
        patch, errors = parse_album_form(form, current=current)
        self.assertEqual(errors, {})

        store = Store(self.config.db_path)
        ok, message = store.update_album_fields(self.key, patch)
        self.assertTrue(ok, message)
        after = store.get_album(self.key)
        store.close()

        self.assertEqual(dict(after.urls), self.urls,
                         "保存后外链名称必须不变")

    def test_public_view_unchanged(self) -> None:
        """公开端视图保持合规: 不含来源站名/外链地址."""
        site = self._site()
        view = site.data.get_album(self.key)
        for link in view.get("urls") or []:
            self.assertNotIn("key", link, "公开端不该暴露来源名")
            self.assertNotIn("source", link, "公开端不该暴露来源名")
            self.assertIn("href", link, "公开端用 /link/ 中转")

    def test_no_names_falls_back_gracefully(self) -> None:
        """payload 里没有 urls 时不能崩."""
        from dddb.db import Store
        from dddb.models import Album

        store = Store(self.config.db_path)
        store.upsert_album(Album.from_dict({
            "key": "no-urls", "title": "无外链", "tracks": [],
            "source_ids": {}}))
        store.close()

        site = self._site()
        album = site._album_with_url_names("no-urls")
        self.assertIsNotNone(album)
        self.assertEqual(album.get("urls") or [], [])


class TestTrackDiscPersisted(unittest.TestCase):
    """碟号必须真的落库(update_album_fields 构造 Track 时带 disc)."""

    def test_disc_saved_and_read_back(self) -> None:
        from dddb.config import Config
        from dddb.db import Store
        from dddb.models import Album

        ctx = temp_dir("discdb-")
        root = Path(ctx.__enter__())
        try:
            config = Config.default(root=root)
            config.ensure_dirs()
            store = Store(config.db_path)
            store.upsert_album(Album.from_dict({
                "key": "d", "title": "碟号测试", "tracks": [],
                "source_ids": {}}))
            ok, message = store.update_album_fields("d", {
                "track_list": [
                    {"disc": 1, "index": 1, "title": "碟1曲1",
                     "artists": [], "duration": 180},
                    {"disc": 2, "index": 1, "title": "碟2曲1",
                     "artists": [], "duration": 200},
                    {"disc": 3, "index": 5, "title": "碟3曲5",
                     "artists": [], "duration": None},
                ],
            })
            self.assertTrue(ok, message)
            album = store.get_album("d")
            store.close()
            self.assertEqual([t.disc for t in album.tracks], [1, 2, 3],
                             "碟号必须落库")
            self.assertEqual([t.index for t in album.tracks], [1, 1, 5])
            self.assertEqual([t.title for t in album.tracks],
                             ["碟1曲1", "碟2曲1", "碟3曲5"])
        finally:
            ctx.__exit__(None, None, None)

    def test_missing_disc_defaults_to_one(self) -> None:
        from dddb.config import Config
        from dddb.db import Store
        from dddb.models import Album

        ctx = temp_dir("discdb2-")
        root = Path(ctx.__enter__())
        try:
            config = Config.default(root=root)
            config.ensure_dirs()
            store = Store(config.db_path)
            store.upsert_album(Album.from_dict({
                "key": "d2", "title": "T", "tracks": [], "source_ids": {}}))
            store.update_album_fields("d2", {
                "track_list": [{"title": "无碟号", "index": 1}],
            })
            album = store.get_album("d2")
            store.close()
            self.assertEqual(album.tracks[0].disc, 1)
        finally:
            ctx.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()