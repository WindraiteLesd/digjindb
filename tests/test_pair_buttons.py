"""测试: 外链/词条的添加删除按钮.

历史 bug: `setupPairRows()` 定义在 public.js 里但**从未在 boot() 中调用**,
所以按钮完全没绑定事件 —— 点了没反应。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _js() -> str:
    return (ROOT / "dddb/public/static/public.js").read_text(encoding="utf-8")


def _boot_block() -> str:
    js = _js()
    idx = js.find("function boot(")
    depth = 0
    for i in range(idx, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[idx:i + 1]
    return ""


def _edit_html():
    from dddb.public import templates as T

    album = {
        "key": "d", "title": "T", "title_original": "T", "title_alt": "",
        "romaji": "", "circle": "", "artists": [],
        "credits": [{"role": "作曲", "name": "甲"}],
        "urls": [{"key": "circle_site", "url": "https://a.com/"}],
        "tags": [], "genres": [], "tracks": [], "date": "", "event": "",
        "catalog_no": "", "price": "", "hall": "", "booth": "",
        "description": "", "missing": [], "track_count": 0,
    }
    ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                        user={"id": 1, "username": "ed", "role": "editor"})
    return T.edit_album_page(ctx, album=album,
                             values=T.edit_values_from_album(album))


class TestPairRowsRegistered(unittest.TestCase):
    """所有 setup* 函数都必须被 boot() 调用 —— 否则功能静默失效."""

    def test_setup_pair_rows_defined(self) -> None:
        self.assertIn("function setupPairRows(", _js())

    def test_setup_pair_rows_called_in_boot(self) -> None:
        boot = _boot_block()
        self.assertIn("setupPairRows();", boot,
                      "setupPairRows 必须在 boot() 里注册, "
                      "否则增删按钮点了没反应")

    def test_all_setup_functions_registered(self) -> None:
        """兜底: 定义的每个 setup* 都要在 boot 里出现."""
        js = _js()
        defined = set(re.findall(r"function (setup\w+)\s*\(", js))
        self.assertGreater(len(defined), 5, "setup 函数太少, 可能解析错")
        boot = _boot_block()
        missing = sorted(fn for fn in defined if (fn + "()") not in boot)
        self.assertEqual(missing, [],
                         f"这些 setup 函数没有在 boot() 里注册: {missing}")

    def test_track_expand_registered(self) -> None:
        """曲目展开也曾漏注册."""
        self.assertIn("setupTrackExpand();", _boot_block())


class TestPairButtonsInHtml(unittest.TestCase):
    """模板必须渲染出成对的按钮与容器."""

    def setUp(self) -> None:
        self.html = _edit_html()

    def test_add_buttons_present(self) -> None:
        self.assertIn('data-pair-add="credit"', self.html)
        self.assertIn('data-pair-add="url"', self.html)

    def test_del_buttons_present(self) -> None:
        self.assertIn('data-pair-del="credit"', self.html)
        self.assertIn('data-pair-del="url"', self.html)

    def test_rows_container_present(self) -> None:
        rows = re.findall(r'<div class="rows">', self.html)
        self.assertGreaterEqual(len(rows), 2,
                                "词条和外链都要有 .rows 容器")

    def test_add_button_outside_rows(self) -> None:
        """添加按钮必须在 .rows 外(在 .pair-add 里), 否则会被当成一行数据."""
        for m in re.finditer(r'<div class="rows">([\s\S]*?)</div>\s*<div class="pair-add">',
                             self.html):
            self.assertNotIn("pair-add", m.group(1))

    def test_pair_add_wrapper_present(self) -> None:
        self.assertGreaterEqual(
            len(re.findall(r'<div class="pair-add">', self.html)), 2)

    def test_rows_carry_data_pair_row(self) -> None:
        """初始行必须带 data-pair-row(与 JS 新建的行一致).

        缺这个属性时, 提交前的空行清理逻辑按
        `.pair-row[data-pair-row="kind"]` 选不到初始行, filled/empties 统计全错。
        """
        credit = re.findall(r'data-pair-row="credit"', self.html)
        url = re.findall(r'data-pair-row="url"', self.html)
        self.assertGreater(len(credit), 0, "词条初始行缺 data-pair-row")
        self.assertGreater(len(url), 0, "外链初始行缺 data-pair-row")
        # 模板行与 JS 行属性必须同名, 否则选择器对不上
        js = _js()
        self.assertIn('row.setAttribute("data-pair-row", kind)', js)

    def test_buttons_are_type_button(self) -> None:
        """必须 type="button", 否则会提交表单."""
        for m in re.finditer(
                r'<button[^>]*data-pair-(?:add|del)="[^"]*"[^>]*>', self.html):
            self.assertIn('type="button"', m.group(0),
                          f"按钮缺少 type=button: {m.group(0)[:70]}")


class TestPairJsBehaviour(unittest.TestCase):
    """JS 行为约定."""

    def setUp(self) -> None:
        self.js = _js()

    def test_finds_rows_container(self) -> None:
        """添加时优先找 .rows 容器."""
        start = self.js.find("function setupPairRows(")
        end = self.js.find("\n  function ", start + 10)
        block = self.js[start:end if end > 0 else start + 3000]
        self.assertIn('closest(".rows")', block)

    def test_build_pair_row_sets_attr(self) -> None:
        self.assertIn('row.setAttribute("data-pair-row", kind)', self.js,
                      "新行要带 data-pair-row 才能被后续逻辑识别")

    def test_delete_removes_row(self) -> None:
        start = self.js.find("function setupPairRows(")
        end = self.js.find("\n  function ", start + 10)
        block = self.js[start:end if end > 0 else start + 3000]
        self.assertIn("removeChild", block)

    def test_uses_crawler_escape_attribute(self) -> None:
        """CSS 选择器里 data attribute 名不能写错."""
        self.assertIn('data-pair-add', self.js)
        self.assertIn('data-pair-del', self.js)

    def test_empty_row_signal_logic(self) -> None:
        """清空语义: 保留恰好一行空行作为"已清空"信号."""
        self.assertIn("keepAsSignal", self.js,
                      "要区分'没改动'和'清空了'")
        # 信号只保留一行
        self.assertIn("index === 0", self.js)


class TestClearSemanticsBackend(unittest.TestCase):
    """后端: 字段缺失=没改动, 字段存在但空=清空."""

    def test_missing_fields_keep_original(self) -> None:
        from dddb.public.site import parse_album_form

        patch, _ = parse_album_form({"title": ["T"],
                                     "title_original": ["T"]})
        self.assertNotIn("credits", patch)
        self.assertNotIn("urls", patch)

    def test_present_but_empty_clears(self) -> None:
        from dddb.public.site import parse_album_form

        patch, _ = parse_album_form({
            "title": ["T"], "title_original": ["T"],
            "credit_role": [""], "credit_name": [""],
            "url_key": [""], "url_value": [""],
        })
        self.assertEqual(patch.get("credits"), [])
        self.assertEqual(patch.get("urls"), {})

    def test_normal_values(self) -> None:
        from dddb.public.site import parse_album_form

        patch, _ = parse_album_form({
            "title": ["T"], "title_original": ["T"],
            "credit_role": ["作曲", ""], "credit_name": ["甲", ""],
            "url_key": ["x", ""], "url_value": ["https://x.com/", ""],
        })
        self.assertEqual(patch["credits"], [{"role": "作曲", "name": "甲"}])
        self.assertEqual(patch["urls"], {"x": "https://x.com/"})


if __name__ == "__main__":
    unittest.main()
