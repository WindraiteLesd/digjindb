"""文本规范化与相似度测试."""

from __future__ import annotations

import unittest

from dddb import textutil as T


class TestNormalize(unittest.TestCase):
    def test_halfwidth_and_punct(self) -> None:
        self.assertEqual(T.to_halfwidth("ＡＢＣ１２３"), "ABC123")
        self.assertEqual(T.to_halfwidth("（東方）"), "(東方)")

    def test_strip_event_markers(self) -> None:
        self.assertEqual(T.clean_whitespace(T.strip_event_markers("Album Title M3-2026秋")), "Album Title")
        self.assertEqual(T.clean_whitespace(T.strip_event_markers("Album C107")), "Album")
        self.assertIn("Album", T.strip_event_markers("Album 例大祭23"))

    def test_norm_title_removes_brackets(self) -> None:
        self.assertEqual(T.norm_title("幻想郷の記憶【M3-2026秋】"), "幻想郷の記憶")
        self.assertEqual(T.norm_title("Foo Bar (2024)"), "Foo Bar")
        self.assertEqual(T.norm_title("Test Album [Comiket 107]"), "Test Album")

    def test_norm_key_is_stable(self) -> None:
        a = T.norm_key("東方Project　アレンジ")
        b = T.norm_key("東方Project アレンジ")
        self.assertEqual(a, b)

    def test_slugify(self) -> None:
        # 事件标记与 "Records" 这类通用词会被剔除, 让 slug 稳定于「社团 + 标题」
        self.assertEqual(T.slugify("M3-2026秋 Shibayan Records"), "shibayan")
        self.assertEqual(T.slugify("Foo Bar"), "foo-bar")
        self.assertNotIn(" ", T.slugify("a b c"))
        self.assertEqual(T.slugify(""), "untitled")
        self.assertLessEqual(len(T.slugify("x" * 200)), 80)


class TestKana(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertEqual(T.kana_to_romaji("とうほう"), "touhou")
        self.assertEqual(T.kana_to_romaji("カタカナ"), "katakana")

    def test_digraph_and_small_tsu(self) -> None:
        self.assertEqual(T.kana_to_romaji("きゃ"), "kya")
        self.assertEqual(T.kana_to_romaji("っか"), "kka")

    def test_long_vowel(self) -> None:
        self.assertEqual(T.kana_to_romaji("メーカー"), "meekaa")

    def test_kanji_untouched(self) -> None:
        self.assertEqual(T.kana_to_romaji("東方"), "東方")


class TestSimilarity(unittest.TestCase):
    def test_identical(self) -> None:
        self.assertAlmostEqual(T.similarity("幻想郷", "幻想郷"), 1.0)

    def test_case_and_width(self) -> None:
        self.assertGreater(T.similarity("Foo Bar", "ｆｏｏ　ｂａｒ"), 0.95)

    def test_different(self) -> None:
        self.assertLess(T.similarity("Alpha", "Zeta"), 0.5)

    def test_best_similarity(self) -> None:
        self.assertAlmostEqual(T.best_similarity("abc", ["xyz", "abc"]), 1.0)

    def test_track_overlap(self) -> None:
        a = ["Intro", "Song A", "Song B"]
        b = ["intro", "song a", "song b", "bonus"]
        self.assertAlmostEqual(T.track_overlap(a, b), 1.0)
        self.assertEqual(T.track_overlap(a, ["完全不相关"]), 0.0)
        self.assertEqual(T.track_overlap([], b), 0.0)


class TestVariantsAndDuration(unittest.TestCase):
    def test_title_variants_no_garbage(self) -> None:
        variants = T.title_variants("幻想郷の記憶")
        self.assertIn("幻想郷の記憶", variants)
        # 不能产出只剩冠词的垃圾检索词
        self.assertNotIn("no", [v.lower() for v in variants])

    def test_title_variants_kana(self) -> None:
        variants = T.title_variants("とうほうふぁんたずむ")
        self.assertTrue(any(v.isascii() for v in variants), variants)

    def test_parse_duration(self) -> None:
        self.assertEqual(T.parse_duration("3:45"), 225)
        self.assertEqual(T.parse_duration("1:02:03"), 3723)
        self.assertEqual(T.parse_duration(225), 225)
        self.assertEqual(T.parse_duration("225"), 225)
        self.assertIsNone(T.parse_duration("abc"))
        self.assertIsNone(T.parse_duration(None))

    def test_format_duration(self) -> None:
        self.assertEqual(T.format_duration(225), "3:45")
        self.assertEqual(T.format_duration(None), "")

    def test_dedupe(self) -> None:
        self.assertEqual(T.dedupe_preserve_order(["a", "A", " b ", "b"]), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
