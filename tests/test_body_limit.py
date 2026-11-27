"""请求体上限: 图片上传不能被 413 挡住.

用户实测: "413 提交内容过大, 提交内容被清空不保存, 图片不上传"

根因: ``MAX_BODY = 256KB``, 而表单允许上传 **8MB** 图片(用户 m18831 从 2MB
提到 8MB, 因为很多导出封面本身就超过 2MB) —— 一张稍大的图
直接 413, 整个表单白填。
"""

from __future__ import annotations

import base64
import unittest
from pathlib import Path

from tests._tmp import temp_path

ROOT = Path(__file__).resolve().parents[1]

PNG_HEAD = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
    "hQGAhKmMIQAAAABJRU5ErkJggg==")

B = "----limitbnd"


def _body(files: dict[str, tuple[str, str, bytes]],
          fields: dict[str, str] | None = None) -> tuple[dict, bytes]:
    parts = []
    for k, v in (fields or {}).items():
        parts.append(
            ('--{}\r\nContent-Disposition: form-data; name="{}"\r\n\r\n{}'
             .format(B, k, v)).encode())
    for k, (fn, ct, data) in files.items():
        parts.append(
            ('--{}\r\nContent-Disposition: form-data; name="{}";'
             ' filename="{}"\r\nContent-Type: {}\r\n\r\n'
             .format(B, k, fn, ct)).encode() + data)
    raw = b"\r\n".join(parts) + b"\r\n--" + B.encode() + b"--\r\n"
    return {"content-type": "multipart/form-data; boundary={}".format(B)}, raw


class TestBodyLimitLargeEnough(unittest.TestCase):
    """上限必须容得下一张 2MB 图。"""

    def test_limit_above_max_image(self) -> None:
        from dddb.public.site import MAX_BODY, MAX_IMAGE_BODY

        self.assertGreater(MAX_BODY, MAX_IMAGE_BODY,
                           "请求体上限必须大于单图上限, 否则图永远传不上")
        self.assertGreaterEqual(MAX_BODY, 4 * 1024 * 1024,
                                "至少要能放一张 8MB 图 + 表单文本")

    def test_image_limit_allows_big_export_covers(self) -> None:
        """用户 m18831: 很多导出封面超过 2MB, 单图上限必须高于它。"""
        from dddb.public.site import MAX_IMAGE_BODY

        self.assertGreaterEqual(MAX_IMAGE_BODY, 8 * 1024 * 1024,
                                "单图上限不能低于 8MB")

    def test_two_megabyte_body_fits(self) -> None:
        from dddb.public.site import MAX_BODY

        self.assertLess(2 * 1024 * 1024, MAX_BODY)
        self.assertLess(8 * 1024 * 1024, MAX_BODY,
                        "8MB 图 + 表单文本也必须放得下")
        self.assertGreater(MAX_BODY, 256 * 1024,
                           "旧的 256KB 是 413 的根因")


class TestLargeImageUpload(unittest.TestCase):
    """1.9MB 图片要走完全程。"""

    def setUp(self) -> None:
        from dddb.db import Store
        from dddb.public.site import build_site

        self.tmp = temp_path("big-upload")
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True)
        self.db = self.data / "dddb.sqlite3"
        self.covers = self.data / "covers"
        self.covers.mkdir()
        store = Store(self.db)
        store.save_series("M3", abbr="M3",
                          full_name="Music Media-Mix Market")
        store.close()
        self.site = build_site(
            None, root=self.tmp, db_path=self.db,
            users_db=self.data / "u.sqlite3",
            sessions_db=self.data / "s.sqlite3", covers_dir=self.covers)

    def tearDown(self) -> None:
        import shutil

        self.site.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_nineteen_hundred_kb(self) -> None:
        big = PNG_HEAD + b"\x00" * (1900 * 1024)
        headers, raw = _body({"logo": ("big.png", "image/png", big)},
                             {"name": "M3"})
        values = self.site._values_from_multipart(headers, raw)
        self.assertIn("logo", values)
        self.assertEqual(len(values["logo"]["data"]), len(big),
                         "大图被截断了")
        saved = self.site._save_series_image("M3", "logo", values)
        self.assertTrue(saved, "1.9MB 图应该能存下")
        self.assertEqual(Path(saved).stat().st_size, len(big))

    def test_over_limit_rejected_cleanly(self) -> None:
        """超过上限的图被拒, 但不该抛异常/破坏请求(用户 m18831: 上限提到 8MB)。"""
        from dddb.public.site import MAX_IMAGE_BODY

        huge = PNG_HEAD + b"\x00" * (MAX_IMAGE_BODY + 100 * 1024)
        headers, raw = _body({"logo": ("huge.png", "image/png", huge)},
                             {"name": "M3"})
        values = self.site._values_from_multipart(headers, raw)
        self.assertEqual(self.site._save_series_image("M3", "logo", values), "")

    def test_two_point_one_megabytes_now_passes(self) -> None:
        """2.1MB 的图以前被拒, 用户 m18831 之后必须能存下。"""
        big = PNG_HEAD + b"\x00" * (2100 * 1024)
        headers, raw = _body({"logo": ("big.png", "image/png", big)},
                             {"name": "M3"})
        values = self.site._values_from_multipart(headers, raw)
        saved = self.site._save_series_image("M3", "logo", values)
        self.assertTrue(saved, "2.1MB 图现在应该能存下")
        self.assertEqual(Path(saved).stat().st_size, len(big))

    def test_multiple_images_in_one_form(self) -> None:
        """一张表单里多个图(封面 + LOGO + 海报)也要能过。"""
        img = PNG_HEAD + b"\x00" * (900 * 1024)
        headers, raw = _body(
            {"logo": ("l.png", "image/png", img),
             "poster": ("p.png", "image/png", img),
             "cover": ("c.png", "image/png", img)},
            {"name": "M3"})
        from dddb.public.site import MAX_BODY

        self.assertLess(len(raw), MAX_BODY,
                        "两张 900KB 图应该在上限内")
        values = self.site._values_from_multipart(headers, raw)
        for key in ("logo", "poster", "cover"):
            self.assertIn(key, values, f"{key} 丢了")
            self.assertEqual(len(values[key]["data"]), len(img))


class TestErrorPageMessage(unittest.TestCase):
    """413 提示要说清上限与怎么办。"""

    def test_message_mentions_limit(self) -> None:
        src = (ROOT / "dddb/public/site.py").read_text(encoding="utf-8")
        self.assertIn("上限 20MB", src,
                      "413 提示应告诉用户上限是多少")
        self.assertIn("8MB", src, "应提示图片要压到 8MB 以内")


if __name__ == "__main__":
    unittest.main()
