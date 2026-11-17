"""测试: 曲目表 HTML 结构合法(tr 里只能有 td/th)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _album(tracks=None, **overrides):
    base = {
        "key": "d", "title": "T", "title_original": "T", "title_alt": "",
        "romaji": "", "circle": "", "artists": [], "credits": [], "urls": {},
        "tags": [], "genres": [], "missing": [], "cover": "", "has_cover": False,
        "cover_local": "", "date": "", "event": "", "catalog_no": "", "price": "",
        "hall": "", "booth": "", "updated_text": "", "event_kind": "", "year": "",
        "description": "", "track_count": len(tracks or []),
        "tracks": tracks if tracks is not None else [
            {"disc": 1, "index": 1, "title": "曲一", "artists": [],
             "duration": 100},
            {"disc": 1, "index": 2, "title": "曲二", "artists": [],
             "duration": 200},
        ],
    }
    base.update(overrides)
    return base


def _render(album):
    from dddb.public import templates as T

    ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                        user={"id": 1, "username": "ed", "role": "editor"})
    return T.edit_album_page(ctx, album=album,
                             values=T.edit_values_from_album(album))


class TestTrStructure(unittest.TestCase):
    """<tr> 的直接子元素只能是 td/th.

    <tr> 里直接放 <input> 是非法 HTML —— 浏览器会把它们移到表格外并重排
    整行, 表现为"每行被拆成两行、文字重叠"(真实截图确认过)。
    """

    def test_no_non_cell_child_in_tr(self) -> None:
        html = _render(_album())
        offenders = []
        for m in re.finditer(r"<tr\b[^>]*>([\s\S]*?)</tr>", html):
            inner = m.group(1)
            # 去掉所有 td/th 块, 剩下的都算非法
            stripped = re.sub(r"<(td|th)\b[^>]*>[\s\S]*?</\1>", "", inner)
            rest = stripped.strip()
            if rest:
                offenders.append(rest[:100])
        self.assertEqual(offenders, [],
                         f"tr 里有非法子元素: {offenders[:2]}")

    def test_hidden_inputs_inside_td(self) -> None:
        html = _render(_album())
        m = re.search(r'<tr class="track-row"[\s\S]*?</tr>', html)
        self.assertIsNotNone(m)
        row = m.group(0)
        for im in re.finditer(r'<input[^>]*type="hidden"[^>]*>', row):
            before = row[:im.start()]
            self.assertGreater(before.rfind("<td"), before.rfind("</td>"),
                               "hidden 字段必须在 td 内")
        # 两个 hidden 都要在
        self.assertIn('name="track_disc"', row)
        self.assertIn('name="track_duration"', row)

    def test_cell_count_matches_header(self) -> None:
        html = _render(_album())
        headers = re.findall(r"<th[^>]*>([^<]*)</th>", html)
        m = re.search(r'<tr class="track-row"[\s\S]*?</tr>', html)
        cells = re.findall(r"<td", m.group(0))
        self.assertEqual(len(cells), len(headers),
                         f"表头 {len(headers)} 列, 数据行 {len(cells)} 格")

    def test_multiple_discs_all_valid(self) -> None:
        html = _render(_album(tracks=[
            {"disc": 1, "index": 1, "title": "A", "artists": [],
             "duration": 100},
            {"disc": 2, "index": 1, "title": "B", "artists": [],
             "duration": 200},
            {"disc": 2, "index": 2, "title": "C", "artists": [],
             "duration": 300},
        ]))
        for m in re.finditer(r"<tr\b[^>]*>([\s\S]*?)</tr>", html):
            inner = m.group(1)
            stripped = re.sub(r"<(td|th)\b[^>]*>[\s\S]*?</\1>", "", inner)
            self.assertEqual(stripped.strip(), "",
                             "每个 tr 都只能有 td/th")

    def test_js_puts_hidden_in_td(self) -> None:
        """JS 新增行也要把 hidden 放进 td."""
        js = (ROOT / "dddb/public/static/public.js").read_text(
            encoding="utf-8")
        start = js.find("function buildTrackRow")
        end = js.find("\n  function ", start + 10)
        block = js[start:end if end > 0 else start + 4000]
        # 隐藏字段(track_disc)必须加进某个 td 里, 不能直接 append 到 tr
        self.assertIn("sortTd.appendChild(discHidden)", block,
                      "hidden 应加进排序列的 td")
        self.assertNotIn("tr.appendChild(hidden)", block,
                         "不能直接 append 到 tr")
        self.assertNotIn("tr.appendChild(discHidden)", block,
                         "不能直接 append 到 tr")


class TestTableVisibility(unittest.TestCase):
    """输入框要可见(不能是透明背景+透明边框)."""

    def test_input_has_visible_border(self) -> None:
        css = (ROOT / "dddb/public/static/public.css").read_text(
            encoding="utf-8")
        m = re.search(r'\.track-table input\[type="text"\]\s*\{([^}]*)\}',
                      css)
        self.assertIsNotNone(m, "找不到输入框样式")
        block = m.group(1)
        self.assertIn("border: 1px solid var(--line", block,
                      "输入框要有可见边框")
        self.assertNotIn("border: 1px solid transparent", block,
                         "不能是透明边框")
        self.assertIn("min-height", block, "要有最小高度")
        self.assertIn("background: var(--panel-2", block,
                      "要有可见背景")

    def test_table_layout_auto(self) -> None:
        css = (ROOT / "dddb/public/static/public.css").read_text(
            encoding="utf-8")
        m = re.search(r"\.track-table\s*\{([^}]*)\}", css)
        self.assertIsNotNone(m)
        self.assertIn("table-layout: auto", m.group(1),
                      "fixed 会让列挤在一起")
        self.assertNotIn("table-layout: fixed", m.group(1))


if __name__ == "__main__":
    unittest.main()