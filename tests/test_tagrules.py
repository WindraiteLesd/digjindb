"""`dddb/tagrules.py` 的判据测试 —— 这是后端清理与用户端展示共用的**唯一判据**.

真实漏网过的例子: `CD + Download Code`(两个介质词用 `+` 连起来)整词匹配不到,
所以规则必须支持"组合标签按段判断"。
"""

from __future__ import annotations

import unittest

from dddb.tagrules import is_useless_tag


class TestSingleWords(unittest.TestCase):
    def test_event_names_are_useless(self) -> None:
        """展会名/会次: 结构化字段 `event` 已存, 标签是重复."""
        for tag in ("M3-2026秋", "M3-2026春", "M3-57", "M3-58", "C108", "C102",
                    "例大祭", "秋季例大祭", "紅楼夢", "M3-2026秋(resale)"):
            self.assertTrue(is_useless_tag(tag), tag)

    def test_pure_year_is_useless(self) -> None:
        for tag in ("2026", "2020", "1999"):
            self.assertTrue(is_useless_tag(tag), tag)

    def test_platform_words_are_useless(self) -> None:
        for tag in ("Download Code", "DL", "DTM", "Mini CD", "CD-R", "CD",
                    "Digital", "HiRes", "EP", "Album"):
            self.assertTrue(is_useless_tag(tag), tag)

    def test_voice_synth_names_are_useless(self) -> None:
        for tag in ("VOCALOID", "Vocaloid", "Synthesizer V", "CeVIO", "初音ミク",
                    "可不", "AIボーカル", "合成音声", "重音テト", "ずんだもん"):
            self.assertTrue(is_useless_tag(tag), tag)

    def test_misc_is_useless(self) -> None:
        self.assertTrue(is_useless_tag("誤登録"))
        self.assertTrue(is_useless_tag(""))

    def test_real_genres_are_kept(self) -> None:
        for tag in ("Rock", "Vocal", "ポップス", "Electronic", "J-Core", "ロック",
                    "ジャズ", "DnB", "Hardcore", "女性ボーカル", "インスト",
                    "ピアノ", "ギター", "Touhou", "東方Project", "R18", "アレンジ"):
            self.assertFalse(is_useless_tag(tag), tag)


class TestCompositeTags(unittest.TestCase):
    """**回归**: 组合标签必须按段判断."""

    def test_all_parts_useless_is_useless(self) -> None:
        for tag in ("CD + Download Code", "CD / Digital", "CD, Digital",
                    "Download Code & DL", "M3-2026秋 / 2026"):
            self.assertTrue(is_useless_tag(tag), tag)

    def test_any_meaningful_part_keeps_whole(self) -> None:
        for tag in ("Rock + Metal", "Vocaloid & Rock", "CD + Rock",
                    "ポップス / ロック"):
            self.assertFalse(is_useless_tag(tag), tag)

    def test_separator_variants(self) -> None:
        self.assertTrue(is_useless_tag("CD＋Digital"))     # 全角加号
        self.assertTrue(is_useless_tag("CD・Digital"))     # 中点
        self.assertTrue(is_useless_tag("CD and Digital"))  # 英文 and


if __name__ == "__main__":
    unittest.main()