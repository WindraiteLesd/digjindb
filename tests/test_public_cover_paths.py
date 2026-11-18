"""回归: `cover_local` 里存的是**另一台机器的绝对路径**时也要能找到文件.

真实事故: 库里存 `E:\\...\\doujindigdb\\data\\covers\\xxx.img`(本机 Windows),
搬到 Linux 云服务器后 `resolve_cover_path` 直接 resolve 失败 -> 753 张有封面的
专辑全部退化成占位图。文件名是稳定的, 目录前缀不是 —— 所以要按文件名回退。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from dddb.public.data import PublicData, ReadOnlyStore
from tests._tmp import temp_dir


class TestCoverPathPortability(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_dir("coverpath-")
        self.root = self._ctx.__enter__()
        self.covers = self.root / "data" / "covers"
        (self.covers / "upload").mkdir(parents=True, exist_ok=True)
        (self.covers / "album-a.img").write_bytes(b"IMG-A")
        (self.covers / "upload" / "album-b.jpg").write_bytes(b"IMG-B")
        # 目录之外的文件(用来验证安全边界)
        (self.root / "outside.img").write_bytes(b"OUTSIDE")
        # 一个最小的只读库(只为构造 PublicData, 不查表)
        import sqlite3

        db = self.root / "db.sqlite3"
        conn = sqlite3.connect(db)
        conn.close()
        self.data = PublicData(ReadOnlyStore(db), root=self.root, covers_dir=self.covers)

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def test_relative_name_resolves(self) -> None:
        got = self.data.resolve_cover_path("album-a.img")
        self.assertEqual(got, (self.covers / "album-a.img").resolve())

    def test_windows_absolute_path_falls_back_to_filename(self) -> None:
        """**核心回归**: 本机绝对路径在 Linux 上要能按文件名找回."""
        got = self.data.resolve_cover_path(
            r"E:\Files\Works\DSHWorkSpace\doujindigdb\data\covers\album-a.img")
        self.assertIsNotNone(got, "跨机器绝对路径应回退到 covers_dir/文件名")
        self.assertEqual(got.name, "album-a.img")

    def test_windows_upload_subdir_falls_back(self) -> None:
        got = self.data.resolve_cover_path(
            r"E:\other\data\covers\upload\album-b.jpg")
        self.assertIsNotNone(got)
        self.assertEqual(got, (self.covers / "upload" / "album-b.jpg").resolve())

    def test_posix_absolute_other_machine_falls_back(self) -> None:
        got = self.data.resolve_cover_path("/home/someone/app/data/covers/album-a.img")
        self.assertIsNotNone(got)
        self.assertEqual(got.name, "album-a.img")

    def test_missing_file_returns_none(self) -> None:
        self.assertIsNone(self.data.resolve_cover_path(r"E:\x\covers\nope.img"))

    def test_empty_returns_none(self) -> None:
        self.assertIsNone(self.data.resolve_cover_path(""))
        self.assertIsNone(self.data.resolve_cover_path("   "))

    def test_outside_covers_dir_is_refused(self) -> None:
        """安全边界不变: 封面目录之外的文件即使存在也不许发."""
        got = self.data.resolve_cover_path("outside.img")
        self.assertIsNone(got, "covers 目录之外的文件必须拒绝")

    def test_traversal_is_refused(self) -> None:
        got = self.data.resolve_cover_path("../outside.img")
        self.assertIsNone(got)


if __name__ == "__main__":
    unittest.main()