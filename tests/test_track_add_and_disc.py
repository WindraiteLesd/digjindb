"""测试: 曲目表的「新增一首」与「分碟」功能."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _album(**overrides):
    base = {
        "key": "d1", "title": "T", "title_original": "T", "title_alt": "",
        "romaji": "", "circle": "", "artists": [], "credits": [], "urls": {},
        "tags": [], "genres": [], "missing": [], "cover": "", "has_cover": False,
        "cover_local": "", "date": "", "event": "", "catalog_no": "", "price": "",
        "hall": "", "booth": "", "updated_text": "", "event_kind": "", "year": "",
        "description": "", "track_count": 0, "tracks": [],
    }
    base.update(overrides)
    return base


def _render(album):
    from dddb.public import templates as T

    ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                        user={"id": 1, "username": "ed", "role": "editor"})
    return T.edit_album_page(ctx, album=album,
                             values=T.edit_values_from_album(album))


class TestAddTrackRow(unittest.TestCase):
    """编辑页要能动态添加曲目行(不用刷新)."""

    def setUp(self) -> None:
        self.html = _render(_album(tracks=[
            {"disc": 1, "index": 1, "title": "曲一", "artists": [],
             "duration": 180},
        ]))
        self.js = (ROOT / "dddb/public/static/public.js").read_text(encoding="utf-8")
        self.css = (ROOT / "dddb/public/static/public.css").read_text(encoding="utf-8")

    def test_container_has_id(self) -> None:
        self.assertIn('id="track-rows"', self.html,
                      "曲目容器要有 id, JS 才找得到")

    def test_add_button_exists(self) -> None:
        """「添加一首」改为**每碟**自己的按钮(底部那个已删)."""
        self.assertIn('data-track-add="1"', self.html)
        self.assertIn("添加专辑歌曲", self.html)
        self.assertNotIn('id="add-track-row"', self.html,
                         "底部的旧按钮已删除")

    def test_add_disc_button_exists(self) -> None:
        """「添加光盘」保留(底部「添加一碟(10首)」已删)."""
        self.assertIn('data-disc-add="1"', self.html)
        self.assertIn("添加光盘", self.html)
        self.assertNotIn('id="add-disc-rows"', self.html,
                         "底部的旧按钮已删除")

    def test_buttons_not_inline_onclick(self) -> None:
        """CSP script-src 'self', 不许内联 onclick."""
        for match in re.finditer(r"<button[^>]*track-add[^>]*>", self.html):
            self.assertNotIn("onclick", match.group(0))

    def test_js_builds_row_with_all_fields(self) -> None:
        """新增行必须带全部 track_* 字段名, 否则解析时会错位.

        新结构把可编辑列定义在 ``TRACK_COLS`` 常量里(音轨/曲名/副标题/
        翻译/艺人/语种/曲风), 碟号与时长用隐藏字段补上。
        """
        self.assertIn("function buildTrackRow", self.js)
        # 列定义常量必须含这些字段
        self.assertIn("TRACK_COLS", self.js)
        cols_start = self.js.find("var TRACK_COLS")
        # 用 "];\n" 结尾定位(数组元素里有 "]" 不会误判)
        cols_end = self.js.find("\n  ];", cols_start)
        if cols_end < 0:
            cols_end = self.js.find("];", cols_start)
        cols = self.js[cols_start:cols_end]
        for field in ("track_index", "track_title", "track_subtitle",
                      "track_title_zh", "track_artists", "track_lang",
                      "track_genre", "track_duration"):
            self.assertIn(field, cols, f"TRACK_COLS 缺字段 {field}")

        # buildTrackRow: 碟号用**隐藏字段**(放排序列 td 内);
        # 时长已是 TRACK_COLS 里的正经列(用户要求加回来), 不再是隐藏字段。
        start = self.js.find("function buildTrackRow")
        end = self.js.find("\n  function ", start + 10)
        block = self.js[start:end if end > 0 else start + 3000]
        self.assertIn("track_disc", block, "新增行缺隐藏字段 track_disc")
        self.assertIn("discHidden", block, "碟号隐藏字段应放进 td")
        self.assertNotIn("track_duration", block,
                         "时长已是列, 不该再作为隐藏字段")

    def test_js_bound_at_boot(self) -> None:
        self.assertIn("setupTrackRows()", self.js)

    def test_js_uses_event_delegation(self) -> None:
        """用事件委托(动态插入的行也能响应)."""
        start = self.js.find("function setupTrackRows")
        end = self.js.find("\n  function ", start + 10)
        block = self.js[start:end if end > 0 else start + 3000]
        self.assertIn("addEventListener", block)
        self.assertIn("data-track-add", block)


class TestDiscColumn(unittest.TestCase):
    """分碟: 碟号 渲染/解析/存储 全链路."""

    def test_render_disc_input(self) -> None:
        html = _render(_album(tracks=[
            {"disc": 1, "index": 1, "title": "碟1曲1", "artists": [],
             "duration": 180},
        ]))
        # rows_for(..., 3) 会自动补空行, 所以断言"碟号框与曲名框数量一致"
        disc_inputs = re.findall(r'name="track_disc"', html)
        title_inputs = re.findall(r'name="track_title"', html)
        self.assertGreaterEqual(len(disc_inputs), 1)
        self.assertEqual(len(disc_inputs), len(title_inputs),
                         "碟号框数必须与曲名框数一致, 否则五列会错位")

    def test_single_disc_leaves_blank(self) -> None:
        """单碟专辑碟号留空(表单里空 = 1), 不干扰旧数据."""
        from dddb.public import templates as T

        values = T.edit_values_from_album(_album(tracks=[
            {"disc": 1, "index": 1, "title": "曲一", "artists": [],
             "duration": 180},
        ]))
        self.assertEqual(values["tracks"][0]["disc"], "",
                         "单碟应留空")

    def test_multi_disc_shows_number(self) -> None:
        from dddb.public import templates as T

        values = T.edit_values_from_album(_album(tracks=[
            {"disc": 2, "index": 1, "title": "曲一", "artists": [],
             "duration": 180},
        ]))
        self.assertEqual(values["tracks"][0]["disc"], 2)

    def test_parse_disc(self) -> None:
        from dddb.public.site import parse_album_form

        values = {
            "title": ["T"], "title_original": ["T"],
            "track_disc": ["1", "1", "2"],
            "track_index": ["1", "2", "1"],
            "track_title": ["a", "b", "c"],
        }
        patch, errors = parse_album_form(values)
        self.assertEqual(errors, {})
        tracks = patch["track_list"]
        self.assertEqual(len(tracks), 3)
        self.assertEqual([t["disc"] for t in tracks], [1, 1, 2])

    def test_blank_disc_defaults_to_one(self) -> None:
        from dddb.public.site import parse_album_form

        patch, _ = parse_album_form({
            "title": ["T"], "title_original": ["T"],
            "track_disc": [""], "track_index": ["1"], "track_title": ["a"],
        })
        self.assertEqual(patch["track_list"][0]["disc"], 1)

    def test_invalid_disc_reports_error(self) -> None:
        from dddb.public.site import parse_album_form

        _patch, errors = parse_album_form({
            "title": ["T"], "title_original": ["T"],
            "track_disc": ["abc"], "track_index": ["1"], "track_title": ["a"],
        })
        self.assertIn("tracks", errors)
        self.assertIn("碟号", errors["tracks"])

    def test_detail_page_shows_disc(self) -> None:
        """详情页多碟时显示 2-1 这样的编号."""
        from dddb.public import templates as T

        ctx = T.SiteContext(csrf_token="t", is_editor=False, lang="zh")
        album = {
            "key": "d1", "title": "多碟", "urls": {}, "credits": [],
            "tags": [], "genres": [],
            "tracks": [
                {"disc": 1, "index": 1, "title": "A", "artists": [],
                 "duration": 100, "duration_text": "1:40"},
                {"disc": 2, "index": 1, "title": "B", "artists": [],
                 "duration": 200, "duration_text": "3:20"},
            ],
        }
        html = T.album_detail_page(ctx, album=album)
        self.assertIn("2-1", html, "多碟时序号应显示 碟-曲")


class TestTrackFieldsRoundTrip(unittest.TestCase):
    """五列一起往返: 碟/序号/曲名/艺人/时长 都不许错位."""

    def test_all_fields_roundtrip(self) -> None:
        from dddb.public import templates as T
        from dddb.public.site import parse_album_form

        album = _album(tracks=[
            {"disc": 1, "index": 1, "title": "曲一", "artists": ["A", "B"],
             "duration": 180},
            {"disc": 2, "index": 1, "title": "曲二", "artists": ["C"],
             "duration": 200},
        ])
        values = T.edit_values_from_album(album)
        rows = values["tracks"]
        self.assertEqual(len(rows), 2)

        # 模拟浏览器提交(每个字段名重复出现)
        form = {"title": ["T"], "title_original": ["T"]}
        for field in ("track_disc", "track_index", "track_title",
                      "track_artists", "track_duration"):
            form[field] = [str(row.get(field.replace("track_", ""), "")
                               or row.get({
                                   "track_disc": "disc",
                                   "track_index": "index",
                                   "track_title": "title",
                                   "track_artists": "artists",
                                   "track_duration": "duration",
                               }[field], "")) for row in rows]

        patch, errors = parse_album_form(form)
        self.assertEqual(errors, {})
        tracks = patch["track_list"]
        self.assertEqual([t["disc"] for t in tracks], [1, 2])
        self.assertEqual([t["title"] for t in tracks], ["曲一", "曲二"])
        self.assertEqual(tracks[0]["artists"], ["A", "B"])
        self.assertEqual(tracks[0]["duration"], 180)


class TestDurationFormatting(unittest.TestCase):
    """时长在编辑页要显示 分:秒."""

    def test_seconds_formatted(self) -> None:
        from dddb.public import templates as T

        values = T.edit_values_from_album(_album(tracks=[
            {"disc": 1, "index": 1, "title": "a", "artists": [],
             "duration": 180},
        ]))
        self.assertEqual(values["tracks"][0]["duration"], "3:00")

    def test_existing_text_kept(self) -> None:
        from dddb.public.templates import _format_duration_text

        self.assertEqual(_format_duration_text("3:00"), "3:00")

    def test_empty_ok(self) -> None:
        from dddb.public.templates import _format_duration_text

        self.assertEqual(_format_duration_text(None), "")
        self.assertEqual(_format_duration_text(""), "")

    def test_odd_seconds(self) -> None:
        from dddb.public.templates import _format_duration_text

        self.assertEqual(_format_duration_text(3661), "61:01")


if __name__ == "__main__":
    unittest.main()