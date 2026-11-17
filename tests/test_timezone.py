"""测试: 时间戳按语言时区显示(zh=UTC+8)."""

from __future__ import annotations

import unittest
from pathlib import Path

from dddb.public.timefmt import (
    DEFAULT_OFFSET,
    LANGUAGE_TZ_LABEL,
    LANGUAGE_TZ_OFFSET,
    format_stamp,
    format_stamp_iso,
    zone_label,
    zone_offset,
    zone_of,
)

ROOT = Path(__file__).resolve().parents[1]

# 2026-11-13 15:25:27 UTC
#   zh (UTC+8) -> 2026-11-13 23:25
#   ja (UTC+9) -> 2026-11-14 00:25
#   en (UTC)   -> 2026-11-13 15:25
STAMP = 1794583527.0


class TestZoneMapping(unittest.TestCase):
    """语言 -> 时区偏移."""

    def test_offsets(self) -> None:
        self.assertEqual(LANGUAGE_TZ_OFFSET["zh"], 8)
        self.assertEqual(LANGUAGE_TZ_OFFSET["ja"], 9)
        self.assertEqual(LANGUAGE_TZ_OFFSET["en"], 0)

    def test_zone_offset_lookup(self) -> None:
        self.assertEqual(zone_offset("zh"), 8)
        self.assertEqual(zone_offset("zh-CN"), 8)
        self.assertEqual(zone_offset("ja"), 9)
        self.assertEqual(zone_offset("en"), 0)

    def test_unknown_falls_back_to_chinese(self) -> None:
        for bad in ("", None, "fr", "xx", 123):
            self.assertEqual(zone_offset(bad), DEFAULT_OFFSET)

    def test_labels(self) -> None:
        self.assertEqual(zone_label("zh"), "UTC+8")
        self.assertEqual(zone_label("ja"), "UTC+9")
        self.assertEqual(zone_label("en"), "UTC")
        self.assertEqual(LANGUAGE_TZ_LABEL["zh"], "UTC+8")

    def test_zone_object(self) -> None:
        self.assertEqual(zone_of("zh").utcoffset(None).total_seconds(), 8 * 3600)
        self.assertEqual(zone_of("en").utcoffset(None).total_seconds(), 0)


class TestFormatStamp(unittest.TestCase):
    """按语言格式化时间戳."""

    def test_chinese_is_utc8(self) -> None:
        """核心需求: 中文必须是 UTC+8。"""
        self.assertEqual(format_stamp(STAMP, "zh"), "2026-11-13 23:25")

    def test_japanese_is_utc9(self) -> None:
        self.assertEqual(format_stamp(STAMP, "ja"), "2026-11-14 00:25")

    def test_english_is_utc(self) -> None:
        self.assertEqual(format_stamp(STAMP, "en"), "2026-11-13 15:25")

    def test_default_is_chinese(self) -> None:
        self.assertEqual(format_stamp(STAMP), "2026-11-13 23:25")

    def test_with_seconds(self) -> None:
        self.assertEqual(format_stamp(STAMP, "zh", with_seconds=True),
                         "2026-11-13 23:25:27")

    def test_empty_values(self) -> None:
        for bad in (0, None, "", "abc", -1, -100.5):
            self.assertEqual(format_stamp(bad, "zh"), "")

    def test_custom_empty_text(self) -> None:
        self.assertEqual(format_stamp(0, "zh", empty="—"), "—")

    def test_iso_with_offset(self) -> None:
        self.assertEqual(format_stamp_iso(STAMP, "zh"),
                         "2026-11-13T23:25:27+08:00")
        self.assertEqual(format_stamp_iso(STAMP, "en"),
                         "2026-11-13T15:25:27+00:00")

    def test_cross_day_boundary(self) -> None:
        """UTC 晚上 = 中文第二天(边界要正确进位)."""
        # 2026-11-13 17:00 UTC -> zh 2026-11-14 01:00
        self.assertEqual(format_stamp(1794589200.0, "zh"),
                         "2026-11-14 01:00")
        self.assertEqual(format_stamp(1794589200.0, "en"),
                         "2026-11-13 17:00")


class TestWiredIntoDataLayer(unittest.TestCase):
    """数据层 / 用户层 / 站点层都走新的格式化."""

    def test_human_time_accepts_lang(self) -> None:
        from dddb.public.data import human_time

        self.assertEqual(human_time(STAMP, "zh"), "2026-11-13 23:25")
        self.assertEqual(human_time(STAMP, "en"), "2026-11-13 15:25")

    def test_time_text_accepts_lang(self) -> None:
        from dddb.public.users import _time_text

        self.assertEqual(_time_text(STAMP, "zh"), "2026-11-13 23:25")
        self.assertEqual(_time_text(0, "zh"), "—")

    def test_stamp_text_accepts_lang(self) -> None:
        from dddb.public.site import _stamp_text

        self.assertEqual(_stamp_text(STAMP, "zh"), "2026-11-13 23:25")
        self.assertEqual(_stamp_text(STAMP, "ja"), "2026-11-14 00:25")

    def test_no_localtime_left(self) -> None:
        """时间格式化不能再依赖服务器本地时区。"""
        # 检查真实的格式化调用(不是注释/docstring 里的说明文字)
        for rel in ("dddb/public/data.py", "dddb/public/users.py"):
            text = (ROOT / rel).read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # 真正的调用形如 strftime("%Y-...", time.localtime(x))
                if "strftime(" in stripped and "localtime(" in stripped:
                    self.fail(
                        f"{rel} 还在用服务器本地时区格式化: {stripped[:80]}")


class TestAlbumUpdatedText(unittest.TestCase):
    """详情页"最后更新"按页面语言. """

    def test_uses_timestamp(self) -> None:
        from dddb.public import templates as T

        album = {"updated_at": STAMP, "updated_text": "旧文本"}
        self.assertEqual(T._album_updated_text(album, "zh"),
                         "2026-11-13 23:25")
        self.assertEqual(T._album_updated_text(album, "en"),
                         "2026-11-13 15:25")

    def test_falls_back_to_text(self) -> None:
        from dddb.public import templates as T

        album = {"updated_text": "只有文本"}
        self.assertEqual(T._album_updated_text(album, "zh"), "只有文本")


class TestLanguageDetection(unittest.TestCase):
    """语言来自 Cookie / Accept-Language."""

    def test_cookie_wins(self) -> None:
        from dddb.public.site import PublicSite

        site = object.__new__(PublicSite)
        self.assertEqual(
            site.lang_from_headers({"cookie": "dddb_lang=ja"}),
            "ja")

    def test_accept_language(self) -> None:
        from dddb.public.site import PublicSite

        site = object.__new__(PublicSite)
        self.assertEqual(
            site.lang_from_headers({"accept-language": "en-US,en;q=0.9"}),
            "en")

    def test_default_chinese(self) -> None:
        from dddb.public.site import PublicSite

        site = object.__new__(PublicSite)
        self.assertEqual(site.lang_from_headers({}), "zh")

    def test_unsupported_language_falls_back(self) -> None:
        from dddb.public.site import PublicSite

        site = object.__new__(PublicSite)
        self.assertEqual(
            site.lang_from_headers({"cookie": "dddb_lang=fr"}), "zh")


if __name__ == "__main__":
    unittest.main()
