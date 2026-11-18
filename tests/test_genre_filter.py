"""曲风与标签去噪: 介质/载体/版本形态不算曲风, 逗号整串要先拆.

实测库里两种污染:
① `genres` 里 14 种全是 VGMdb 的分类与介质(`Digital` / `DL Card` / `Original Work` / `EP` …)
② 标签是**逗号整串**存的(`"CD, Digital"` / `"Compilation, CD, Digital"`)——
   归一化后匹配不到噪声集, 于是整串留在页面上
"""

from __future__ import annotations

import unittest

from dddb.config import Config
from dddb.db import (Store, is_noise_genre, is_noise_tag, split_tag_values)
from dddb.models import Album
from tests._tmp import temp_dir


class TestSplitTagValues(unittest.TestCase):
    def test_comma_joined_split(self) -> None:
        """**核心**: "CD, Digital" 是一个整串, 必须先拆成两个词."""
        self.assertEqual(split_tag_values(["CD, Digital"]), ["CD", "Digital"])

    def test_mixed_separators(self) -> None:
        self.assertEqual(split_tag_values(["A, B、C；D;E/F"]),
                         ["A", "B", "C", "D", "E", "F"])

    def test_dedupe_and_empty(self) -> None:
        self.assertEqual(split_tag_values(["A, A", "", "  ", None]), ["A"])


class TestGenreNoise(unittest.TestCase):
    def test_media_and_format_are_not_genres(self) -> None:
        for value in ("Digital", "DL Card", "Original Work", "Original Soundtrack",
                      "EP", "Album", "Compilation", "CD-R", "8cm CD", "MD",
                      "Single", "Live Event"):
            self.assertTrue(is_noise_genre(value), f"{value!r} 不该算曲风")

    def test_real_genres_kept(self) -> None:
        for value in ("Vocal", "Remix", "Electronic", "Rock", "J-Core",
                      "ポップス", "ボイスドラマ", "Touhou"):
            self.assertFalse(is_noise_genre(value), f"{value!r} 是曲风, 不该被过滤")

    def test_media_tag_is_also_noise(self) -> None:
        """介质词当**标签**也是噪声(标签只放流派/展会这类索引信息)."""
        for value in ("Digital", "EP", "Compilation", "CD-R"):
            self.assertTrue(is_noise_tag(value))

    def test_origin_terms_kept_as_tags(self) -> None:
        """出处类保留为标签(有索引价值), 只是不算曲风."""
        for value in ("Original Work", "Original Soundtrack"):
            self.assertFalse(is_noise_tag(value))
            self.assertTrue(is_noise_genre(value))


class TestWriteChokePoint(unittest.TestCase):
    """写库那一个闸门: 拆词 + 去噪 —— 所有数据源都过这里."""

    def setUp(self) -> None:
        self._ctx = temp_dir("genre-")
        self.root = self._ctx.__enter__()
        self.cfg = Config.default(root=self.root)
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._ctx.__exit__(None, None, None)

    def _write(self, tags, genres) -> Album:
        album = Album(title="T", title_original="T", circle="C",
                      event="M3-2026春", tags=list(tags), genres=list(genres),
                      source_ids={"t": "1"})
        album.key = album.compute_key()
        self.store.upsert_album(album, event=album.event)
        return self.store.get_album(album.key)

    def test_comma_joined_tag_is_split_then_filtered(self) -> None:
        saved = self._write(["CD, Digital", "東方Project"], ["Electronic"])
        self.assertNotIn("CD, Digital", saved.tags, "整串不该留着")
        self.assertIn("東方Project", saved.tags)
        self.assertNotIn("CD", saved.tags)
        self.assertNotIn("Digital", saved.tags)

    def test_genres_clean(self) -> None:
        saved = self._write([], ["Digital", "DL Card", "Vocal", "Rock"])
        self.assertEqual(saved.genres, ["Vocal", "Rock"])

    def test_real_tags_survive(self) -> None:
        saved = self._write(["女性ボーカル", "インスト", "Rock"], [])
        self.assertEqual(saved.tags, ["女性ボーカル", "インスト", "Rock"])

    def test_event_tags_are_dropped(self) -> None:
        """**新规则**: 展会名/会次/年份已经有结构化字段, 不再当标签存.

        (实测这类标签占全部标签出现次数的一半, 属于"无检索意义的重复")
        """
        saved = self._write(["C108", "M3-2026秋", "2026", "女性ボーカル"], [])
        self.assertEqual(saved.tags, ["女性ボーカル"])

    def test_album_tags_table_has_no_noise(self) -> None:
        self._write(["CD, Digital"], ["EP", "Rock"])
        rows = self.store.album_tags()
        names = [t for t, _ in rows]
        for bad in ("CD", "Digital", "EP", "CD, Digital"):
            self.assertNotIn(bad, names)
        self.assertIn("Rock", names)


if __name__ == "__main__":
    unittest.main()