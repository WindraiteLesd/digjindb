"""来源可信度驱动的合并 / 去重测试.

这是「一手来源说了算」的核心: 次级来源(第三方转载站)不能覆盖一手来源
(社团自己登记)的标题、社团、发售日、封面与曲目表。
"""

from __future__ import annotations

import unittest

from dddb.db import Store
from dddb.models import Album, Track
from tests._tmp import temp_dir

TRUST_PRIMARY = 100      # dojinmusic: 社团自己登记
TRUST_SITE = 80          # bgm.tv / VGMdb
TRUST_SECONDARY = 30     # doujinstyle: 第三方转载站


def album(title: str, **kw) -> Album:
    kw.setdefault("title_original", title)
    kw.setdefault("source_ids", {"test": title})
    return Album(title=title, **kw)


class TestTrustTier(unittest.TestCase):
    def test_tiers(self) -> None:
        cases = [(100, "一手来源"), (90, "一手来源"), (80, "权威站点"),
                 (50, "权威站点"), (30, "次要来源"), (1, "次要来源"), (0, "未知来源")]
        for trust, want in cases:
            with self.subTest(trust=trust):
                self.assertEqual(album("x", trust=trust).trust_tier, want)


class TestPrimarySource(unittest.TestCase):
    def test_picks_highest_trust(self) -> None:
        a = Album(title="t", title_original="t", trust=100,
                  source_ids={"dojinmusic": "1"},
                  trust_by_source={"dojinmusic": 100, "doujinstyle": 30})
        self.assertEqual(a.primary_source, "dojinmusic")

    def test_seeded_from_trust(self) -> None:
        a = album("t", trust=TRUST_SECONDARY, source_ids={"doujinstyle": "9"})
        self.assertEqual(a.trust_by_source, {"doujinstyle": TRUST_SECONDARY})
        self.assertEqual(a.primary_source, "doujinstyle")

    def test_empty(self) -> None:
        self.assertEqual(Album(title="t").primary_source, "")


class TestTrustAwareMerge(unittest.TestCase):
    """低可信度记录先入库、高可信度记录后到的场景 —— 必须由高可信度说了算."""

    def setUp(self) -> None:
        self.low = Album(
            title="Wrong Title", title_original="Wrong Title",
            circle="wrong circle", release_date="2011-05-08",
            cover_url="https://doujinstyle.com/thumbs/1.jpg",
            description="第三方转载", source_ids={"doujinstyle": "1"}, trust=TRUST_SECONDARY,
            tracks=[Track(title="旧曲目", index=1)],
            urls={"doujinstyle": "https://doujinstyle.com/?p=page&id=1"},
        )
        self.high = Album(
            title="正しいタイトル", title_original="正しいタイトル",
            circle="正しいサークル", release_date="2026-10-25",
            cover_url="https://www.dojin-music.info/jacket.jpg",
            description="一手登记", source_ids={"dojinmusic": "36739"}, trust=TRUST_PRIMARY,
            tracks=[Track(title="水面の記憶", index=1), Track(title="傘", index=2)],
            urls={"dojinmusic": "https://www.dojin-music.info/cd/36739"},
            artists=["SΔYO"],
        )

    def test_higher_trust_overrides_existing_values(self) -> None:
        merged = self.low.merge(self.high)
        self.assertEqual(merged.title, "正しいタイトル", "一手来源必须覆盖次级来源的标题")
        self.assertEqual(merged.circle, "正しいサークル")
        self.assertEqual(merged.release_date, "2026-10-25", "次级来源写的错日期必须被纠正")
        self.assertIn("dojin-music", merged.cover_url, "高可信度的封面胜出")
        self.assertEqual(merged.description, "一手登记")
        self.assertEqual([t.title for t in merged.tracks], ["水面の記憶", "傘"],
                         "高可信度的曲目表胜出, 哪怕条数一样")

    def test_lower_trust_does_not_override(self) -> None:
        merged = self.high.merge(self.low)
        self.assertEqual(merged.title, "正しいタイトル")
        self.assertEqual(merged.circle, "正しいサークル")
        self.assertEqual(merged.release_date, "2026-10-25")
        self.assertIn("dojin-music", merged.cover_url)
        self.assertEqual([t.title for t in merged.tracks], ["水面の記憶", "傘"])

    def test_lower_trust_fills_gaps_only(self) -> None:
        sparse = Album(title="正しいタイトル", title_original="正しいタイトル",
                       source_ids={"dojinmusic": "1"}, trust=TRUST_PRIMARY)
        merged = sparse.merge(self.low)
        self.assertEqual(merged.circle, "wrong circle", "空字段应该被补上")
        self.assertEqual(merged.release_date, "2011-05-08", "空字段应该被补上")
        self.assertEqual(merged.title, "正しいタイトル", "已有字段不被覆盖")

    def test_union_fields(self) -> None:
        merged = self.low.merge(self.high)
        # 注意拼写: 源的正式名字是 doujinstyle(带 u)
        self.assertEqual(set(merged.source_ids), {"doujinstyle", "dojinmusic"})
        self.assertIn("doujinstyle", merged.urls)
        self.assertIn("dojinmusic", merged.urls)
        self.assertIn("SΔYO", merged.artists)

    def test_trust_and_trust_by_source(self) -> None:
        merged = self.low.merge(self.high)
        self.assertEqual(merged.trust, TRUST_PRIMARY)
        self.assertEqual(merged.trust_by_source["dojinmusic"], TRUST_PRIMARY)
        self.assertEqual(merged.trust_by_source["doujinstyle"], TRUST_SECONDARY)
        self.assertEqual(merged.primary_source, "dojinmusic")

    def test_equal_trust_keeps_existing(self) -> None:
        """可信度相同时保持"先到者"的取值, 不被后来者覆盖."""
        a = album("A", circle="c", release_date="2026-01-01",
                  source_ids={"bangumi": "1"}, trust=TRUST_SITE)
        b = album("B", circle="c", release_date="2026-02-02",
                  source_ids={"vgmdb": "2"}, trust=TRUST_SITE)
        merged = a.merge(b)
        self.assertEqual(merged.release_date, "2026-01-01")

    def test_prefer_other_forces_override(self) -> None:
        a = album("A", circle="c", source_ids={"x": "1"}, trust=TRUST_PRIMARY)
        b = album("B", circle="c", source_ids={"y": "2"}, trust=1)
        self.assertEqual(a.merge(b, prefer_other=True).title, "B")

    def test_booth_is_filled(self) -> None:
        a = album("t", circle="c", source_ids={"dojinmusic": "1"}, trust=TRUST_PRIMARY)
        b = album("t", circle="c", booth="F-07a", source_ids={"dojinmusic": "2"}, trust=TRUST_PRIMARY)
        self.assertEqual(a.merge(b).booth, "F-07a")


