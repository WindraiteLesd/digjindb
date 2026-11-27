"""用户 m19354: 曲目表刷新后正常一瞬间, 随即缩成只占左半边.

根因不在 CSS, 而在 ``public.js`` 的 ``setupTableResize()``: 它是桌面那套
"拖列宽" 的实现, 打开页面就把 ``table.style.width`` 设成**各列宽度之和**。
手机上曲目表早已被 CSS 改成 block/grid 卡片, 而那个和往往只有屏幕的一半,
于是首帧正常、脚本一跑就缩回左半边。

修法: 窄屏(<= 820px)直接不初始化列宽, 并且从宽屏缩回窄屏时把之前写下的
内联宽度清掉、交还给 CSS。

顺带修「社团索引搜索框小、筛选按钮过大」: ``.filters-inline`` 以前是
``260px auto``, 而 ``.field-actions button`` 有 ``flex: 1 1 auto`` 会撑满
剩下的整条 —— 改成输入框吃剩余宽度、按钮按内容宽度排。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "dddb" / "public" / "static" / "public.js").read_text(encoding="utf-8")
CSS = (ROOT / "dddb" / "public" / "static" / "public.css").read_text(encoding="utf-8")
TPL = (ROOT / "dddb" / "public" / "templates.py").read_text(encoding="utf-8")


def _function(name: str) -> str:
    """按花括号配平抠出 ``function name(...) {...}`` 的函数体."""
    start = JS.find("function " + name + "(")
    assert start >= 0, name
    depth = 0
    index = JS.find("{", start)
    begin = index
    while index < len(JS):
        char = JS[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return JS[begin:index + 1]
        index += 1
    raise AssertionError(name)


class TestTableResizeSkippedOnNarrowScreens(unittest.TestCase):
    def test_narrow_helper_exists(self) -> None:
        self.assertIn("function isNarrowTable()", JS)
        helper = _function("isNarrowTable")
        self.assertIn("max-width: 820px", helper)
        self.assertIn("matchMedia", helper)

    def test_setup_bails_out_when_narrow(self) -> None:
        body = _function("setupTableResize")
        # 必须在拿表格之前就返回, 否则内联宽度已经写下去了
        head = body[: body.find("querySelectorAll")]
        self.assertIn("isNarrowTable()", head)
        self.assertIn("return", head)

    def test_resize_listener_clears_inline_width_on_narrow(self) -> None:
        body = _function("setupTableResize")
        self.assertIn("table.style.width = \"\"", body)
        self.assertIn("table.style.tableLayout = \"\"", body)
        self.assertIn("cell.style.width = \"\"", body)


class TestCirclesFilterProportions(unittest.TestCase):
    def test_search_box_takes_the_rest(self) -> None:
        rule = re.search(r"\.filters-inline \{([^}]*)\}", CSS)
        self.assertIsNotNone(rule)
        body = rule.group(1)
        self.assertIn("minmax(0, 1fr)", body)
        self.assertNotIn("260px auto", body)

    def test_button_no_longer_stretches(self) -> None:
        rule = re.search(r"\.filters-inline \.field-actions button \{([^}]*)\}", CSS)
        self.assertIsNotNone(rule, "要有 .filters-inline 的按钮宽度覆盖")
        self.assertIn("flex: 0 0 auto", rule.group(1))

    def test_reset_link_also_sized(self) -> None:
        self.assertIn(".filters-inline .field-actions .btn-ghost", CSS)


class TestVersionsBumped(unittest.TestCase):
    def _version(self, name: str) -> int:
        match = re.search(re.escape(name) + r"\?v=(\d+)", TPL)
        self.assertIsNotNone(match, name)
        return int(match.group(1))

    def test_versions(self) -> None:
        self.assertGreaterEqual(self._version("public.css"), 87)
        self.assertGreaterEqual(self._version("public.js"), 76)


if __name__ == "__main__":
    unittest.main()
