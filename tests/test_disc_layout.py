"""测试: 曲目表的分碟布局(参考网易云唱片编辑界面)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _album(tracks, **overrides):
    base = {
        "key": "d", "title": "T", "title_original": "T", "title_alt": "",
        "romaji": "", "circle": "", "artists": [], "credits": [], "urls": {},
        "tags": [], "genres": [], "missing": [], "cover": "", "has_cover": False,
        "cover_local": "", "date": "", "event": "", "catalog_no": "", "price": "",
        "hall": "", "booth": "", "updated_text": "", "event_kind": "", "year": "",
        "description": "", "track_count": len(tracks), "tracks": tracks,
    }
    base.update(overrides)
    return base


def _render(album):
    from dddb.public import templates as T

    ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                        user={"id": 1, "username": "ed", "role": "editor"})
    return T.edit_album_page(ctx, album=album,
                             values=T.edit_values_from_album(album))


class TestDiscBlockLayout(unittest.TestCase):
    """曲目按碟分组, 每碟独立区块."""

    def test_multi_disc_blocks(self) -> None:
        html = _render(_album([
            {"disc": 1, "index": 1, "title": "A", "artists": [],
             "duration": 100},
            {"disc": 1, "index": 2, "title": "B", "artists": [],
             "duration": 110},
            {"disc": 2, "index": 1, "title": "C", "artists": [],
             "duration": 120},
        ]))
        discs = re.findall(r'class="disc-block" data-disc="(\d+)"', html)
        self.assertEqual(discs, ["1", "2"])
        self.assertIn("Disc 01", html)
        self.assertIn("Disc 02", html)

    def test_single_disc(self) -> None:
        html = _render(_album([
            {"disc": 1, "index": 1, "title": "A", "artists": [],
             "duration": 100},
        ]))
        self.assertEqual(len(re.findall(r'class="disc-block"', html)), 1)

    def test_empty_tracks_still_has_block(self) -> None:
        html = _render(_album([]))
        self.assertEqual(len(re.findall(r'class="disc-block"', html)), 1)
        self.assertIn("暂无数据", html)

    def test_disc_sorted(self) -> None:
        """碟号要按数字排序, 不能按字符串(10 不能排在 2 前面).

        rows_for 会自动补空行, 所以可能出现碟 1 的空 block;
        这里只检查"有碟号的都按数字升序"。
        """
        html = _render(_album([
            {"disc": 10, "index": 1, "title": "J", "artists": [],
             "duration": 100},
            {"disc": 2, "index": 1, "title": "B", "artists": [],
             "duration": 100},
        ]))
        discs = [int(n) for n in re.findall(
            r'class="disc-block" data-disc="(\d+)"', html)]
        self.assertEqual(discs, sorted(discs), "碟号应按数字升序")
        self.assertIn(2, discs)
        self.assertIn(10, discs)
        self.assertLess(discs.index(2), discs.index(10),
                        "2 必须排在 10 之前")


class TestDiscTools(unittest.TestCase):
    """每碟的操作按钮齐全."""

    def setUp(self) -> None:
        self.html = _render(_album([
            {"disc": 1, "index": 1, "title": "A", "artists": [],
             "duration": 100},
        ]))

    def test_move_buttons_replace_the_old_renumber(self) -> None:
        # 用户 m16568: 「调整曲轨符」点了只重排音轨号, 跟碟顺序无关, 换成上移/下移
        self.assertIn('data-disc-move="-1"', self.html)
        self.assertIn('data-disc-move="1"', self.html)
        self.assertIn("上移", self.html)
        self.assertIn("下移", self.html)
        self.assertNotIn("data-disc-renumber", self.html)
        self.assertNotIn("调整曲轨符", self.html)

    def test_remove_disc_button(self) -> None:
        self.assertIn('data-disc-remove="1"', self.html)
        self.assertIn("删除光盘", self.html)

    def test_bulk_add_button(self) -> None:
        """「批量添加歌曲」按钮已按用户要求删除."""
        self.assertNotIn('data-disc-bulk="1"', self.html)
        self.assertNotIn("批量添加歌曲", self.html)

    def test_add_song_button_per_disc(self) -> None:
        self.assertIn('data-track-add="1"', self.html)
        self.assertIn("添加专辑歌曲", self.html)

    def test_add_disc_button(self) -> None:
        self.assertIn('data-disc-add="1"', self.html)
        self.assertIn("添加光盘", self.html)

    def test_row_delete_and_move(self) -> None:
        self.assertIn('data-track-del="1"', self.html)
        # 排序: 拖动把手(按住拖动), 不再有 up/down 按钮
        self.assertIn('data-track-drag="1"', self.html)
        self.assertIn('class="trk-drag"', self.html)
        self.assertNotIn('data-track-move', self.html)

    def test_no_inline_onclick(self) -> None:
        """CSP script-src 'self', 不许内联 onclick."""
        for m in re.finditer(r"<button[^>]*>", self.html):
            self.assertNotIn("onclick", m.group(0))


class TestTrackTable(unittest.TestCase):
    """表头与列顺序符合截图."""

    def setUp(self) -> None:
        self.html = _render(_album([
            {"disc": 1, "index": 1, "title": "A", "artists": [],
             "duration": 100},
        ]))

    def test_table_headers(self) -> None:
        """列顺序: 排序(⠿) 放最前, 时长加回来。"""
        heads = re.findall(r"<th>([^<]*)</th>", self.html)
        self.assertEqual(heads, ["排序", "音轨", "歌曲名", "副标题",
                                 "翻译歌名", "艺人", "语种", "曲风",
                                 "时长", "操作"])

    def test_no_visible_disc_input(self) -> None:
        """左侧不再显示碟号输入框(碟号是隐藏字段)."""
        visible = re.findall(
            r'<input[^>]*type="text"[^>]*name="track_disc"', self.html)
        visible += re.findall(
            r'<input[^>]*name="track_disc"[^>]*type="text"', self.html)
        self.assertEqual(visible, [], "碟号不该出现在表格列里")

    def test_hidden_disc_field_present(self) -> None:
        hidden = re.findall(
            r'<input[^>]*type="hidden"[^>]*name="track_disc"[^>]*>', self.html)
        if not hidden:
            hidden = re.findall(
                r'<input[^>]*name="track_disc"[^>]*value="[^"]*"[^>]*type="hidden"',
                self.html)
        self.assertGreaterEqual(len(hidden), 1, "提交仍要带碟号")

    def test_all_track_fields(self) -> None:
        for name in ("track_index", "track_title", "track_subtitle",
                     "track_title_zh", "track_artists", "track_lang",
                     "track_genre", "track_disc", "track_duration"):
            self.assertIn('name="{}"'.format(name), self.html,
                          f"缺字段 {name}")


class TestDiscJs(unittest.TestCase):
    """JS 支持分碟增删与排序."""

    def setUp(self) -> None:
        self.js = (ROOT / "dddb/public/static/public.js").read_text(
            encoding="utf-8")

    def test_has_disc_functions(self) -> None:
        for fn in ("addDiscBlock", "renumberDisc", "discNumber", "discBody"):
            self.assertIn("function {}( ".format(fn).replace("( ", "("),
                          self.js, f"缺函数 {fn}")

    def test_handles_disc_add(self) -> None:
        self.assertIn("data-disc-add", self.js)

    def test_handles_disc_remove(self) -> None:
        self.assertIn("data-disc-remove", self.js)

    def test_handles_move(self) -> None:
        """排序改为**拖动**方式(不再用上下移按钮)."""
        self.assertIn("data-track-drag", self.js)
        self.assertIn("dragstart", self.js)
        self.assertNotIn("data-track-move", self.js,
                         "上下移按钮已改为拖动排序")

    def test_renumbers_on_submit(self) -> None:
        """提交时按 DOM 顺序重排音轨号."""
        start = self.js.find("function setupTrackRows")
        end = self.js.find("\n  function ", start + 10)
        block = self.js[start:end if end > 0 else start + 6000]
        self.assertIn("submit", block)
        self.assertIn("track_index", block)

    def test_at_least_one_disc_guard(self) -> None:
        """删碟时至少留一碟."""
        self.assertIn("length <= 1", self.js)


class TestDiscCss(unittest.TestCase):
    def test_disc_styles(self) -> None:
        css = (ROOT / "dddb/public/static/public.css").read_text(
            encoding="utf-8")
        for token in (".disc-block", ".disc-head", ".track-table",
                      ".trk-drag", ".disc-add-row", ".disc-empty"):
            self.assertIn(token, css, f"缺样式 {token}")
        # 排序已从上下移按钮改为拖动把手
        self.assertNotIn(".trk-move", css, "旧的上下移按钮样式应已清理")


class TestDiscRoundTrip(unittest.TestCase):
    """分碟数据保存后不丢碟号."""

    def test_multi_disc_persist(self) -> None:
        from tests._tmp import temp_dir

        from dddb.config import Config
        from dddb.db import Store
        from dddb.models import Album
        from dddb.public import templates as T
        from dddb.public.site import parse_album_form

        ctx = temp_dir("discrt-")
        root = Path(ctx.__enter__())
        try:
            config = Config.default(root=root)
            config.ensure_dirs()
            store = Store(config.db_path)
            store.upsert_album(Album.from_dict({
                "key": "rt", "title": "分碟往返", "tracks": [],
                "credits": [], "source_ids": {},
            }))
            store.close()

            album = _album([
                {"disc": 1, "index": 1, "title": "A", "artists": ["X"],
                 "duration": 100},
                {"disc": 1, "index": 2, "title": "B", "artists": [],
                 "duration": 110},
                {"disc": 2, "index": 1, "title": "C", "artists": [],
                 "duration": 120},
            ])
            values = T.edit_values_from_album(album)
            rows = values["tracks"]

            # 模拟浏览器提交(每个字段名重复)
            form = {"title": ["分碟往返"], "title_original": ["分碟往返"]}
            for field in ("track_disc", "track_index", "track_title",
                          "track_artists", "track_duration"):
                short = field.replace("track_", "")
                form[field] = [str(row.get(short) or "") for row in rows]

            patch, errors = parse_album_form(form, current=album)
            self.assertEqual(errors, {})

            store = Store(config.db_path)
            ok, msg = store.update_album_fields("rt", patch)
            self.assertTrue(ok, msg)
            saved = store.get_album("rt")
            store.close()

            self.assertEqual([t.disc for t in saved.tracks], [1, 1, 2],
                             "碟号必须落库")
            self.assertEqual([t.title for t in saved.tracks], ["A", "B", "C"])
        finally:
            ctx.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()