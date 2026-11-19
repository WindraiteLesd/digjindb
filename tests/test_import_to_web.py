"""一键导入线上: 导出字段 / 桥页 / 书签 / 填表逻辑.

用户要求:
    "站长端导出所有字段至缓存并自动打开新建专辑页面, 然后直接从缓存中
     填入对应的表单内容, 包括封面, 不动线上的任何代码不做接口,
     可以做成本地自动化脚本工具"

实现两条路:
    ① CDP 驱动浏览器自动填表(需要能启动浏览器)
    ② 桥页 + 书签(受限环境下的正路, 只靠浏览器自身)
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from tests._tmp import temp_path

ROOT = Path(__file__).resolve().parents[1]


def _make_db(path: Path) -> str:
    from dddb.db import Store
    from dddb.models import Album

    key = "test|artist|petal|2026"
    store = Store(path)
    try:
        store.upsert_album(Album.from_dict({
            "key": key, "title": "PETAL", "title_original": "PETAL",
            "title_romaji": "Petaru", "circle": "Winter Bouquet",
            "artists": ["Winter Bouquet"], "event": "M3-2026秋",
            "release_date": "2026-10-25", "catalog_no": "WB-001",
            "price": "1000", "hall": "第一展示場", "booth": "A-01a",
            "tags": ["东方"], "genres": ["J-Pop"],
            "description": "简介", "company": "WB",
            "credits": [{"role": "circle", "name": "Winter Bouquet"}],
            "urls": {"官网": "https://example.com/"},
            "tracks": [
                {"disc": 1, "index": 1, "title": "T1",
                 "artists": ["A"], "duration": 237},
                {"disc": 1, "index": 2, "title": "T2",
                 "artists": ["B"], "duration": 65},
            ],
            "source_ids": {},
        }))
    finally:
        store.close()
    return key


class TestExportAlbum(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = temp_path("imp-export")
        self.db = self.tmp / "dddb.sqlite3"
        self.key = _make_db(self.db)
        self.cache = self.tmp / "import_cache"

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_all_fields_exported(self) -> None:
        from dddb.import_to_web import export_album

        d = export_album(self.db, self.key, self.cache)
        self.assertEqual(d["title_original"], "PETAL")
        self.assertEqual(d["circle"], "Winter Bouquet")
        self.assertEqual(d["event"], "M3-2026秋")
        self.assertEqual(d["release_date"], "2026-10-25")
        self.assertEqual(d["catalog_no"], "WB-001")
        self.assertEqual(d["hall"], "第一展示場")
        self.assertEqual(d["booth"], "A-01a")
        self.assertEqual(d["tags"], "东方")
        self.assertEqual(d["genres"], "J-Pop")
        self.assertEqual(d["artists"], "Winter Bouquet")
        self.assertEqual(d["description"], "简介")

    def test_credits_urls_tracks(self) -> None:
        from dddb.import_to_web import export_album

        d = export_album(self.db, self.key, self.cache)
        self.assertEqual(len(d["credits"]), 1)
        self.assertEqual(d["credits"][0]["role"], "circle")
        self.assertEqual(len(d["urls"]), 1)
        self.assertEqual(d["urls"][0]["value"], "https://example.com/")
        self.assertEqual(len(d["tracks"]), 2)
        self.assertEqual(d["tracks"][0]["duration"], "3:57")
        self.assertEqual(d["tracks"][1]["duration"], "1:05")

    def test_cache_file_written(self) -> None:
        from dddb.import_to_web import export_album

        d = export_album(self.db, self.key, self.cache)
        self.assertTrue(Path(d["_cache_json"]).is_file())
        saved = json.loads(Path(d["_cache_json"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["title_original"], "PETAL")

    def test_missing_album_raises(self) -> None:
        from dddb.import_to_web import export_album

        with self.assertRaises(KeyError):
            export_album(self.db, "nope|nope", self.cache)

    def test_cover_from_covers_dir(self) -> None:
        """cover_local 为空时, 仍能从 covers/ 按 key 找到封面。"""
        import base64

        from dddb.import_to_web import export_album

        covers = self.tmp / "covers"
        covers.mkdir(exist_ok=True)
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
            "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
        (covers / "test-artist-petal-2026.img").write_bytes(png)
        d = export_album(self.db, self.key, self.cache)
        self.assertTrue(d.get("cover_file"), "没找到 covers/ 里的封面")
        self.assertTrue(Path(d["cover_file"]).is_file())


class TestBridgeOutput(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = temp_path("imp-bridge")
        self.db = self.tmp / "dddb.sqlite3"
        self.key = _make_db(self.db)
        self.cache = self.tmp / "import_cache"

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bridge_html_generated(self) -> None:
        from dddb.import_to_web import export_album

        d = export_album(self.db, self.key, self.cache,
                         base_url="https://x.test")
        bridge = Path(d["_bridge_html"])
        self.assertTrue(bridge.is_file(), "桥页没生成")
        html = bridge.read_text(encoding="utf-8")
        # 关键内容都在
        self.assertIn("PETAL", html)
        self.assertIn("https://x.test/new/album", html)
        self.assertIn("localStorage", html)
        # 书签是 javascript: 开头的一大段(嵌在 textarea 里)
        self.assertIn("javascript:", html)
        self.assertIn("3:57", html, "曲目时长没带进桥页")

    def test_bookmarklet_file(self) -> None:
        from dddb.import_to_web import export_album

        d = export_album(self.db, self.key, self.cache)
        bm = Path(d["_bookmarklet"])
        self.assertTrue(bm.is_file())
        text = bm.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("javascript:"),
                        "书签必须以 javascript: 开头")
        self.assertIn("localStorage", text)
        self.assertIn("title_original", text)

    def test_bookmarklet_fills_all_groups(self) -> None:
        """书签脚本要覆盖 单值/词条/外链/曲目 四组。"""
        from dddb.import_bridge import build_bookmarklet

        js = build_bookmarklet({"title_original": "T"}, "abc")
        for token in ("title_original", "credit_role", "url_key",
                      "track_title", "track_duration"):
            self.assertIn(token, js, f"书签缺少 {token}")

    def test_bookmarklet_no_unhandled_placeholder(self) -> None:
        """模板里的 {stem} 必须被替换干净。"""
        from dddb.import_bridge import build_bookmarklet

        js = build_bookmarklet({"title_original": "T"}, "abc123")
        self.assertIn("dddb_import_abc123", js)
        self.assertNotIn("{stem}", js)

    def test_bridge_storage_key_matches_bookmarklet(self) -> None:
        """桥页写入的 key 必须和书签读的一致, 否则永远取不到数据。"""
        from dddb.import_bridge import build_bookmarklet, build_bridge_html

        stem = "deadbeef1234"
        html = build_bridge_html({"title_original": "T"},
                                 base_url="https://x.test", stem=stem)
        js = build_bookmarklet({"title_original": "T"}, stem)
        self.assertIn("dddb_import_deadbeef1234", html)
        self.assertIn("dddb_import_deadbeef1234", js)


class TestFormFieldsMatchOnline(unittest.TestCase):
    """导出字段名必须与线上新建页的表单字段名一致。"""

    def test_names_in_online_form(self) -> None:
        from dddb.public import templates as T

        ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                            user={"id": 1, "username": "e", "role": "editor"})
        html = T.edit_album_page(ctx, album={},
                                 values=T.edit_values_from_album({}),
                                 creating=True)
        names = set(re.findall(r'name="([^"]+)"', html))

        # 我们导出的单值字段
        for field in ("title_original", "title", "title_romaji", "circle",
                      "company", "event", "release_date", "catalog_no",
                      "price", "hall", "booth", "tags", "genres",
                      "description", "artists"):
            self.assertIn(field, names, f"线上表单没有 {field}")

        # 重复行字段
        for field in ("credit_role", "credit_name", "url_key", "url_value"):
            self.assertIn(field, names, f"线上表单没有 {field}")

        # 曲目
        for field in ("track_disc", "track_index", "track_title",
                      "track_subtitle", "track_title_zh", "track_artists",
                      "track_lang", "track_genre", "track_duration"):
            self.assertIn(field, names, f"线上表单没有 {field}")

        # 封面上传框
        self.assertIn("cover", names)


class TestBrowserLauncher(unittest.TestCase):
    def test_find_browser(self) -> None:
        from dddb.import_to_web import find_browser

        try:
            exe = find_browser()
        except FileNotFoundError:
            self.skipTest("本机没装 Chrome/Edge")
        self.assertTrue(Path(exe).is_file())

    def test_cdp_stub_methods(self) -> None:
        """CDP 类的接口形状(不真连浏览器)."""
        from dddb.import_to_web import CDP

        cdp = CDP(port=9999)
        for name in ("version", "pages", "connect_page", "call", "eval_js",
                     "close"):
            self.assertTrue(hasattr(cdp, name), f"CDP 缺 {name}")


class TestNoOnlineChanges(unittest.TestCase):
    """不改线上代码 —— 新功能不得触碰 public/ 的写路径。"""

    def test_import_module_does_not_touch_public(self) -> None:
        src = (ROOT / "dddb/import_to_web.py").read_text(encoding="utf-8")
        self.assertNotIn("from .public", src,
                         "导入工具不该依赖线上站代码")

    def test_bridge_has_no_network(self) -> None:
        src = (ROOT / "dddb/import_bridge.py").read_text(encoding="utf-8")
        for bad in ("urllib.request", "requests", "http.client"):
            self.assertNotIn(bad, src, "桥页生成不该发网络请求")


if __name__ == "__main__":
    unittest.main()
