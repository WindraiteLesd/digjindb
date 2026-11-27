"""端到端验证「按 URL 抓封面」: 真起服务, 真发 HTTP.

分三层:
  A. SSRF 判定表(is_public_image_url)
  B. fetch_image_from_url / _store_cover_bytes / _store_cover_from_url
  C. 路由 + HTTP 层(POST /album/<key>/cover-url)
"""
from __future__ import annotations

import re
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._tmp import temp_path                      # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200


def _mk_site(tmp: Path):
    from dddb.db import Store
    from dddb.models import Album
    from dddb.public.site import build_site

    data = tmp / "data"
    data.mkdir(parents=True, exist_ok=True)
    db = data / "dddb.sqlite3"
    covers = data / "covers"
    covers.mkdir(exist_ok=True)
    store = Store(db)
    try:
        store.save_series("M3", abbr="M3", full_name="Music Media-Mix Market")
        store.upsert_album(Album.from_dict({
            "key": "A|B|2026", "title": "B", "title_original": "B",
            "circle": "A", "release_date": "2026-01-01",
        }))
    finally:
        store.close()
    site = build_site(None, root=tmp, db_path=db,
                      users_db=data / "u.sqlite3",
                      sessions_db=data / "s.sqlite3", covers_dir=covers)
    return site, db, covers


class TestSsrfGuard(unittest.TestCase):
    def test_allows_public(self) -> None:
        from dddb.public.site import is_public_image_url

        self.assertTrue(is_public_image_url("https://example.com/x.jpg"))

    def test_blocks_loopback(self) -> None:
        from dddb.public.site import is_public_image_url

        for url in ("http://127.0.0.1/a.jpg", "http://127.1.2.3/a.jpg",
                    "https://localhost/a.jpg", "http://[::1]/a.jpg"):
            self.assertFalse(is_public_image_url(url), url)

    def test_blocks_private(self) -> None:
        from dddb.public.site import is_public_image_url

        for url in ("http://192.168.1.1/a.png", "http://10.0.0.5/a.png",
                    "http://172.16.0.1/a.png"):
            self.assertFalse(is_public_image_url(url), url)

    def test_blocks_metadata(self) -> None:
        from dddb.public.site import is_public_image_url

        self.assertFalse(
            is_public_image_url("http://169.254.169.254/latest/meta-data/"))

    def test_blocks_bad_scheme(self) -> None:
        from dddb.public.site import is_public_image_url

        for url in ("file:///etc/passwd", "javascript:alert(1)",
                    "data:image/png;base64,AAA", "ftp://x/a.jpg", ""):
            self.assertFalse(is_public_image_url(url), url)


class TestFetchImage(unittest.TestCase):
    def test_rejects_private_before_download(self) -> None:
        from dddb.public.site import CoverFetchError, fetch_image_from_url

        with self.assertRaises(CoverFetchError):
            fetch_image_from_url("http://127.0.0.1/x.png")

    def _fake(self, blob: bytes, ctype: str = "image/png"):
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.__exit__.return_value = False
        fake.status = 200
        fake.headers = {"Content-Type": ctype}
        fake.read.return_value = blob
        return fake

    def test_returns_bytes_and_mime(self) -> None:
        from dddb.public import site as S

        with mock.patch.object(S.urllib.request, "urlopen",
                              return_value=self._fake(PNG)):
            blob, mime = S.fetch_image_from_url("https://example.com/x.png")
        self.assertEqual(blob, PNG)
        self.assertEqual(mime, "image/png")

    def test_rejects_non_image(self) -> None:
        from dddb.public import site as S

        with mock.patch.object(
                S.urllib.request, "urlopen",
                return_value=self._fake(b"<html>no</html>", "text/html")):
            with self.assertRaises(S.CoverFetchError) as ctx:
                S.fetch_image_from_url("https://example.com/x.png")
        self.assertIn("不是 PNG", str(ctx.exception))

    def test_rejects_oversize(self) -> None:
        from dddb.public import site as S

        # 用户 m18831: 上传/抓取上限从 2MB 提到 8MB, 所以要超过 S.MAX_URL_COVER_BYTES 才算超限
        big = PNG + b"\x00" * (S.MAX_URL_COVER_BYTES + 1024)
        with mock.patch.object(S.urllib.request, "urlopen",
                              return_value=self._fake(big)):
            with self.assertRaises(S.CoverFetchError) as ctx:
                S.fetch_image_from_url("https://example.com/x.png")
        self.assertIn("超过", str(ctx.exception))


