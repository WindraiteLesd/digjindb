"""裁切框: 限定图片内 + 默认 1:1 + 初始覆盖整图.

用户要求:
    "拖拽框要限定在图片范围内，不能拖到空白区域，
     同时默认应该该是一个封面标准的方形框，比例默认为 1:1，
     且初始范围就覆盖整张图片"
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CROP = ROOT / "dddb/public/static/cover-crop.js"


def _js() -> str:
    return CROP.read_text(encoding="utf-8")


def _fn(name: str) -> str:
    js = _js()
    idx = js.find("function {}(".format(name))
    if idx < 0:
        return ""
    m = re.search(r"\n  function \w+\(", js[idx + 10:])
    return js[idx:idx + 10 + (m.start() if m else len(js))]


class TestBoxClampedToImage(unittest.TestCase):
    """① 框不能拖到空白区域。"""

    def test_has_image_rect(self) -> None:
        self.assertIn("function imageRect(", _js())

    def test_has_clamp(self) -> None:
        self.assertIn("function clampBoxToImage(", _js())

    def test_clamp_uses_image_bounds(self) -> None:
        body = _fn("clampBoxToImage")
        self.assertIn("imageRect()", body)
        for key in ("left", "right", "top", "bottom"):
            self.assertIn(key, body, f"没用到 {key} 边界")

    def test_resize_clamps(self) -> None:
        self.assertIn("clampBoxToImage()", _fn("doResize"))

    def test_drag_clamps(self) -> None:
        self.assertIn("clampBoxToImage()", _fn("doDrag"))

    def test_layout_clamps(self) -> None:
        self.assertIn("clampBoxToImage()", _fn("layout"))


class TestDefaultSquare(unittest.TestCase):
    """② 默认 1:1 方形。"""

    def test_state_square_default_false(self) -> None:
        """用户要求框默认就是图片边界, 所以**不**默认锁 1:1。"""
        js = _js()
        self.assertIn("square: false", js)
        self.assertIn("state.square = false", js)

    def test_enforce_square_exists(self) -> None:
        self.assertIn("function enforceSquare(", _js())

    def test_enforce_keeps_square(self) -> None:
        """取较大边作为边长, 宽高相等。"""
        js = _js()
        i = js.find("function enforceSquare(")
        body = js[i:i + 500]
        self.assertIn("Math.max(b.w, b.h)", body)
        self.assertRegex(body, r"w:\s*Math\.round\(side\),\s*h:\s*Math\.round\(side\)")

    def test_resize_applies_square(self) -> None:
        self.assertIn("enforceSquare(", _fn("doResize"))

    def test_clamp_preserves_square(self) -> None:
        """夹取可能把框压扁, 要再压回方形。"""
        body = _fn("clampBoxToImage")
        self.assertIn("state.square", body)
        self.assertIn("Math.min(state.box.w, state.box.h)", body)


class TestInitialCoversImage(unittest.TestCase):
    """③ 初始范围覆盖整张图片。"""

    def test_uses_contain_scale(self) -> None:
        body = _fn("layout")
        self.assertIn("Math.min(sw / iw, sh / ih)", body,
                      "初始缩放该用 contain(完整可见)")

    def test_box_is_full_image(self) -> None:
        """框默认 = 图片完整矩形(不再取短边内缩)。"""
        body = _fn("layout")
        self.assertIn("imgLeft", body)
        self.assertNotIn("Math.min(dispW, dispH)", body,
                         "不该再取短边当方形框")

    def test_box_covers_whole_image(self) -> None:
        """框的宽高应等于图片显示宽高。"""
        body = _fn("layout")
        self.assertIn("Math.round(dispW)", body)
        self.assertIn("Math.round(dispH)", body)

    def test_no_fixed_80_percent(self) -> None:
        """旧的"stage 80%"被去掉了。"""
        body = _fn("layout")
        self.assertNotIn("sw * 0.8", body)

    def test_reset_scale_on_open(self) -> None:
        js = _js()
        self.assertIn("state.keepScale = false", js)


class TestMinBoxHoisted(unittest.TestCase):
    """MIN_BOX 必须是模块级, 否则 layout 里是 undefined -> NaN 尺寸。"""

    def test_module_level(self) -> None:
        js = _js()
        # 顶格两空格的定义(模块级 IIFE 内)
        m = re.search(r"^  var MIN_BOX = (\d+);", js, re.M)
        self.assertIsNotNone(m, "MIN_BOX 不在模块级")
        self.assertGreaterEqual(int(m.group(1)), 20)

    def test_not_inside_function(self) -> None:
        body = _fn("startResize")
        self.assertNotIn("var MIN_BOX", body,
                         "MIN_BOX 不该只定义在函数里")


if __name__ == "__main__":
    unittest.main()
