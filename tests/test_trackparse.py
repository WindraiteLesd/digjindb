"""整段曲目表解析器(:mod:`dddb.trackparse`)测试.

覆盖三大块:

1. **格式容忍** —— 轨号前缀变体 / 标题-艺人分隔 / 各种时长写法 / 全角 / 制表符表格 /
   注释与表头跳过;
2. **保守性** —— 拿不准就整行当标题, 不把标题里的 ``-``、无空格 ``/``、紧贴的
   数字当成结构字段;
3. **边界** —— 空输入、纯空白、只有时长行、超长列表性能、``to_dict`` 契约。
"""

from __future__ import annotations

import time
import unittest

from dddb import trackparse as tp

TITLE = "幻想郷の記憶"
TITLE2 = "月まで届け、不死の煙"
ARTIST = "ShibayanRecords"
ARTIST2 = "坂上なち"


def titles(text: str) -> list[str]:
    return [t.title for t in tp.parse_tracklist(text)]


def only(text: str) -> tp.ParsedTrack:
    """解析单行, 断言恰好一首并返回它."""
    tracks = tp.parse_tracklist(text)
    assert len(tracks) == 1, f"期望 1 首, 实际 {len(tracks)}: {tracks!r}"
    return tracks[0]


# --------------------------------------------------------------------------
# 轨号前缀
# --------------------------------------------------------------------------


class TestTrackNumberPrefix(unittest.TestCase):
    def test_dot_space(self) -> None:
        track = only(f"01. {TITLE}")
        self.assertEqual((track.title, track.number, track.artists), (TITLE, 1, []))

    def test_dot_no_space(self) -> None:
        self.assertEqual(only(f"01.{TITLE}").title, TITLE)

    def test_unpadded_dot(self) -> None:
        self.assertEqual(only(f"1. {TITLE}").number, 1)

    def test_space_only(self) -> None:
        self.assertEqual(only(f"1 {TITLE}").title, TITLE)

    def test_padded_space_only(self) -> None:
        track = only(f"01 {TITLE}")
        self.assertEqual((track.number, track.title), (1, TITLE))

    def test_comma_and_ideographic_comma(self) -> None:
        for line in (f"1, {TITLE}", f"1、{TITLE}", f"1) {TITLE}", f"1.{TITLE}"):
            with self.subTest(line=line):
                self.assertEqual(only(line).title, TITLE)

    def test_dash_after_number_is_layout(self) -> None:
        track = only(f"01 - {TITLE}")
        self.assertEqual((track.number, track.title, track.artists), (1, TITLE, []))

    def test_prefix_tr(self) -> None:
        for line in ("Tr.1 ", "Tr. 1 ", "TR1 ", "tr.01 ", "trk 1 "):
            with self.subTest(line=line):
                self.assertEqual(only(line + TITLE).title, TITLE)

    def test_prefix_track(self) -> None:
        for line in ("Track 1 ", "track 1 ", "TRACK01 ", "Track.1 "):
            with self.subTest(line=line):
                track = only(line + TITLE)
                self.assertEqual((track.number, track.title), (1, TITLE))

    def test_prefix_hash(self) -> None:
        for line in ("#01 ", "#1 ", "# 01 "):
            with self.subTest(line=line):
                self.assertEqual(only(line + TITLE).title, TITLE)

    def test_prefix_no(self) -> None:
        for line in ("No.1 ", "No. 1 ", "no.01 ", "No 1 ", "number 1 "):
            with self.subTest(line=line):
                self.assertEqual(only(line + TITLE).title, TITLE)

    def test_prefix_no_does_not_eat_latin_title(self) -> None:
        # ``No More`` 里那个 "No" 后面不是数字 -> 不该当轨号
        track = only("No More Words")
        self.assertEqual((track.title, track.number), ("No More Words", None))

    def test_glued_padded_number(self) -> None:
        # ``01タイトル``: 紧贴、带前导零 -> 直接认
        track = only(f"01{TITLE}")
        self.assertEqual((track.number, track.title), (1, TITLE))

    def test_glued_number_needs_position_fit(self) -> None:
        # 紧贴且无前导零: 只有"轨号 == 行号"才认(``1タイトル 2タイトル``)
        tracks = tp.parse_tracklist(f"1{TITLE}\n2{TITLE2}")
        self.assertEqual([t.number for t in tracks], [1, 2])
        self.assertEqual([t.title for t in tracks], [TITLE, TITLE2])

    def test_number_not_baked_into_title(self) -> None:
        for line in (f"01. {TITLE}", f"07 {TITLE}", f"#12 {TITLE}"):
            with self.subTest(line=line):
                self.assertNotRegex(only(line).title, r"^\d")

    def test_sparse_numbering_keeps_positional_index(self) -> None:
        tracks = tp.parse_tracklist(f"05. {TITLE}\n06. {TITLE2}")
        # index 始终是"第几首"(与 models.Track.index 一致), 原文轨号放 number
        self.assertEqual([t.index for t in tracks], [1, 2])
        self.assertEqual([t.number for t in tracks], [5, 6])

    def test_number_only_line_is_skipped(self) -> None:
        for line in ("01.", "01", "Tr.1", "#01"):
            with self.subTest(line=line):
                self.assertEqual(tp.parse_tracklist(line), [])

    def test_year_is_not_track_number(self) -> None:
        track = only(f"2020 {TITLE}")
        self.assertEqual((track.title, track.number), (f"2020 {TITLE}", None))


