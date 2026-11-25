"""infobox 封面「先缓存、提交才转正」(用户 m13899).

背景: 以前 infobox 里一带封面地址, 前端就立刻 ``POST /album/<key>/cover-url``
把它写进库 —— 编辑者只是粘一段词条试试也会改掉封面, 而且换图之后旧图
已经被覆盖、没法退回。现在拆成两步:

* ``POST /cover-cache/new`` 只抓下来存进 ``covers/pending/<token>``,
  页面用 ``GET /cover-cache/<token>`` 预览(no-store);
* 真正提交表单时才由 ``_apply_pending_cover`` 转成正式封面;
* 用户自己传了图 -> 缓存作废并删掉, 以用户提交的为准。
"""

from __future__ import annotations

import base64
import re
import shutil
import unittest
from pathlib import Path

from tests import _tmp

from dddb.public import site as site_mod

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
    "DwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

BOUNDARY = "----dddb-test"


def _multipart(fields, files=None):
    """拼一份 multipart 请求体, 返回 (headers, body)."""
    parts = []
    for name, value in fields:
        parts.append(
            '--{}\r\nContent-Disposition: form-data; name="{}"\r\n\r\n{}'
            .format(BOUNDARY, name, value).encode("utf-8"))
    for name, filename, ctype, data in (files or []):
        parts.append(
            '--{}\r\nContent-Disposition: form-data; name="{}"; filename="{}"'
            '\r\nContent-Type: {}\r\n\r\n'.format(
                BOUNDARY, name, filename, ctype).encode("utf-8") + data)
    body = b"\r\n".join(parts) + b"\r\n--" + BOUNDARY.encode() + b"--\r\n"
    return ({"content-type": "multipart/form-data; boundary=" + BOUNDARY},
            body)


class _SiteCase(unittest.TestCase):
    """起一个真的 PublicSite(临时库), 但把鉴权/CSRF 短路成编辑者."""

    def setUp(self):
        from dddb import db as db_mod
        from dddb.public import auth
        from dddb.public.site import build_site

        self.tmp = _tmp.temp_path("covercache")
        self.data_dir = self.tmp / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "t.sqlite3"
        self.covers = self.data_dir / "covers"
        self.covers.mkdir()
        db_mod.Store(self.db_path).close()

        self.site = build_site(
            None, root=self.tmp, db_path=self.db_path,
            users_db=self.data_dir / "u.sqlite3",
            sessions_db=self.data_dir / "s.sqlite3",
            covers_dir=self.covers)
        state = auth.AuthState()
        state.session = auth.Session(sid="t", user_id=1, username="e",
                                     role="editor")
        state.csrf_token = "tk"
        self.site.sessions.state = lambda cookies: state          # type: ignore
        self.site.same_origin = lambda *a, **k: (True, "")        # type: ignore
        self.site.sessions.signer.check = lambda b, g: True       # type: ignore
        self.site.can_edit = lambda s: True                       # type: ignore
        self.auth_state = state

    def tearDown(self):
        self.site.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- 小工具 ----------------------------------------------------------
    @property
    def pending(self) -> Path:
        folder = self.covers / "pending"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def fake_cache(self, token="0123456789abcdef", blob=PNG) -> str:
        """绕过网络抓取, 直接放一个"已经缓存好"的图."""
        (self.pending / (token + ".png")).write_bytes(blob)
        return token

    def post(self, path, headers, body):
        return self.site.handle(
            "POST", path,
            headers={**headers, "host": "127.0.0.1:9",
                     "origin": "http://127.0.0.1:9"}, body=body)

    def get(self, path):
        return self.site.handle("GET", path)


class TestPendingCoverFiles(_SiteCase):
    def test_token_must_be_16_hex(self):
        self.assertIsNone(self.site._pending_cover_path(""))
        self.assertIsNone(self.site._pending_cover_path("../etc/passwd"))
        self.assertIsNone(self.site._pending_cover_path("zzzz"))
        self.assertIsNone(self.site._pending_cover_path("0" * 15))
        token = self.fake_cache()
        self.assertIsNotNone(self.site._pending_cover_path(token))

    def test_get_serves_image_with_no_store(self):
        token = self.fake_cache()
        resp = self.get("/cover-cache/" + token)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("Cache-Control"), "no-store")

    def test_get_unknown_token_404(self):
        for bad in ("../x", "zzzz", "0" * 15, "f" * 16):
            self.assertEqual(self.get("/cover-cache/" + bad).status, 404, bad)

    def test_purge_removes_expired(self):
        token = self.fake_cache()
        path = self.pending / (token + ".png")
        old = path.stat().st_mtime - site_mod.PENDING_COVER_TTL - 60
        import os
        os.utime(path, (old, old))
        self.site._purge_pending_covers(self.pending)
        self.assertFalse(path.exists())

    def test_purge_keeps_fresh(self):
        token = self.fake_cache()
        self.site._purge_pending_covers(self.pending)
        self.assertTrue((self.pending / (token + ".png")).exists())


