"""测试: 手机版曲目表卡片式布局."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _mobile_block() -> str:
    css = (ROOT / "dnh/public/static/public.css").read_text(encoding="utf-8")
    for m in re.finditer(r"@media \(max-width: 860px\) \{", css):
        start = m.start()
        depth = 0
        end = start
        for i in range(m.end() - 1, len(css)):
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        block = css[start:end]
        # 取含卡片布局的那个(有 tr.track-row 且 display:block)
        if "tr.track-row" in block:
            return block
    return ""


class TestMobileCardLayout(unittest.TestCase):
    """手机版(≤860px)每字段一行, 不需要横向滚动."""

    def setUp(self) -> None:
        self.block = _mobile_block()
        self.css = (ROOT / "dnh/public/static/public.css").read_text(
            encoding="utf-8")

    def test_block_exists(self) -> None:
        self.assertTrue(self.block, "找不到手机版卡片布局块")

    def test_table_becomes_block(self) -> None:
        self.assertRegex(
            self.block,
            r"\.track-table \{[^}]*display: block;",
            "表格要改成 block")
        self.assertIn("min-width: 0", self.block,
                      "不能保留 880px 最小宽度")
        self.assertIn("width: 100%", self.block)

    def test_no_horizontal_scroll(self) -> None:
        """容器不能强制横向滚动."""
        self.assertRegex(
            self.block,
            r"\.track-table-wrap \{[^}]*overflow-x: visible;",
            "手机版不该横向滚动")

    def test_row_is_card(self) -> None:
        self.assertRegex(
            self.block,
            r"tr\.track-row \{[^}]*display: block;",
            "每行要变成卡片")
        self.assertIn("border-radius", self.block)
        self.assertIn("border: 1px solid", self.block)

    def test_cell_is_block_row(self) -> None:
        self.assertRegex(
            self.block,
            r"\.track-table td \{[^}]*display: block;",
            "每个单元格独占一行")

    def test_cell_width_overridden(self) -> None:
        """全局列宽(62/78/90 + min-width 200)必须被压掉."""
        self.assertIn("width: auto !important", self.block)
        self.assertIn("min-width: 0 !important", self.block)

    def test_shows_data_label(self) -> None:
        """字段名显示在输入框上方."""
        self.assertIn("attr(data-label)", self.block)

    def test_input_readable(self) -> None:
        """手机输入框字号不能太小."""
        m = re.search(r"\.track-table input\[type=\"text\"\] \{([^}]*)\}",
                      self.block)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("font-size: 14px", body, "12px 在手机上太小")
        self.assertIn("min-height: 36px", body)

    def test_index_column_narrow(self) -> None:
        """音轨号不用占满整行."""
        self.assertIn("trk-col-idx", self.block)
        self.assertIn("96px", self.block)

    def test_ops_and_sort_inline(self) -> None:
        """操作/排序按钮并排, 不各占一行."""
        self.assertRegex(
            self.block,
            r"td\.trk-col-ops[\s\S]{0,400}?display: inline-block")

    def test_no_narrow_column_widths_left(self) -> None:
        """不再用 86/100/116 那种"放宽一点"的方案."""
        self.assertNotIn("width: 86px; min-width: 86px", self.block)
        self.assertNotIn("min-width: 880px", self.block)

    def test_desktop_table_unchanged(self) -> None:
        """桌面版仍然是普通表格(不能在全局改坏)."""
        # 全局 .track-table 不能是 block
        m = re.search(r"\n\.track-table \{([^}]*)\}", self.css)
        self.assertIsNotNone(m)
        self.assertNotIn("display: block", m.group(1),
                         "桌面版表格要保持 table 布局")
        self.assertIn("table-layout: auto", m.group(1))


if __name__ == "__main__":
    unittest.main()