# --------------------------------------------------------------------------
# 标题 / 艺人
# --------------------------------------------------------------------------


class TestTitleArtistSplit(unittest.TestCase):
    def test_slash_with_spaces(self) -> None:
        track = only(f"{TITLE} / {ARTIST}")
        self.assertEqual((track.title, track.artists), (TITLE, [ARTIST]))

    def test_dash_with_spaces(self) -> None:
        track = only(f"{TITLE} - {ARTIST}")
        self.assertEqual((track.title, track.artists), (TITLE, [ARTIST]))

    def test_en_and_em_dash(self) -> None:
        for sep in ("–", "—"):
            with self.subTest(sep=sep):
                self.assertEqual(only(f"{TITLE} {sep} {ARTIST}").artists, [ARTIST])

    def test_multiple_artists(self) -> None:
        track = only(f"{TITLE} / {ARTIST} / {ARTIST2}")
        self.assertEqual(track.artists, [ARTIST, ARTIST2])

    def test_dash_then_slash_artists(self) -> None:
        track = only(f"{TITLE} - {ARTIST} / {ARTIST2}")
        self.assertEqual((track.title, track.artists), (TITLE, [ARTIST, ARTIST2]))

    def test_leftmost_separator_wins(self) -> None:
        track = only(f"{TITLE} - {ARTIST} / {ARTIST2}")
        self.assertEqual(track.title, TITLE)

    def test_no_title_or_no_artist_means_no_split(self) -> None:
        for line in (f"{TITLE} - ", f" - {ARTIST}", f"{TITLE} / "):
            with self.subTest(line=line):
                self.assertEqual(only(line).artists, [])

    def test_artistless_line_keeps_title_only(self) -> None:
        track = only(TITLE)
        self.assertEqual((track.title, track.artists), (TITLE, []))

    def test_full_combo(self) -> None:
        track = only(f"01. {TITLE} - {ARTIST} 4:12")
        self.assertEqual(track.title, TITLE)
        self.assertEqual(track.artists, [ARTIST])
        self.assertEqual(track.duration, 252)
        self.assertEqual(track.number, 1)

    def test_continuation_artist_line_becomes_its_own_row(self) -> None:
        # 多行艺人(续行不缩进): 不做"并到上一首"的猜测, 整行当标题 -> 用户自己删
        tracks = tp.parse_tracklist(f"01. {TITLE}\n{ARTIST}\n02. {TITLE2}")
        self.assertEqual(len(tracks), 3)
        self.assertEqual(tracks[1].title, ARTIST)
        self.assertEqual(tracks[1].artists, [])


# --------------------------------------------------------------------------
# 时长
# --------------------------------------------------------------------------


