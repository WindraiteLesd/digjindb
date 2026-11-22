"""裁切框自由调整(四角可拖动).

用户要求: "调整范围为自由范围，不要固定"

原状: ``crop-frame`` 上四个 ``.crop-handle`` 只设了 ``data-dir``,
**没绑任何事件** —— 框大小固定, 只能整体拖动。

现在:
    * 四角各自绑定 pointerdown -> 自由改 state.box 的 x/y/w/h
    * 最小边长 40px(拖过头卡住, 不翻转)
    * 不超出 stage
    * 整框拖动不抢占角上的拖动
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CROP = ROOT / "dddb/public/static/cover-crop.js"
CSS = ROOT / "dddb/public/static/public.css"


def _js() -> str:
    return CROP.read_text(encoding="utf-8")


class TestHandlesBound(unittest.TestCase):
    def test_four_handles_exist(self) -> None:
        js = _js()
        for d in ("nw", "ne", "sw", "se"):
            self.assertIn('"{}"'.format(d), js, f"缺 {d} 角")
        self.assertIn("crop-handle", js)

    def test_handles_have_listeners(self) -> None:
        """四角必须绑 mousedown / touchstart。"""
        js = _js()
        idx = js.find("querySelectorAll(\".crop-handle\")")
        self.assertGreater(idx, 0, "没遍历 handle")
        window = js[idx:idx + 400]
        self.assertIn("mousedown", window)
        self.assertIn("touchstart", window)
        self.assertIn("startResize", window)

    def test_start_resize_defined(self) -> None:
        js = _js()
        for fn in ("startResize", "doResize", "endResize"):
            self.assertIn("function {}(".format(fn), js, f"缺 {fn}")


class TestResizeLogic(unittest.TestCase):
    def test_four_directions(self) -> None:
        """四个方向都要处理(x/y/w/h)。"""
        js = _js()
        body = js[js.find("function doResize("):]
        body = body[:body.find("\n  function ", 10)]
        self.assertIn('indexOf("w")', body)
        self.assertIn('indexOf("e")', body)
        self.assertIn('indexOf("n")', body)
        self.assertIn('indexOf("s")', body)

    def test_min_size(self) -> None:
        js = _js()
        self.assertIn("MIN_BOX", js)
        self.assertIn("if (w < MIN_BOX)", js)
        self.assertIn("if (h < MIN_BOX)", js)

    def test_clamped_to_stage(self) -> None:
        """不能拖出 stage(否则裁出来是空白)。"""
        js = _js()
        body = js[js.find("function doResize("):]
        body = body[:body.find("\n  function ", 10)]
        self.assertIn("stage.clientWidth", body)
        self.assertIn("stage.clientHeight", body)

    def test_repaints_frame(self) -> None:
        js = _js()
        body = js[js.find("function doResize("):]
        body = body[:body.find("\n  function ", 10)]
        self.assertIn("paintFrame()", body)

    def test_apply_transform_after_resize(self) -> None:
        """框变了, 图片可拖范围也要重算。"""
        js = _js()
        body = js[js.find("function doResize("):]
        body = body[:body.find("\n  function ", 10)]
        self.assertIn("applyTransform()", body)


class TestDragDoesNotStealHandle(unittest.TestCase):
    def test_startdrag_skips_handles(self) -> None:
        js = _js()
        body = js[js.find("function startDrag("):]
        body = body[:body.find("\n  function ", 10)]
        self.assertIn("crop-handle", body,
                      "整框拖动会抢走角的拖动")

    def test_resize_stops_propagation(self) -> None:
        js = _js()
        body = js[js.find("function startResize("):]
        body = body[:body.find("\n  function ", 10)]
        self.assertIn("stopPropagation", body)


class TestHandleCss(unittest.TestCase):
    def test_handles_clickable(self) -> None:
        css = CSS.read_text(encoding="utf-8")
        block = css.split(".crop-handle {")[1].split("}")[0]
        self.assertIn("pointer-events: auto", block)
        self.assertIn("z-index", block, "handle 要在框之上")

    def test_cursor_per_corner(self) -> None:
        css = CSS.read_text(encoding="utf-8")
        self.assertIn("nwse-resize", css)
        self.assertIn("nesw-resize", css)

    def test_handles_inside_frame(self) -> None:
        """handle 不能完全落在框外(stage 有 overflow:hidden 会裁掉)。"""
        css = CSS.read_text(encoding="utf-8")
        for corner in ("nw", "ne", "sw", "se"):
            m = re.search(r"\.crop-{}\s*\{{([^}}]*)\}}".format(corner), css)
            self.assertIsNotNone(m, f"缺 .crop-{corner}")
            body = m.group(1)
            # -5px 是半嵌入, -7px 及更大会被裁
            nums = [int(x) for x in re.findall(r"-(\d+)px", body)]
            for n in nums:
                self.assertLessEqual(n, 5,
                                     f".crop-{corner} 偏移 {n}px 会被裁掉")


class TestCropStillFree(unittest.TestCase):
    def test_box_not_fixed_ratio(self) -> None:
        """layout 初始框是 80% 的 stage, 但拖动后可任意比例。"""
        js = _js()
        self.assertIn("state.box = {", js)
        # 不该有强制比例的代码
        self.assertNotIn("state.box.w = state.box.h", js)


if __name__ == "__main__":
    unittest.main()
