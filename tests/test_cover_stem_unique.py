"""封面上传: 中文专辑名不能共用同一个文件.

真实 bug:
    ``stem = re.sub(r"[^A-Za-z0-9._-]+", "-", key)``

    库里绝大多数专辑名是中日文, 这种 slug 会把它们全变成 ``"-"`` 或
    ``"--"``, 于是**所有中文专辑共用一个封面文件** —— 后上传的覆盖先上传的。
    表现: "上传了封面, 显示的是别的碟的图" / "上传后没变化"。
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestCoverStemUnique(unittest.TestCase):
    def setUp(self) -> None:
        import sys

        sys.path.insert(0, str(ROOT))
        from dddb.public.site import _cover_stem

        self.stem = _cover_stem

    def test_chinese_keys_differ(self) -> None:
        """两个纯中文 key 必须得到不同的文件名。"""
        a = self.stem("東方|紅")
        b = self.stem("東方|妖")
        self.assertNotEqual(a, b, "纯中文专辑共用文件名 —— 后传会覆盖前传")

    def test_japanese_keys_differ(self) -> None:
        a = self.stem("あいさつ部|s-g-vol-10")
        b = self.stem("あいさつ部|s-g-vol-11")
        self.assertNotEqual(a, b)

    def test_ascii_only(self) -> None:
        """生成的名字必须是纯 ASCII(避免编码/路径问题)。"""
        for key in ("東方|紅", "あいさつ部|test", "Mino|test|2026"):
            stem = self.stem(key)
            self.assertTrue(stem.isascii(), f"{key} -> {stem} 含非 ASCII")

    def test_stable(self) -> None:
        """同一个 key 每次得到同样结果(否则每次上传都换文件名, 旧文件堆积)。"""
        key = "東方|紅"
        self.assertEqual(self.stem(key), self.stem(key))

    def test_no_path_separators(self) -> None:
        for key in ("a/b|c", "../../etc/passwd", "a\\b"):
            stem = self.stem(key)
            self.assertNotIn("/", stem)
            self.assertNotIn("\\", stem)

    def test_empty_key(self) -> None:
        self.assertEqual(self.stem(""), "cover")

    def test_bulk_uniqueness(self) -> None:
        """一批真实形态的 key 不能撞车。"""
        keys = [
            "東方|紅", "東方|妖", "東方|永", "東方|風",
            "初音ミク|アルバム", "初音ミク|シングル",
            "あいさつ部|s-g-vol-10", "あいさつ部|s-g-vol-11",
            "Mino|test|2026", "test|album|2025",
            "kani|クラゲ", "kani|花",
        ]
        stems = {self.stem(k) for k in keys}
        self.assertEqual(len(stems), len(keys),
                         "有 key 撞到了同一个文件名")


class TestUploadUsesUniqueStem(unittest.TestCase):
    """上传路径必须用 _cover_stem, 不能再用旧 slug。"""

    def test_no_legacy_slug_in_upload(self) -> None:
        src = (ROOT / "dddb/public/site.py").read_text(encoding="utf-8")
        # 专辑封面相关的位置不该再有旧 slug
        self.assertNotIn(
            're.sub(r"[^A-Za-z0-9._-]+", "-", str(key))', src,
            "封面上传还在用会把中文变 - 的 slug")

    def test_uses_helper(self) -> None:
        src = (ROOT / "dddb/public/site.py").read_text(encoding="utf-8")
        self.assertIn("_cover_stem(key)", src)
        self.assertIn("def _cover_stem(", src)


class TestUploadEndToEnd(unittest.TestCase):
    """真上传:covers 目录里出现的文件必须与该专辑一一对应。"""

    def test_two_chinese_albums_do_not_collide(self) -> None:
        from tests._tmp import temp_path

        tmp = temp_path("cover-collide")
        data = tmp / "data"
        data.mkdir(parents=True)
        covers = data / "covers"
        covers.mkdir()
        db = data / "dddb.sqlite3"

        from dddb.db import Store
        from dddb.models import Album
        from dddb.public.site import _cover_stem

        store = Store(db)
        keys = ["東方|紅", "東方|妖"]
        for k in keys:
            store.upsert_album(Album.from_dict({
                "key": k, "title": k, "title_original": k,
                "event": "M3-2026秋", "source_ids": {}}))
        store.close()

        # 模拟上传: 用修好后的命名规则写两个文件
        try:
            paths = []
            for k, blob in zip(keys, (b"AAAA", b"BBBB")):
                stem = _cover_stem(k)
                p = covers / f"{stem}.img"
                p.write_bytes(blob)
                paths.append(p)
            self.assertNotEqual(paths[0], paths[1], "两个中文专辑写到同一个文件")
            self.assertEqual(paths[0].read_bytes(), b"AAAA")
            self.assertEqual(paths[1].read_bytes(), b"BBBB")
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