class TestDuration(unittest.TestCase):
    def test_parenthesised(self) -> None:
        self.assertEqual(only(f"{TITLE} (4:12)").duration, 252)

    def test_bracketed(self) -> None:
        self.assertEqual(only(f"{TITLE} [4:12]").duration, 252)

    def test_cjk_brackets(self) -> None:
        self.assertEqual(only(f"{TITLE} 【4:12】").duration, 252)

    def test_trailing_bare_colon(self) -> None:
        track = only(f"{TITLE} 4:12")
        self.assertEqual((track.title, track.duration), (TITLE, 252))

    def test_trailing_after_artist(self) -> None:
        track = only(f"{TITLE} - {ARTIST} 4:12")
        self.assertEqual((track.title, track.artists, track.duration), (TITLE, [ARTIST], 252))

    def test_hours(self) -> None:
        track = only(f"{TITLE} 1:02:03")
        self.assertEqual((track.duration, track.duration_text), (3723, "1:02:03"))

    def test_minutes_seconds_japanese(self) -> None:
        track = only(f"{TITLE} 4分12秒")
        self.assertEqual(track.duration, 252)

    def test_duration_only_line_is_skipped(self) -> None:
        for line in ("4:12", "1:02:03", "[4:12]", "(4:12)", "252", "00:00"):
            with self.subTest(line=line):
                self.assertEqual(tp.parse_tracklist(line), [])

    def test_zero_duration_is_dropped(self) -> None:
        # ``0:00`` 是占位不是时长
        self.assertIsNone(only(f"{TITLE} (0:00)").duration)

    def test_bare_seconds_need_a_separator(self) -> None:
        track = only(f"{TITLE} - {ARTIST} 252")
        self.assertEqual((track.title, track.artists, track.duration), (TITLE, [ARTIST], 252))

    def test_tiny_trailing_number_is_not_duration(self) -> None:
        # 20 秒以下不当时长(现实里没有这种曲目)
        track = only(f"{TITLE} - {ARTIST} 12")
        self.assertIsNone(track.duration)

    def test_title_ending_with_number_keeps_number(self) -> None:
        # 没有标题-艺人分隔符时, 尾巴上的裸数字留在标题里(保守)
        track = only(f"01. {TITLE} 10")
        self.assertEqual((track.title, track.duration), (f"{TITLE} 10", None))

    def test_mid_line_duration_not_extracted(self) -> None:
        # 时长只认"尾部"或"括号里"; 夹在中间的 4:12 保守地留在标题里
        track = only(f"01. {TITLE} 4:12 - {ARTIST}")
        self.assertEqual((track.title, track.duration), (f"{TITLE} 4:12", None))

    def test_duration_before_separator(self) -> None:
        track = only(f"{TITLE} (4:12) / {ARTIST}")
        self.assertEqual((track.title, track.artists, track.duration), (TITLE, [ARTIST], 252))

    def test_duration_not_treated_as_title(self) -> None:
        title = only(f"01. {TITLE} 4:12").title
        self.assertNotIn("4:12", title)


# --------------------------------------------------------------------------
# 全角 / 归一化
# --------------------------------------------------------------------------


class TestFullWidth(unittest.TestCase):
    def test_full_digits_period_and_colon(self) -> None:
        track = only("０１．" + TITLE + "　４：１２")
        self.assertEqual((track.number, track.title, track.duration), (1, TITLE, 252))

    def test_full_width_parentheses(self) -> None:
        track = only(TITLE + "（４：１２）")
        self.assertEqual((track.title, track.duration), (TITLE, 252))

    def test_full_width_slash_separator(self) -> None:
        track = only(TITLE + " ／ " + ARTIST)
        self.assertEqual((track.title, track.artists), (TITLE, [ARTIST]))

    def test_full_width_latin_prefix(self) -> None:
        self.assertEqual(only("ＴＲＡＣＫ　１ " + TITLE).title, TITLE)

    def test_full_width_hash_prefix(self) -> None:
        self.assertEqual(only("＃０１ " + TITLE).number, 1)

    def test_raw_keeps_original_line(self) -> None:
        line = "０１．" + TITLE + "　４：１２"
        track = only(line)
        self.assertEqual(track.raw, line)          # 归一化只用于匹配, raw 是原行

    def test_crlf_and_cr(self) -> None:
        blob = f"01. {TITLE}\r\n02. {TITLE2}\r\n"
        self.assertEqual(titles(blob), [TITLE, TITLE2])


# --------------------------------------------------------------------------
# 表格粘贴
# --------------------------------------------------------------------------