class TestPromoteOnSubmit(_SiteCase):
    def test_create_album_promotes_cache(self):
        token = self.fake_cache()
        headers, body = _multipart([
            ("csrf_token", "tk"), ("title_original", "缓存封面专辑"),
            ("circle", "测社团"), ("cover_cache", token),
        ])
        resp = self.post("/new/album", headers, body)
        self.assertIn(resp.status, (302, 303))
        self.assertIn("/album/", resp.headers.get("Location") or "")
        # 建完直接进详情页(用户 m13494), 不再回编辑页
        self.assertIn("created=1", resp.headers.get("Location") or "")

        from dddb import db as db_mod
        store = db_mod.Store(self.db_path)
        try:
            keys = [row["key"] for row in store.query("SELECT key FROM albums")]
            album = store.get_album(keys[0])
        finally:
            store.close()
        self.assertTrue(album.cover_local, "缓存图没有转成正式封面")
        self.assertFalse((self.pending / (token + ".png")).exists(),
                         "转正之后缓存文件该删掉")

    def test_user_file_wins_and_cache_is_dropped(self):
        token = self.fake_cache()
        headers, body = _multipart(
            [("csrf_token", "tk"), ("title_original", "自带图"),
             ("cover_cache", token)],
            [("cover", "mine.png", "image/png", PNG)])
        self.post("/new/album", headers, body)
        self.assertFalse((self.pending / (token + ".png")).exists(),
                         "用户自己传了图 -> 云端缓存要删掉")

    def test_no_token_is_noop(self):
        self.assertFalse(self.site._apply_pending_cover(
            "k", {}, None, b""))

    def test_edit_path_promotes(self):
        from dddb.models import Album
        from dddb import db as db_mod
        store = db_mod.Store(self.db_path)
        store.upsert_album(Album.from_dict(
            {"key": "e|b|2026", "title": "E", "circle": "C"}))
        store.close()

        token = self.fake_cache("aaaaaaaaaaaaaaaa")
        headers, body = _multipart([
            ("csrf_token", "tk"), ("title_original", "E"),
            ("circle", "C"), ("cover_cache", token),
        ])
        resp = self.post("/album/e|b|2026/edit", headers, body)
        self.assertIn(resp.status, (302, 303))
        store = db_mod.Store(self.db_path)
        try:
            album = store.get_album("e|b|2026")
        finally:
            store.close()
        self.assertTrue(album.cover_local)


class TestCoverCacheWiring(unittest.TestCase):
    """前端/模板/路由这三处必须接得上, 否则功能是死的."""

    def test_routes_registered(self):
        src = Path(site_mod.__file__).read_text(encoding="utf-8")
        self.assertIn('"cover-cache": "pending_cover"', src)
        self.assertIn('"pending_cover"', src.split("_ENTITY_ROUTES")[0])

    def test_infobox_js_uses_cache_endpoint(self):
        js = (Path(site_mod.__file__).parent / "static" / "infobox.js"
              ).read_text(encoding="utf-8")
        self.assertIn("/cover-cache/new", js)
        self.assertIn("cover-cache", js)
        # 不再直接写库 —— 填表阶段不该产生持久效果
        self.assertNotIn("/cover-url", js)
        self.assertIn("点保存才会真正存下来", js)

    def test_public_js_clears_cache_when_user_picks(self):
        js = (Path(site_mod.__file__).parent / "static" / "public.js"
              ).read_text(encoding="utf-8")
        self.assertIn('getElementById("cover-cache")', js)
        # 用户选图 -> 把缓存 token 清空(服务端据此不转正)
        self.assertRegex(js, r"cached\.value = \"\";")

    def test_draft_keeps_cache_token(self):
        js = (Path(site_mod.__file__).parent / "static" / "public.js"
              ).read_text(encoding="utf-8")
        self.assertIn("extra.cover_cache", js)
        self.assertIn("extra.cover_cache", js.split("function applyExtra")[1])

    def test_templates_have_hidden_field(self):
        from dddb.public import templates as T

        for creating in (True, False):
            html = T.edit_album_page(
                T.SiteContext(csrf_token="t", is_editor=True, lang="zh"),
                album={} if creating else {"key": "a|b|2026", "title": "A"},
                values={}, creating=creating)
            self.assertIn('name="cover_cache"', html, creating)
            self.assertIn('id="cover-cache"', html, creating)


if __name__ == "__main__":
    unittest.main()
