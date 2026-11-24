"""测试: 封面替换后缓存必须失效(mtime 精度不足也不能返回旧图)."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests._tmp import temp_dir

ROOT = Path(__file__).resolve().parents[1]


class TestCoverByteCache(unittest.TestCase):
    """_read_cover 的缓存不能因为 mtime 精度不足而返回旧字节."""

    def test_has_drop_cover_cache(self) -> None:
        from dddb.public.site import PublicSite

        self.assertTrue(hasattr(PublicSite, "drop_cover_cache"),
                        "必须有显式丢弃缓存的方法")

    def test_upload_clears_both_caches(self) -> None:
        """上传后要清 data 列缓存 + 封面字节缓存."""
        import inspect

        from dddb.public.site import PublicSite

        # 写盘逻辑在 _store_cover_bytes 里(上传与按 URL 抓共用), 所以要
        # 连它一起看 —— 只看处理函数会漏掉真正清缓存的那段代码。
        source = (inspect.getsource(PublicSite._do_album_cover)
                  + inspect.getsource(PublicSite._store_cover_bytes))
        self.assertIn("invalidate", source, "要清 data 列缓存")
        self.assertIn("drop_cover_cache", source,
                      "要清封面字节缓存(否则换图后仍显示旧图)")

    def test_cache_signature_includes_size(self) -> None:
        """缓存签名要含文件大小, 不只是 mtime."""
        import inspect

        from dddb.public.site import PublicSite

        source = inspect.getsource(PublicSite._read_cover)
        self.assertIn("st_size", source, "签名要含文件大小")
        self.assertIn("st_mtime_ns", source, "要用纳秒精度")

    def test_drop_cache_actually_works(self) -> None:
        """真实场景: 同尺寸覆盖 + mtime 不变时, drop 后能读到新内容."""
        import time

        from dddb.public.site import PublicSite

        ctx = temp_dir("covercache-")
        root = Path(ctx.__enter__())
        try:
            site = PublicSite.__new__(PublicSite)
            site._cover_cache = {}
            import threading

            site._cover_lock = threading.Lock()
            site.cover_cache_size = 100

            path = root / "test.img"
            old = b"\x89PNG\r\n\x1a\nOLDCONTENT"
            path.write_bytes(old)
            first, _ = site._read_cover(path)
            self.assertEqual(first, old)

            # 覆盖成同长度的新内容, 并把 mtime 强行设回原值(模拟精度不足)
            st = path.stat()
            new = b"\x89PNG\r\n\x1a\nNEWCONTENT"
            path.write_bytes(new)
            import os

            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))

            # 不清缓存: 可能仍是旧值(mtime+size 相同)
            # 清缓存后: 必须读到新内容
            site.drop_cover_cache(path)
            after, _ = site._read_cover(path)
            self.assertEqual(after, new, "drop 缓存后必须读到新字节")
        finally:
            ctx.__exit__(None, None, None)

    def test_drop_is_safe_for_unknown_path(self) -> None:
        from dddb.public.site import PublicSite

        site = PublicSite.__new__(PublicSite)
        site._cover_cache = {}
        import threading

        site._cover_lock = threading.Lock()
        site.cover_cache_size = 10
        # 不存在的路径不应抛异常
        site.drop_cover_cache("/nonexistent/path.img")
        self.assertIsInstance(site._cover_cache, dict)


class TestCoverUploadRedirect(unittest.TestCase):
    def test_redirects_to_edit(self) -> None:
        import inspect

        from dddb.public.site import PublicSite

        source = inspect.getsource(PublicSite._do_album_cover)
        self.assertIn("next_url", source, "要读表单 next 回编辑页")


if __name__ == "__main__":
    unittest.main()