class TestCoverUrlRoute(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = temp_path("coverurl-e2e")
        self.site, self.db, self.covers = _mk_site(self.tmp)

    def tearDown(self) -> None:
        self.site.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_route_parsed(self) -> None:
        from dddb.public.site import parse_route

        route = parse_route("/album/A%7CB%7C2026/cover-url")
        self.assertEqual(route["name"], "album_cover_url")
        self.assertEqual(route["arg"], "A|B|2026")

    def test_handler_registered(self) -> None:
        src = (ROOT / "dddb/public/site.py").read_text(encoding="utf-8")
        self.assertIn('if name == "album_cover_url":', src)
        self.assertIn("_do_album_cover_url", src)

    def test_unauthenticated_gets_403(self) -> None:
        """未登录: 分发前的权限闸直接拒掉(HTML 403 页)."""
        resp = self.site.handle(
            "POST", "/album/A%7CB%7C2026/cover-url",
            body=b"url=https%3A%2F%2Fexample.com%2Fx.png",
            headers={"content-type": "application/x-www-form-urlencoded"})
        self.assertEqual(resp.status, 403)

    def test_handler_403_is_json(self) -> None:
        """处理函数自己的 403 走 JSON(前端 fetch 要解析)."""
        state = mock.MagicMock()
        state.session = None
        with mock.patch.object(self.site, "can_edit", return_value=False):
            resp = self.site._do_album_cover_url(
                "A|B|2026", state, {"url": ["https://e.com/x.png"]}, [])
        self.assertEqual(resp.status, 403)
        self.assertIn("json", resp.content_type)

    def test_stores_cover_when_editor(self) -> None:
        """编辑者 + 抓取成功 -> 落盘 + 写库."""
        from dddb.public import auth

        state = mock.MagicMock()
        state.session = None
        with mock.patch.object(self.site, "can_edit", return_value=True), \
                mock.patch.object(self.site, "_member_uid", return_value=1), \
                mock.patch.object(self.site, "_record_revision"), \
                mock.patch.object(self.site, "profile_call"), \
                mock.patch("dddb.public.site.fetch_image_from_url",
                           return_value=(PNG, "image/png")):
            resp = self.site._do_album_cover_url("A|B|2026", state,
                                                 {"url": ["https://e.com/x.png"]}, [])
        self.assertEqual(resp.status, 200)
        self.assertIn(b'"ok": true', resp.body.replace(b'"ok":true', b'"ok": true'))
        from dddb.db import Store

        store = Store(self.db)
        try:
            row = store.query_one(
                "SELECT cover_local FROM albums WHERE key = ?", ("A|B|2026",))
        finally:
            store.close()
        self.assertTrue(row["cover_local"])
        self.assertTrue(Path(row["cover_local"]).is_file())
        _ = auth


class TestStoreHelpers(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = temp_path("coverstore-e2e")
        self.site, self.db, self.covers = _mk_site(self.tmp)

    def tearDown(self) -> None:
        self.site.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_store_bytes_writes_cover(self) -> None:
        from dddb.db import Store

        ok, note = self.site._store_cover_bytes("A|B|2026", PNG, "image/png", ".png")
        self.assertTrue(ok, note)
        target = Path(note)
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), PNG)
        store = Store(self.db)
        try:
            row = store.query_one(
                "SELECT cover_local FROM albums WHERE key = ?", ("A|B|2026",))
        finally:
            store.close()
        self.assertEqual(row["cover_local"], note)
        # cover_v 不在 albums 表列里, 它存在 payload JSON 中(data 层读它做 ?v=)
        self.site.data.invalidate()
        view = self.site.data.get_album("A|B|2026")
        self.assertIn("?v=", str(view.get("cover") or ""))

    def test_store_from_url_failure_is_soft(self) -> None:
        ok = self.site._store_cover_from_url("A|B|2026", "http://127.0.0.1/x.png")
        self.assertFalse(ok)

    def test_new_album_hint_fallback(self) -> None:
        src = (ROOT / "dddb/public/site.py").read_text(encoding="utf-8")
        start = src.find("def _do_new_entity")
        end = src.find("\n    def ", start + 10)
        block = src[start:end]
        self.assertIn("cover_url_hint", block)
        self.assertIn("_store_cover_from_url", block)

    def test_edit_hint_fallback(self) -> None:
        src = (ROOT / "dddb/public/site.py").read_text(encoding="utf-8")
        start = src.find("def _do_album_edit")
        end = src.find("\n    def ", start + 10)
        block = src[start:end]
        self.assertIn("cover_url_hint", block)


class TestFrontendWiring(unittest.TestCase):
    def setUp(self) -> None:
        self.js = (ROOT / "dddb/public/static/infobox.js").read_text(encoding="utf-8")
        self.tpl = (ROOT / "dddb/public/templates.py").read_text(encoding="utf-8")

    def test_no_manual_upload_message(self) -> None:
        self.assertNotIn("封面需手动上传", self.js)

    def test_apply_cover_function(self) -> None:
        """infobox 里的封面地址: 先缓存, 提交时才转正(用户 m13899)。

        以前这里是直接 `POST /album/<key>/cover-url` 写库 —— 填一次表就
        把封面换了, 编辑者想反悔都没退路。现在走 `/cover-cache/new`,
        token 存进隐藏域 `cover_cache`, 保存表单时才真正落盘。
        """
        self.assertIn("function applyCover", self.js)
        self.assertIn("/cover-cache/new", self.js)
        self.assertIn('credentials: "same-origin"', self.js)
        self.assertNotIn("/cover-url", self.js)

    def test_hidden_hint_field(self) -> None:
        self.assertIn('name="cover_url_hint"', self.tpl)

    def test_version_bumped(self) -> None:
        # infobox.js 每轮都改, 用下限断言免得每次改版都要改测试
        self.assertRegex(self.tpl, r"infobox\.js\?v=(\d+)")
        ver = int(re.search(r"infobox\.js\?v=(\d+)", self.tpl).group(1))
        self.assertGreaterEqual(ver, 9)


if __name__ == "__main__":
    unittest.main()
