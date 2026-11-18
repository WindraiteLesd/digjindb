"""回归测试: multipart 表单必须保留重复字段的全部值.

背景(真实事故): 编辑专辑表单是 enctype=multipart/form-data,
`url_key` / `url_value` / `credit_role` / `track_title` 每行一个、字段名重复。
`_values_from_multipart` 曾借用 `parse_multipart()` 得到 `dict[str, str]`,
同名只保留**最后一个** —— 于是"保存一次编辑, 外链 5 条变 0 条, 词条也没了"。
"""

from __future__ import annotations

import unittest

from tests._tmp import temp_dir
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]


def build_multipart(fields: list[tuple[str, str]],
                    files: list[tuple[str, str, bytes]] | None = None,
                    boundary: str = "----dddbTestBoundary") -> tuple[bytes, str]:
    """构造浏览器风格的 multipart 请求体(同名可重复)."""
    crlf = "\r\n"
    parts: list[str] = []
    for name, value in fields:
        parts.append(
            "--" + boundary + crlf
            + 'Content-Disposition: form-data; name="' + name + '"' + crlf
            + crlf + value + crlf)
    body = "".join(parts).encode("utf-8")
    for name, filename, blob in files or []:
        body += (
            "--" + boundary + crlf
            + 'Content-Disposition: form-data; name="' + name
            + '"; filename="' + filename + '"' + crlf
            + "Content-Type: image/png" + crlf + crlf).encode("utf-8")
        body += blob + crlf.encode()
    body += ("--" + boundary + "--" + crlf).encode()
    return body, "multipart/form-data; boundary=" + boundary


class TestMultipartRepeatedFields(unittest.TestCase):
    """重复字段一个都不能丢。"""

    def setUp(self) -> None:
        from dddb.public.site import PublicSite

        self.handler = PublicSite.__new__(PublicSite)

    def _parse(self, fields, files=None):
        body, ctype = build_multipart(fields, files)
        return self.handler._values_from_multipart({"content-type": ctype}, body)

    def test_repeated_url_fields_kept(self) -> None:
        values = self._parse([
            ("csrf_token", "T"),
            ("url_key", "circle_site"),
            ("url_value", "https://a.com/"),
            ("url_key", "x"),
            ("url_value", "https://x.com/b"),
            ("url_key", "youtube"),
            ("url_value", "https://youtube.com/watch?v=1"),
        ])
        self.assertEqual(values["url_key"],
                         ["circle_site", "x", "youtube"])
        self.assertEqual(len(values["url_value"]), 3)

    def test_repeated_credit_fields_kept(self) -> None:
        values = self._parse([
            ("credit_role", "作曲"),
            ("credit_name", "某人"),
            ("credit_role", "编曲"),
            ("credit_name", "另一人"),
        ])
        self.assertEqual(values["credit_role"], ["作曲", "编曲"])
        self.assertEqual(values["credit_name"], ["某人", "另一人"])

    def test_repeated_track_fields_kept(self) -> None:
        values = self._parse([
            ("track_title", "曲一"),
            ("track_artists", "A"),
            ("track_title", "曲二"),
            ("track_artists", "B"),
            ("track_title", "曲三"),
        ])
        self.assertEqual(values["track_title"], ["曲一", "曲二", "曲三"])

    def test_file_field_kept_as_dict(self) -> None:
        """文件字段要**保留**成 dict, 否则上传的图片会被静默丢掉.

        早先这里断言"文件字段跳过" —— 那正是"上传了但图片不显示"的根因:
        ``_save_series_image`` / ``_save_album_cover_file`` 从 values 里取
        文件内容, 而 values 里根本没有它。
        """
        values = self._parse(
            [("csrf_token", "T"), ("title", "标题")],
            files=[("cover", "a.png", b"\x89PNG\r\n\x1a\nBINARY")])
        self.assertIn("csrf_token", values)
        self.assertIn("title", values)
        self.assertIn("cover", values, "文件字段不能被丢掉")
        cover = values["cover"]
        self.assertIsInstance(cover, dict)
        self.assertEqual(cover["filename"], "a.png")
        self.assertEqual(cover["data"], b"\x89PNG\r\n\x1a\nBINARY")

    def test_blank_values_kept(self) -> None:
        """空值也要保留(否则行数对不齐)。"""
        values = self._parse([
            ("url_key", "a"),
            ("url_value", ""),
            ("url_key", "b"),
            ("url_value", "https://b.com/"),
        ])
        self.assertEqual(values["url_value"], ["", "https://b.com/"])

    def test_utf8_values(self) -> None:
        values = self._parse([("title", "日本語のタイトル"),
                              ("circle", "サークル")])
        self.assertEqual(values["title"], ["日本語のタイトル"])


