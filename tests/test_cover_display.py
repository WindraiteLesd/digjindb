"""编辑页封面上传后必须能看见新图.

用户截图: 点了「上传封面」, 页面仍显示「无封面」。

服务端实测**正常**(上传后 /cover 返回新图, 编辑页 HTML 也含 img),
所以问题在前端:
    ① ``fetch(..., {redirect: "manual"})`` 在不同浏览器返回形态不一
       (opaqueredirect / status 0 / 空 body), 容易误判成失败 -> 不刷新
    ② 成功后 ``location.href = next`` 没带 cache-bust, 可能用缓存的旧编辑页
    ③ 编辑页 <img src="/cover/..."> 是长缓存, 刷新后仍显示旧图
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests._tmp import temp_path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "dddb/public/static/public.js"


class TestFrontendNoManualRedirect(unittest.TestCase):
    def test_no_redirect_manual(self) -> None:
        js = JS.read_text(encoding="utf-8")
        block = js.split("function setupCoverUpload(")[1].split("\n  function ")[0]
        self.assertNotIn('redirect: "manual"', block,
                         "manual 模式在部分浏览器会误判失败")

    def test_refresh_with_cache_bust(self) -> None:
        js = JS.read_text(encoding="utf-8")
        block = js.split("function setupCoverUpload(")[1].split("\n  function ")[0]
        self.assertIn("Date.now()", block, "跳转没带时间戳, 可能用缓存页")

    def test_shows_server_error(self) -> None:
        """失败时要把服务端的原因显示出来。"""
        js = JS.read_text(encoding="utf-8")
        block = js.split("function setupCoverUpload(")[1].split("\n  function ")[0]
        self.assertIn("empty-title", block,
                      "没解析服务端错误页, 用户只看到 HTTP xxx")


class TestEditPageImgCacheBust(unittest.TestCase):
    def test_img_has_version_param(self) -> None:
        src = (ROOT / "dddb/public/templates.py").read_text(encoding="utf-8")
        idx = src.find("_cover_url = esc(cover_src(key, album))")
        self.assertGreater(idx, 0)
        window = src[idx:idx + 400]
        self.assertIn("cover_v", window,
                      "编辑页封面图没带版本号, 上传后可能显示缓存图")
        self.assertIn("?", window)


class TestUploadWritesNewStem(unittest.TestCase):
    """上传写入的文件名必须是唯一的(前一问题的修复不能被回退)。"""

    def test_uses_cover_stem(self) -> None:
        src = (ROOT / "dddb/public/site.py").read_text(encoding="utf-8")
        self.assertIn("_cover_stem(key)", src)
        self.assertNotIn(
            're.sub(r"[^A-Za-z0-9._-]+", "-", str(key))', src)


class TestUploadRoundTrip(unittest.TestCase):
    """服务端端到端: 上传 -> 库里更新 -> 编辑页 HTML 含 img。"""

    def test_upload_then_rendered(self) -> None:
        import base64
        import re as _re

        from dddb.db import Store
        from dddb.models import Album
        from dddb.public import templates as T
        from dddb.public.data import PublicData, ReadOnlyStore
        from dddb.public.site import _cover_stem

        tmp = temp_path("cover-rt")
        data = tmp / "data"
        data.mkdir(parents=True)
        covers = data / "covers"
        covers.mkdir()
        db = data / "dddb.sqlite3"
        key = "サヨナラ|test|2026"

        store = Store(db)
        store.upsert_album(Album.from_dict({
            "key": key, "title": "サヨナラ", "title_original": "サヨナラ",
            "event": "M3-2026秋", "source_ids": {}}))
        store.close()

        try:
            # 模拟上传写盘
            png = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4"
                "2mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
            stem = _cover_stem(key)
            target = covers / f"{stem}.img"
            target.write_bytes(png)

            store = Store(db)
            store.update_album_fields(key, {
                "cover_local": str(target), "cover_v": "1795000000"})
            store.close()

            # 数据层
            rs = ReadOnlyStore(db)
            pub = PublicData(rs, root=tmp, covers_dir=covers)
            album = pub.get_album(key)
            rs.close()

            self.assertTrue(album.get("has_cover"), "has_cover 没置上")
            self.assertEqual(album.get("cover_local"), str(target))

            # 模板层: 编辑页必须出 <img>
            ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                                user={"id": 1, "username": "e",
                                      "role": "editor"})
            html = T.edit_album_page(
                ctx, album=album,
                values=T.edit_values_from_album(album), creating=False)
            m = _re.search(r'<img[^>]*edit-cover-img[^>]*>', html)
            self.assertIsNotNone(m, "编辑页没渲染封面 img")
            # 只要带 v= 参数即可(具体值由 cover_src 决定, 可能是 mtime)
            self.assertRegex(m.group(0), r"\?v=\d+",
                             "img src 没带版本号, 上传后可能显示缓存图")
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