class TestTablePaste(unittest.TestCase):
    def test_four_columns(self) -> None:
        track = only(f"01\t{TITLE}\t{ARTIST}\t4:12")
        self.assertEqual((track.number, track.title, track.artists, track.duration),
                         (1, TITLE, [ARTIST], 252))

    def test_columns_without_number(self) -> None:
        track = only(f"{TITLE}\t{ARTIST}\t4:12")
        self.assertEqual((track.title, track.artists, track.duration), (TITLE, [ARTIST], 252))

    def test_two_columns_title_artist(self) -> None:
        track = only(f"{TITLE}\t{ARTIST}")
        self.assertEqual((track.title, track.artists), (TITLE, [ARTIST]))

    def test_two_columns_title_duration(self) -> None:
        track = only(f"{TITLE}\t4:12")
        self.assertEqual((track.title, track.artists, track.duration), (TITLE, [], 252))

    def test_seconds_column(self) -> None:
        track = only(f"{TITLE}\t{ARTIST}\t252")
        self.assertEqual((track.title, track.artists, track.duration), (TITLE, [ARTIST], 252))

    def test_artist_cell_with_multiple_artists(self) -> None:
        track = only(f"01\t{TITLE}\t{ARTIST}, {ARTIST2}\t4:12")
        self.assertEqual(track.artists, [ARTIST, ARTIST2])

    def test_title_cell_with_separator(self) -> None:
        track = only(f"01\t{TITLE} - {ARTIST}\t4:12")
        self.assertEqual((track.title, track.artists), (TITLE, [ARTIST]))

    def test_title_cell_with_bracket_duration(self) -> None:
        track = only(f"{TITLE} (4:12)\t{ARTIST}")
        self.assertEqual((track.title, track.artists, track.duration), (TITLE, [ARTIST], 252))

    def test_dotted_index_cell(self) -> None:
        track = only(f"01.\t{TITLE}\t4:12")
        self.assertEqual((track.number, track.title, track.duration), (1, TITLE, 252))

    def test_multi_row_blob(self) -> None:
        blob = "\n".join([
            f"01\t{TITLE}\t{ARTIST}\t4:12",
            f"02\t{TITLE2}\t{ARTIST2}\t5:03",
        ])
        tracks = tp.parse_tracklist(blob)
        self.assertEqual(titles(blob), [TITLE, TITLE2])
        self.assertEqual([t.duration for t in tracks], [252, 303])
        self.assertEqual(tracks[1].artists, [ARTIST2])


# --------------------------------------------------------------------------
# 跳过: 空行 / 注释 / 表头 / 碟号
# --------------------------------------------------------------------------


class TestSkipLines(unittest.TestCase):
    def test_blank_lines(self) -> None:
        blob = f"\n\n01. {TITLE}\n   \n\t\n02. {TITLE2}\n"
        self.assertEqual(titles(blob), [TITLE, TITLE2])

    def test_comment_lines(self) -> None:
        blob = "\n".join([
            "# 这是注释, 不是曲目",
            "// comment",
            "; comment",
            "※ 补足说明",
            f"01. {TITLE}",
        ])
        self.assertEqual(titles(blob), [TITLE])

    def test_hash_track_number_is_not_comment(self) -> None:
        self.assertEqual(titles(f"#01 {TITLE}"), [TITLE])

    def test_header_lines(self) -> None:
        blob = "\n".join([
            "曲目列表",
            "曲目",
            "Tracklist",
            "Track List",
            f"01. {TITLE}",
        ])
        self.assertEqual(titles(blob), [TITLE])

    def test_header_columns(self) -> None:
        for line in ("# 曲名 / アーティスト / 時間", "Title\tArtist\tTime",
                     "曲名　アーティスト　時間", "# 曲目 アーティスト"):
            with self.subTest(line=line):
                self.assertEqual(tp.parse_tracklist(line), [])

    def test_latin_single_word_title_is_kept(self) -> None:
        # Title / Time / Name 这种英文单词单独出现时宁可当曲名(Pink Floyd 的 Time)
        for line in ("Time", "Title", "Name"):
            with self.subTest(line=line):
                self.assertEqual(titles(line), [line])

    def test_disc_and_volume_headers(self) -> None:
        blob = "\n".join([
            "Disc 1", "CD2", "ディスク1", "Vol.1", "===========", "01. " + TITLE,
        ])
        self.assertEqual(titles(blob), [TITLE])

    def test_bullet_lines(self) -> None:
        blob = f"・{TITLE}\n- {TITLE2}\n* {TITLE}"
        self.assertEqual(titles(blob), [TITLE, TITLE2, TITLE])


