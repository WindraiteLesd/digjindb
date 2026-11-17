"""测试: 只有前三张卡片左右分栏, 制作人员和外链必须整行(上下排列).

历史问题: 用户只要求还原「封面&标题 / 出品信息 / 分类」的左右布局,
          结果连带把「制作人员 / 外链」也改成了左右并排。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _css() -> str:
    return (ROOT / "dddb/public/static/public.css").read_text(encoding="utf-8")


def _position(name: str) -> str:
    m = re.search(r"\.edit-card-" + name + r"\s*\{([^}]*)\}", _css())
    return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""


class TestCardRowLayout(unittest.TestCase):
    """版面: 前三张左右分栏, 其余整行。"""

    def test_cover_left_column(self) -> None:
        body = _position("cover")
        self.assertIn("grid-column: 1;", body)
        self.assertNotIn("1 / -1", body)

    def test_category_left_column(self) -> None:
        body = _position("category")
        self.assertIn("grid-column: 1;", body)
        self.assertNotIn("1 / -1", body)

    def test_info_right_column_spans_two_rows(self) -> None:
        body = _position("info")
        self.assertIn("grid-column: 2;", body)
        self.assertIn("grid-row: 1 / span 2", body)

    def test_description_full_row(self) -> None:
        self.assertIn("1 / -1", _position("description"))

    def test_credits_full_row(self) -> None:
        """制作人员必须**整行**(不是左右并排)。"""
        body = _position("credits")
        self.assertIn("grid-column: 1 / -1", body,
                      "制作人员要整行, 不能只占左列")
        self.assertNotIn("grid-column: 1;", body)

    def test_links_full_row(self) -> None:
        """外链必须**整行**(不是左右并排)。"""
        body = _position("links")
        self.assertIn("grid-column: 1 / -1", body,
                      "外链要整行, 不能只占右列")
        self.assertNotIn("grid-column: 2;", body)

    def test_credits_before_links(self) -> None:
        """上下顺序: 制作人员在上, 外链在下。"""
        credits_row = re.search(r"grid-row: (\d+)", _position("credits"))
        links_row = re.search(r"grid-row: (\d+)", _position("links"))
        self.assertIsNotNone(credits_row)
        self.assertIsNotNone(links_row)
        self.assertLess(int(credits_row.group(1)),
                        int(links_row.group(1)))

    def test_three_cards_share_two_rows(self) -> None:
        """前三张卡片总共占 2 行(封面+分类叠在左, 出品信息跨右)。"""
        rows = set()
        for name in ("cover", "category", "info"):
            for m in re.finditer(r"grid-row: (\d+)", _position(name)):
                rows.add(int(m.group(1)))
        self.assertEqual(rows, {1, 2})


class TestGridContainer(unittest.TestCase):
    def test_two_columns(self) -> None:
        m = re.search(r"\.edit-cards \{([^}]*)\}", _css())
        self.assertIsNotNone(m)
        self.assertIn("grid-template-columns: repeat(2", m.group(1))

    def test_equal_height(self) -> None:
        m = re.search(r"\.edit-cards \{([^}]*)\}", _css())
        self.assertIn("align-items: stretch", m.group(1))


class TestMobileStack(unittest.TestCase):
    def test_single_column_on_narrow(self) -> None:
        css = _css()
        self.assertRegex(
            css,
            r"@media \(max-width: 980px\)[\s\S]{0,900}?"
            r"\.edit-cards \{ grid-template-columns: minmax\(0, 1fr\)")


if __name__ == "__main__":
    unittest.main()