class TestEditFormRoundTrip(unittest.TestCase):
    """端到端: 渲染编辑页 -> 原样提交 -> 外链/词条/曲目不许丢."""

    def setUp(self) -> None:
        self._ctx = temp_dir("mp-")
        self.root = Path(self._ctx.__enter__())
        from dddb.config import Config
        from dddb.db import Store
        from dddb.models import Album

        self.config = Config.default(root=self.root)
        self.config.ensure_dirs()
        store = Store(self.config.db_path)
        store.upsert_album(Album.from_dict({
            "key": "rt", "title": "往返测试", "circle": "C",
            "artists": [], "source_ids": {},
            "urls": {"circle_site": "https://a.com/",
                     "x": "https://x.com/b",
                     "youtube": "https://youtube.com/watch?v=1"},
            "credits": [{"role": "作曲", "name": "甲"},
                        {"role": "编曲", "name": "乙"}],
            "tracks": [{"title": "曲一", "artists": ["A"]},
                       {"title": "曲二", "artists": ["B"]}],
        }))
        store.close()

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def test_roundtrip_keeps_everything(self) -> None:
        import re

        from dddb.db import Store
        from dddb.public import templates as T
        from dddb.public.site import PublicSite, parse_album_form

        store = Store(self.config.db_path)
        album = store.get_album("rt")
        ctx = T.SiteContext(csrf_token="T", is_editor=True, lang="zh",
                            user={"id": 1, "username": "ed", "role": "editor"})
        html = T.edit_album_page(ctx, album=album,
                                 values=T.edit_values_from_album(album))
        store.close()

        # 从渲染出的 HTML 里抓出所有字段(模拟浏览器提交)
        fields: list[tuple[str, str]] = []
        for match in re.finditer(
                r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', html):
            name, value = match.groups()
            fields.append((name, value))
        # textarea 类字段(urls 用 input, tracks/credits 用 input)
        handler = PublicSite.__new__(PublicSite)
        body, ctype = build_multipart(fields)
        values = handler._values_from_multipart({"content-type": ctype}, body)

        patch, errors = parse_album_form(values, current=album.to_dict())
        self.assertEqual(errors, {}, "不该有校验错误")

        # 关键: 三件套必须原样保留
        self.assertEqual(len(patch.get("urls") or {}), 3,
                         "外链不该丢")
        self.assertEqual(len(patch.get("credits") or []), 2,
                         "词条不该丢")
        self.assertEqual(len(patch.get("track_list") or []), 2,
                         "曲目不该丢")


class TestMultipartVsUrlencodedParity(unittest.TestCase):
    """multipart 与 urlencoded 解析结果必须同形(都是 {key: [values]})."""

    def test_same_shape(self) -> None:
        from dddb.public.site import PublicSite, form_values

        handler = PublicSite.__new__(PublicSite)
        body, ctype = build_multipart([
            ("title", "T"), ("url_key", "a"), ("url_value", "https://a/"),
        ])
        mp = handler._values_from_multipart({"content-type": ctype}, body)

        # urlencoded 等价体
        from urllib.parse import urlencode

        ue = form_values(urlencode({
            "title": "T", "url_key": "a", "url_value": "https://a/",
        }).encode())

        self.assertEqual(sorted(mp.keys()), sorted(ue.keys()))
        for key in ue:
            self.assertEqual(mp[key], ue[key],
                             f"{key} 的解析结果不一致")


if __name__ == "__main__":
    unittest.main()