# --------------------------------------------------------------------------
# 保守性: 不该错拆的例子
# --------------------------------------------------------------------------


class TestConservative(unittest.TestCase):
    def test_dash_inside_title_without_spaces(self) -> None:
        track = only("Re:Collection -X-")
        self.assertEqual((track.title, track.artists), ("Re:Collection -X-", []))

    def test_dash_inside_title_with_single_side_space(self) -> None:
        track = only("Oriental -Dark-")
        self.assertEqual(track.artists, [])

    def test_slash_without_spaces_is_not_a_separator(self) -> None:
        track = only(f"{TITLE}/{ARTIST}")
        self.assertEqual((track.title, track.artists), (f"{TITLE}/{ARTIST}", []))

    def test_glued_kanji_month_is_title(self) -> None:
        for line in ("4月の雨", "12月の雨"):
            with self.subTest(line=line):
                track = only(line)
                self.assertEqual((track.title, track.number), (line, None))

    def test_glued_thousands_is_title(self) -> None:
        track = only("1,000人の夕無")
        self.assertEqual((track.title, track.number), ("1,000人の夕無", None))

    def test_colon_time_like_title_is_kept(self) -> None:
        track = only("4:12 幻想")
        self.assertEqual((track.title, track.number), ("4:12 幻想", None))

    def test_title_first_ordering(self) -> None:
        # ``艺人 - 曲名`` 顺序反了也按 标题/艺人 拆(不做语义猜测), 但至少不错位
        track = only(f"01. {ARTIST} - {TITLE}")
        self.assertEqual((track.title, track.artists), (ARTIST, [TITLE]))

    def test_no_artist_extraction_from_parenthesis(self) -> None:
        # ``(ShibayanRecords)`` 不是时长 -> 留在标题里, 不硬当艺人
        track = only(f"{TITLE} ({ARTIST})")
        self.assertEqual((track.title, track.artists), (f"{TITLE} ({ARTIST})", []))


# --------------------------------------------------------------------------
# 边界 / 契约 / 性能
# --------------------------------------------------------------------------


