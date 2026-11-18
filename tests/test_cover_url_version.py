"""测试: 全站封面 URL 都带版本号(换了封面所有页面都更新)."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests._tmp import temp_dir

ROOT = Path(__file__).resolve().parents[1]


class TestCoverUrlVersioning(unittest.TestCase):
    """封面 URL 必须带 ?v= 版本号, 否则 CDN 缓存会让"退出编辑页还是原封面"."""

    def setUp(self) -> None:
        self._ctx = temp_dir("coverurl-")
        self.root = Path(self._ctx.__enter__())
        from dddb.config import Config
        from dddb.db import Store
        from dddb.models import Album

        self.config = Config.default(root=self.root)
        self.config.ensure_dirs()
        covers = Path(self.config.data_dir) / "covers"
        covers.mkdir(parents=True, exist_ok=True)
        self.covers = covers

        img = covers / "k.img"
        img.write_bytes(b"\x89PNG\r\n\x1a\nX")
        self.img = img

        store = Store(self.config.db_path)
        store.upsert_album(Album.from_dict({
            "key": "k", "title": "T", "tracks": [], "source_ids": {},
            "cover_local": str(img)}))
        store.close()

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def _album_cover(self) -> str:
        from dddb.public.data import PublicData, ReadOnlyStore

        store = ReadOnlyStore(self.config.db_path)
        try:
            data = PublicData(store, covers_dir=self.covers)
            album = data.get_album("k")
            return str((album or {}).get("cover") or "")
        finally:
            if hasattr(store, "close"):
                store.close()

    def test_album_detail_cover_has_version(self) -> None:
        cover = self._album_cover()
        self.assertTrue(cover.startswith("/cover/"), cover)
        self.assertIn("?v=", cover, "详情页封面 URL 要带版本号")

    def test_version_changes_after_replace(self) -> None:
        before = self._album_cover()
        from dddb.config import Config
        from dddb.db import Store

        store = Store(self.config.db_path)
        store.update_album_fields("k", {"cover_v": "1234567890"})
        store.close()
        after = self._album_cover()
        self.assertNotEqual(before, after, "换封面后 URL 必须变化")
        self.assertIn("1234567890", after)

    def test_falls_back_to_file_stat(self) -> None:
        """没写 cover_v 时, 用文件 mtime+大小兜底."""
        cover = self._album_cover()
        self.assertIn("?v=", cover)
        # 版本号应是数字(时间戳+大小)
        version = cover.split("?v=")[-1]
        self.assertTrue(version.isdigit(), f"版本号格式异常: {version}")

    def test_no_cover_no_version(self) -> None:
        from dddb.config import Config
        from dddb.db import Store
        from dddb.models import Album
        from dddb.public.data import PublicData, ReadOnlyStore

        store = Store(self.config.db_path)
        store.upsert_album(Album.from_dict({
            "key": "n", "title": "无封面", "tracks": [], "source_ids": {}}))
        store.close()

        rs = ReadOnlyStore(self.config.db_path)
        try:
            data = PublicData(rs, covers_dir=self.covers)
            album = data.get_album("n")
            cover = str((album or {}).get("cover") or "")
            self.assertEqual(cover, "", "无封面时 cover 应为空串")
        finally:
            if hasattr(rs, "close"):
                rs.close()

    def test_site_py_uses_versioned_helper(self) -> None:
        """收藏/关注列表也要用带版本的封面 URL."""
        source = (ROOT / "dddb/public/site.py").read_text(encoding="utf-8")
        # 不该再有裸的 "/cover/" 拼接
        bad = []
        for line in source.splitlines():
            if '"/cover/"' in line and "T.cover_src" not in line:
                bad.append(line.strip())
        self.assertEqual(bad, [], f"还有裸的封面 URL 拼接: {bad[:3]}")

    def test_data_py_uses_cover_v(self) -> None:
        source = (ROOT / "dddb/public/data.py").read_text(encoding="utf-8")
        self.assertIn("cover_v", source, "data.py 要用 cover_v 做版本号")


class TestCoverUploadSetsVersion(unittest.TestCase):
    def test_upload_writes_cover_v(self) -> None:
        import inspect

        from dddb.public.site import PublicSite

        source = inspect.getsource(PublicSite._do_album_cover)
        self.assertIn("cover_v", source, "上传时要写 cover_v")


if __name__ == "__main__":
    unittest.main()