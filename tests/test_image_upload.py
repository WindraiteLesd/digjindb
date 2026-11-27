"""图片上传必须真正生效(端到端).

历史 bug:
    ``_values_from_multipart`` 里对带 ``filename=`` 的字段直接 ``continue`` ——
    上传的图片被静默丢掉, ``_save_series_image`` / ``_save_album_cover_file``
    永远读不到内容。表现就是"图片上传了但不显示"。
"""

from __future__ import annotations

import base64
import unittest
from pathlib import Path

from tests._tmp import temp_path

ROOT = Path(__file__).resolve().parents[1]

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
    "hQGAhKmMIQAAAABJRU5ErkJggg==")

BOUNDARY = "----dddbtest"


def _multipart(fields: dict[str, str],
               files: dict[str, tuple[str, str, bytes]]) -> tuple[dict, bytes]:
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            ('--{}\r\nContent-Disposition: form-data; name="{}"\r\n\r\n{}'
             .format(BOUNDARY, name, value)).encode("utf-8"))
    for name, (filename, ctype, data) in files.items():
        parts.append(
            ('--{}\r\nContent-Disposition: form-data; name="{}";'
             ' filename="{}"\r\nContent-Type: {}\r\n\r\n'
             .format(BOUNDARY, name, filename, ctype)).encode("utf-8") + data)
    body = (b"\r\n".join(parts)
            + "\r\n--{}--\r\n".format(BOUNDARY).encode("utf-8"))
    headers = {"content-type":
               "multipart/form-data; boundary={}".format(BOUNDARY)}
    return headers, body


class TestMultipartKeepsFiles(unittest.TestCase):
    """解析结果必须同时含文本字段(列表)和文件字段(dict)。"""

    def setUp(self) -> None:
        from dddb.public.site import build_site

        self.tmp = temp_path("mp-files")
        (self.tmp / "covers").mkdir(parents=True, exist_ok=True)
        from dddb.db import Store as _Store
        _Store(self.tmp / "t.sqlite3").close()   # 建库
        self.site = build_site(
            None, root=self.tmp, db_path=self.tmp / "t.sqlite3",
            users_db=self.tmp / "u.sqlite3",
            sessions_db=self.tmp / "s.sqlite3",
            covers_dir=self.tmp / "covers")

    def tearDown(self) -> None:
        import shutil

        self.site.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_file_field_present(self) -> None:
        headers, body = _multipart(
            {"name": "M3", "wiki": "# hi"},
            {"logo": ("logo.png", "image/png", PNG)})
        values = self.site._values_from_multipart(headers, body)
        # 文本字段仍是 list
        self.assertEqual(values["name"], ["M3"])
        self.assertEqual(values["wiki"], ["# hi"])
        # 文件字段是 dict, 且带内容
        logo = values.get("logo")
        self.assertIsInstance(logo, dict, "文件字段被丢掉了")
        self.assertEqual(logo["filename"], "logo.png")
        self.assertEqual(logo["content_type"], "image/png")
        self.assertEqual(logo["data"], PNG)

    def test_repeated_fields_keep_all(self) -> None:
        """同名文本字段仍要保留全部值(曲目/词条/外链依赖这个)。"""
        parts = []
        for v in ("a", "b", "c"):
            parts.append(
                ('--{}\r\nContent-Disposition: form-data; name="url_key"'
                 '\r\n\r\n{}'.format(BOUNDARY, v)).encode())
        body = (b"\r\n".join(parts)
                + "\r\n--{}--\r\n".format(BOUNDARY).encode())
        headers = {"content-type":
                   "multipart/form-data; boundary={}".format(BOUNDARY)}
        values = self.site._values_from_multipart(headers, body)
        self.assertEqual(values["url_key"], ["a", "b", "c"])

    def test_empty_file_input_ignored(self) -> None:
        """没选文件的空 input 不该当成"要清空"。"""
        headers, body = _multipart(
            {"name": "M3"},
            {"logo": ("", "application/octet-stream", b"")})
        values = self.site._values_from_multipart(headers, body)
        self.assertNotIn("logo", values)