class TestEdgesAndContract(unittest.TestCase):
    def test_empty_input(self) -> None:
        for value in ("", "   ", "\n\n\t \n", None):
            with self.subTest(value=value):
                self.assertEqual(tp.parse_tracklist(value), [])  # type: ignore[arg-type]

    def test_numbering_without_numbers(self) -> None:
        blob = f"{TITLE}\n{TITLE2}\n{ARTIST}"
        tracks = tp.parse_tracklist(blob)
        self.assertEqual([t.index for t in tracks], [1, 2, 3])
        self.assertEqual([t.number for t in tracks], [None, None, None])

    def test_order_preserved(self) -> None:
        blob = "".join(f"{i:02d}. 曲{i}\n" for i in range(1, 11))
        self.assertEqual(titles(blob), [f"曲{i}" for i in range(1, 11)])

    def test_whitespace_trimmed(self) -> None:
        track = only(f"   01.   {TITLE}   -   {ARTIST}   4:12   ")
        self.assertEqual((track.title, track.artists, track.duration), (TITLE, [ARTIST], 252))
        self.assertEqual(track.raw, f"01.   {TITLE}   -   {ARTIST}   4:12")

    def test_duration_text_property(self) -> None:
        self.assertEqual(only(f"{TITLE} (4:12)").duration_text, "4:12")
        self.assertEqual(only(TITLE).duration_text, "")

    def test_to_dict_contract(self) -> None:
        track = only(f"01. {TITLE} / {ARTIST} 4:12")
        data = track.to_dict()
        self.assertEqual(set(data), {"index", "title", "artists", "duration",
                                     "duration_text", "number", "raw", "disc"})
        self.assertEqual(data["artists"], [ARTIST])
        self.assertEqual(data["duration"], 252)
        self.assertEqual(data["index"], 1)
        self.assertEqual(data["number"], 1)

    def test_artists_list_is_not_shared(self) -> None:
        track = only(f"{TITLE} / {ARTIST}")
        track.artists.append("x")
        self.assertEqual(len(tp.parse_tracklist(f"{TITLE} / {ARTIST}")[0].artists), 1)

    def test_dataclass_shape(self) -> None:
        track = tp.ParsedTrack()
        self.assertEqual((track.index, track.title, track.artists, track.duration,
                          track.raw, track.number), (0, "", [], None, "", None))

    def test_long_list_performance(self) -> None:
        lines = []
        for i in range(1, 201):
            if i % 4 == 0:
                lines.append(f"{i:02d}. {TITLE} {i} - {ARTIST} 4:12")
            elif i % 4 == 1:
                lines.append(f"Track {i} {TITLE} {i} / {ARTIST}")
            elif i % 4 == 2:
                lines.append(f"{i:02d}\t{TITLE} {i}\t{ARTIST}\t5:03")
            else:
                lines.append(f"{i:02d}. {TITLE} {i}")
        blob = "\n".join(lines)
        started = time.perf_counter()
        tracks = tp.parse_tracklist(blob)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(tracks), 200)
        self.assertEqual([t.index for t in tracks], list(range(1, 201)))
        self.assertLess(elapsed, 1.0, f"200 行解析耗时 {elapsed:.3f}s, 太慢")
        self.assertEqual(tracks[3].duration, 252)
        self.assertEqual(tracks[5].duration, 303)
        self.assertEqual(tracks[1].artists, [ARTIST])

    def test_realistic_blob(self) -> None:
        blob = "\n".join([
            "■ 収録曲",
            "01. " + TITLE + " - " + ARTIST + " 4:12",
            "02. " + TITLE2 + " / " + ARTIST2 + " 5:03",
            "03. ４：１２",                     # 只有时长 -> 跳过
            "",
            "# 以上",
            "04. " + TITLE + " (3:59)",
        ])
        tracks = tp.parse_tracklist(blob)
        self.assertEqual(len(tracks), 3)
        self.assertEqual([t.number for t in tracks], [1, 2, 4])
        self.assertEqual([t.title for t in tracks], [TITLE, TITLE2, TITLE])
        self.assertEqual([t.duration for t in tracks], [252, 303, 239])
        self.assertEqual(tracks[0].artists, [ARTIST])

    def test_full_width_only_seconds_line_skipped(self) -> None:
        self.assertEqual(tp.parse_tracklist("　２５２　"), [])

    def test_tabs_only_line(self) -> None:
        self.assertEqual(tp.parse_tracklist("\t\t\t"), [])


class TestHelpers(unittest.TestCase):
    def test_parse_duration_colon(self) -> None:
        self.assertEqual(tp.parse_duration("4:12"), 252)
        self.assertEqual(tp.parse_duration("04:12"), 252)
        self.assertEqual(tp.parse_duration("1:02:03"), 3723)
        self.assertEqual(tp.parse_duration("0:59"), 59)

    def test_parse_duration_seconds(self) -> None:
        self.assertEqual(tp.parse_duration("252"), 252)
        self.assertEqual(tp.parse_duration("252秒"), 252)
        self.assertEqual(tp.parse_duration("4分12秒"), 252)

    def test_parse_duration_brackets(self) -> None:
        for text in ("[4:12]", "(4:12)", "【4:12】", " 4:12 "):
            with self.subTest(text=text):
                self.assertEqual(tp.parse_duration(text), 252)

    def test_parse_duration_invalid(self) -> None:
        for text in ("", None, "abc", "0", "0:00", 0, "24:99:99x", "-4:12"):
            with self.subTest(text=text):
                self.assertIn(tp.parse_duration(text), (None, 0))

    def test_format_duration(self) -> None:
        self.assertEqual(tp.format_duration(252), "4:12")
        self.assertEqual(tp.format_duration(59), "0:59")
        self.assertEqual(tp.format_duration(3723), "1:02:03")
        self.assertEqual(tp.format_duration(0), "0:00")
        self.assertEqual(tp.format_duration(None), "")
        self.assertEqual(tp.format_duration(-5), "")

    def test_roundtrip(self) -> None:
        for seconds in (1, 59, 60, 252, 3599, 3600, 3723, 86399):
            with self.subTest(seconds=seconds):
                self.assertEqual(tp.parse_duration(tp.format_duration(seconds)), seconds)


if __name__ == "__main__":                                  # pragma: no cover
    unittest.main()