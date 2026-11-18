"""HTTP 客户端的 URL 归一化.

线上踩到的: 封面地址里带日文文件名(``.../CDケース.png``), ``urllib`` 直接抛
``UnicodeEncodeError`` —— 上游其实完全可用, 问题出在我们没做百分号编码。
"""

from __future__ import annotations

import unittest

from dddb.httpclient import _encode_url


class EncodeUrlTest(unittest.TestCase):
    def test_ascii_url_untouched(self) -> None:
        url = "https://ex.com/a/b.jpg?x=1&y=2"
        self.assertEqual(_encode_url(url), url)

    def test_non_ascii_path_is_quoted(self) -> None:
        got = _encode_url("http://compressgeekrecords.com/wp/CDケース.png")
        self.assertIn("%E3%82%B1", got)
        self.assertEqual(got, "http://compressgeekrecords.com/wp/CD%E3%82%B1%E3%83%BC%E3%82%B9.png")
        got.encode("ascii")          # 编码后必须是纯 ASCII, 否则 urllib 还是会炸

    def test_non_ascii_query_is_quoted(self) -> None:
        got = _encode_url("https://ex.com/img?name=ケース")
        self.assertIn("name=%E3%82%B1", got)

    def test_empty(self) -> None:
        self.assertEqual(_encode_url(""), "")


if __name__ == "__main__":
    unittest.main()