class TestDedupeModes(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_dir("dedupe-")
        self.root = self._ctx.__enter__()
        self.store = Store(self.root / "t.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self._ctx.__exit__(None, None, None)

    def _seed(self, title: str, circle: str = "") -> Album:
        a = Album(title=title, title_original=title, circle=circle,
                  source_ids={"seed": title + circle})
        self.store.upsert_album(a)
        return a

    def test_same_title_same_circle_merges(self) -> None:
        self._seed("同名碟", "社团A")
        found = self.store.find_similar_album(album("同名碟", circle="社团A"))
        self.assertIsNotNone(found)

    def test_same_title_different_circle_never_merges(self) -> None:
        self._seed("同名碟", "社团A")
        for mode in ("balanced", "strict"):
            with self.subTest(mode=mode):
                self.assertIsNone(self.store.find_similar_album(album("同名碟", circle="社团B"), mode=mode))

    def test_no_circle_incoming_does_not_steal_circled_album(self) -> None:
        """回归: 以前"没有社团就认领第一条同名记录", 会把常见同名碟错并到一起."""
        self._seed("ReMind", "某个社团")
        self.assertIsNone(self.store.find_similar_album(album("ReMind"), mode="balanced"))

    def test_no_circle_both_sides_merges_in_balanced(self) -> None:
        self._seed("ReMind")
        self.assertIsNotNone(self.store.find_similar_album(album("ReMind"), mode="balanced"))

    def test_strict_requires_both_circles(self) -> None:
        self._seed("ReMind")
        self.assertIsNone(self.store.find_similar_album(album("ReMind"), mode="strict"))

    def test_off_never_merges(self) -> None:
        self._seed("同名碟", "社团A")
        self.assertIsNone(self.store.find_similar_album(album("同名碟", circle="社团A"), mode="off"))

    def test_store_mode_is_used_by_default(self) -> None:
        store = Store(self.root / "s.sqlite3", dedupe_mode="strict")
        try:
            store.upsert_album(Album(title="ReMind", title_original="ReMind",
                                     source_ids={"seed": "1"}))
            self.assertIsNone(store.find_similar_album(album("ReMind")))
        finally:
            store.close()

    def test_upsert_uses_trust_when_merging(self) -> None:
        """走完整 upsert: 次级来源先入库, 一手来源后到 -> 一手来源的字段生效."""
        self.store.upsert_album(Album(
            title="Wrong", title_original="Wrong", circle="c", release_date="2011-05-08",
            source_ids={"doujinstyle": "1"}, trust=TRUST_SECONDARY,
        ))
        key_before = self.store.list_albums()[0].key

        # 同一个规范化标题 + 同一个社团, 但年份不同 -> key 不同 -> 会走 find_similar_album
        self.store.upsert_album(Album(
            title="Wrong", title_original="Wrong", circle="c", release_date="2026-10-25",
            source_ids={"dojinmusic": "36739"}, trust=TRUST_PRIMARY,
        ))
        albums = self.store.list_albums()
        self.assertEqual(len(albums), 1, "应该合并成一条而不是两条")
        merged = albums[0]
        self.assertEqual(merged.key, key_before)
        self.assertEqual(merged.release_date, "2026-10-25", "一手来源的日期必须生效")
        self.assertEqual(merged.trust, TRUST_PRIMARY)
        self.assertIn("dojinmusic", merged.source_ids)
        self.assertIn("doujinstyle", merged.source_ids)


if __name__ == "__main__":
    unittest.main()