class TestSeriesImageUpload(unittest.TestCase):
    """上传 -> 落盘 -> 入库 -> 页面可访问."""

    def setUp(self) -> None:
        from dddb.db import Store
        from dddb.public.site import build_site

        self.tmp = temp_path("series-img")
        self.db = self.tmp / "t.sqlite3"
        self.covers = self.tmp / "covers"
        self.covers.mkdir(parents=True, exist_ok=True)
        store = Store(self.db)
        store.save_series("M3", abbr="M3",
                          full_name="Music Media-Mix Market")
        store.close()
        self.site = build_site(
            None, root=self.tmp, db_path=self.db,
            users_db=self.tmp / "u.sqlite3",
            sessions_db=self.tmp / "s.sqlite3", covers_dir=self.covers)

    def tearDown(self) -> None:
        import shutil

        self.site.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_upload_saves_and_serves(self) -> None:
        from dddb.db import Store

        headers, body = _multipart(
            {"name": "M3", "abbr": "M3",
             "full_name": "Music Media-Mix Market"},
            {"logo": ("logo.png", "image/png", PNG)})
        values = self.site._values_from_multipart(headers, body)

        saved = self.site._save_series_image("M3", "logo", values)
        self.assertTrue(saved, "图片没落盘")
        self.assertTrue(Path(saved).is_file())
        self.assertEqual(Path(saved).read_bytes(), PNG)

        # 入库
        store = Store(self.db)
        store.save_series("M3", abbr="M3",
                          full_name="Music Media-Mix Market",
                          logo_local=saved)
        got = store.get_series("M3")
        store.close()
        self.assertEqual(got["logo_local"], saved)

        # 页面数据给的是站内路由
        from dddb.public.data import PublicData, ReadOnlyStore

        rs = ReadOnlyStore(self.db)
        pub = PublicData(rs, root=self.tmp, covers_dir=self.covers)
        items = pub.event_series()
        rs.close()
        self.assertTrue(items[0]["has_logo"])
        self.assertTrue(items[0]["logo"].startswith("/event-series-logo/"))

        # 路由能返回真实字节
        resp = self.site.serve_series_image("M3", "logo")
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.body, PNG)

    def test_poster_independent_from_logo(self) -> None:
        from dddb.db import Store

        headers, body = _multipart(
            {"name": "M3"},
            {"poster": ("p.png", "image/png", PNG)})
        values = self.site._values_from_multipart(headers, body)
        saved = self.site._save_series_image("M3", "poster", values)
        self.assertTrue(saved)
        # 只存了 poster, logo 仍为空
        store = Store(self.db)
        store.save_series("M3", abbr="M3",
                          full_name="Music Media-Mix Market",
                          poster_local=saved)
        got = store.get_series("M3")
        store.close()
        self.assertEqual(got["logo_local"], "")
        self.assertTrue(got["poster_local"])

    def test_bad_mime_rejected(self) -> None:
        headers, body = _multipart(
            {"name": "M3"},
            {"logo": ("x.txt", "text/plain", b"hello")})
        values = self.site._values_from_multipart(headers, body)
        self.assertEqual(self.site._save_series_image("M3", "logo", values), "")

    def test_oversize_rejected(self) -> None:
        # 用户 m18831: 图片上限提到 8MB, 要超过新上限才会被拒
        from dddb.public.site import MAX_IMAGE_BODY
        big = b"\x89PNG" + b"0" * (MAX_IMAGE_BODY + 1024)
        headers, body = _multipart(
            {"name": "M3"},
            {"logo": ("big.png", "image/png", big)})
        values = self.site._values_from_multipart(headers, body)
        self.assertEqual(self.site._save_series_image("M3", "logo", values), "")


class TestAlbumCoverUpload(unittest.TestCase):
    """专辑封面走同一条路径, 也要能取到文件。"""

    def test_cover_field_present(self) -> None:
        from dddb.public.site import build_site

        tmp = temp_path("album-cover")
        (tmp / "covers").mkdir(parents=True, exist_ok=True)
        from dddb.db import Store as _Store
        _Store(tmp / "t.sqlite3").close()       # 建库
        site = build_site(None, root=tmp, db_path=tmp / "t.sqlite3",
                          users_db=tmp / "u.sqlite3",
                          sessions_db=tmp / "s.sqlite3",
                          covers_dir=tmp / "covers")
        try:
            headers, body = _multipart(
                {"title_original": "T"},
                {"cover": ("c.png", "image/png", PNG)})
            values = site._values_from_multipart(headers, body)
            self.assertIn("cover", values)
            self.assertEqual(values["cover"]["data"], PNG)
        finally:
            site.close()
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
