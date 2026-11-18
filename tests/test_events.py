"""展会编号与 X 话题标签引擎测试."""

from __future__ import annotations

import datetime as dt
import unittest

from dddb import events as E


class TestComiketNumbering(unittest.TestCase):
    def test_known_events(self) -> None:
        # C100 = 2022 夏, C101 = 2022 冬, C107 = 2025 冬, C108 = 2026 夏
        self.assertTrue(E.comiket_event(100).start_date.startswith("2022-08"))
        self.assertTrue(E.comiket_event(101).start_date.startswith("2022-12"))
        self.assertTrue(E.comiket_event(105).start_date.startswith("2024-12"))
        self.assertTrue(E.comiket_event(106).start_date.startswith("2025-08"))
        self.assertTrue(E.comiket_event(107).start_date.startswith("2025-12"))
        self.assertTrue(E.comiket_event(108).start_date.startswith("2026-08"))

    def test_season(self) -> None:
        self.assertEqual(E.comiket_event(108).season, "summer")
        self.assertEqual(E.comiket_event(109).season, "winter")

    def test_number_for(self) -> None:
        self.assertEqual(E.comiket_number_for(2026, "summer"), 108)
        self.assertEqual(E.comiket_number_for(2026, "winter"), 109)


class TestM3(unittest.TestCase):
    def test_dates_are_sundays_in_right_month(self) -> None:
        for season, month in (("spring", 4), ("autumn", 10)):
            start, _ = E.m3_event(2026, season).start_date, None
            day = dt.date.fromisoformat(start)
            self.assertEqual(day.month, month)
            self.assertEqual(day.weekday(), 6)

    def test_names(self) -> None:
        self.assertEqual(E.m3_event(2026, "spring").name, "M3-2026春")
        self.assertEqual(E.m3_event(2026, "autumn").name, "M3-2026秋")


class TestTags(unittest.TestCase):
    def test_m3_tag_variants(self) -> None:
        tags = E.m3_tags(2026, "autumn")
        for expected in ("#M3", "#M3秋", "#M3秋2026", "#M3-2026秋", "#M3_2026秋",
                         "#M32026秋", "#M3秋26", "#M3新譜", "#M3Autumn2026", "#M3Fall2026"):
            self.assertIn(expected, tags, expected)
        self.assertEqual(len(tags), len(set(tags)), "标签不应重复")

    def test_comiket_tag_variants(self) -> None:
        tags = E.comiket_tags(107, season="winter")
        for expected in ("#C107", "#コミケ", "#コミックマーケット107", "#C107新刊", "#冬コミ", "#Comiket107"):
            self.assertIn(expected, tags, expected)

    def test_all_tags_dedupe(self) -> None:
        evs = [E.m3_event(2026, "autumn"), E.comiket_event(109)]
        tags = E.all_tags(evs, extra=["#カスタム"])
        self.assertIn("#カスタム", tags)
        self.assertEqual(len(tags), len(set(tags)))


class TestParseResolve(unittest.TestCase):
    def test_parse_event(self) -> None:
        self.assertEqual(E.parse_event("M3-2026秋").name, "M3-2026秋")
        self.assertEqual(E.parse_event("【M3秋2026】新譜").name, "M3-2026秋")
        self.assertEqual(E.parse_event("C107 参加します").name, "C107")
        self.assertEqual(E.parse_event("コミックマーケット108").name, "C108")
        self.assertEqual(E.parse_event("例大祭23").name, "例大祭23")
        self.assertEqual(E.parse_event("紅楼夢15").name, "紅楼夢15")

    def test_m3_without_year_uses_default(self) -> None:
        ev = E.parse_event("#M3秋")
        self.assertTrue(ev.name.startswith("M3-"))
        self.assertTrue(ev.name.endswith("秋"))

    def test_detect_multiple(self) -> None:
        evs = E.detect_events("M3-2026秋で頒布します。C107も参加。")
        names = {e.name for e in evs}
        self.assertIn("M3-2026秋", names)
        self.assertIn("C107", names)

    def test_resolve(self) -> None:
        self.assertEqual([e.name for e in E.resolve("M3-2026秋")], ["M3-2026秋"])
        self.assertEqual({e.name for e in E.resolve("C108")}, {"C108"})
        year_events = E.resolve("2026")
        self.assertEqual(len(year_events), 4)
        self.assertEqual(len(E.resolve("M3-2026秋,C107")), 2)

    def test_recent_events_window(self) -> None:
        today = dt.date(2026, 10, 1)
        evs = E.recent_events(today, lookback_days=200, lookahead_days=120)
        names = {e.name for e in evs}
        self.assertIn("M3-2026秋", names)

    def test_tag_to_event(self) -> None:
        evs = [E.m3_event(2026, "autumn")]
        self.assertEqual(E.tag_to_event("#M3秋2026", evs), "M3-2026秋")
        self.assertEqual(E.tag_to_event("#M3", evs), "M3-2026秋")


if __name__ == "__main__":
    unittest.main()
