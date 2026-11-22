"""封面上传的两个前端 bug.

用户截图 1: 点了「上传封面」-> 提示 **"请先选择一张图片"**。
用户截图 2: 上传后左侧仍显示 **"无封面"**。

根因(都在前端):

    ① **两套上传逻辑抢同一个 input**
       ``setupCoverGallery`` 选完图**立刻自动上传**并把 ``fileInput.value``
       清空; 而 ``setupCoverUpload`` 的「上传封面」按钮随后读
       ``input.files[0]`` -> 已经是空 -> 报"请先选择一张图片"。

    ② **"无封面"占位是 div, 不是 img**
       ``thumbImg`` 选择器要求 ``[data-zoom-src]``, 占位 div 没有 ->
       ``thumbImg === null`` -> ``setPrimary()`` 直接 return ->
       左侧永远显示"无封面"(只有缩略图槽里多了张小图)。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "dddb/public/static/public.js"


def _js() -> str:
    return JS.read_text(encoding="utf-8")


def _fn(name: str) -> str:
    js = _js()
    idx = js.find("function {}(".format(name))
    if idx < 0:
        return ""
    m = re.search(r"\n  function \w+\(", js[idx + 10:])
    end = idx + 10 + (m.start() if m else len(js))
    return js[idx:end]


class TestNoDuplicateUpload(unittest.TestCase):
    """两个函数不能再抢同一个 input。"""

    def test_upload_btn_delegates_to_gallery(self) -> None:
        block = _fn("setupCoverUpload")
        self.assertTrue(block, "找不到 setupCoverUpload")
        self.assertIn("hasGallery", block,
                      "没判断是否存在多图槽位")
        self.assertIn("input.click()", block,
                      "「上传封面」应该打开选图框")

    def test_gallery_clears_input(self) -> None:
        """gallery 确实会清空 input —— 所以按钮不能依赖它。"""
        block = _fn("setupCoverGallery")
        self.assertIn('fileInput.value = ""', block)

    def test_gallery_sends_next(self) -> None:
        """gallery 上传要带 next, 否则成功后会跳详情页。"""
        block = _fn("setupCoverGallery")
        self.assertIn('body.append("next"', block)


class TestEmptyPlaceholderCanBecomeImage(unittest.TestCase):
    def test_selector_not_restricted(self) -> None:
        """thumbImg 不能只找 [data-zoom-src]。"""
        js = _js()
        self.assertNotIn(
            'querySelector(".edit-cover-img[data-zoom-src]")', js,
            "选择器排除了占位 div, 上传后不会更新")

    def test_setprimary_handles_div(self) -> None:
        block = _fn("setupCoverGallery")
        idx = block.find("function setPrimary(")
        self.assertGreater(idx, 0, "找不到 setPrimary")
        body = block[idx:idx + 1400]
        self.assertIn('tagName !== "IMG"', body,
                      "没处理\"占位是 div\"的情况")
        self.assertIn("createElement(\"img\")", body,
                      "没把占位 div 换成 img")

    def test_thumbimg_is_var(self) -> None:
        """thumbImg 要被重新赋值(换成新 img), 不能是 const。"""
        js = _js()
        block = _fn("setupCoverGallery")
        self.assertNotIn("const thumbImg", block)


class TestServerSideStillFine(unittest.TestCase):
    """服务端上传逻辑没被这几轮改动破坏。"""

    def test_upload_uses_unique_stem(self) -> None:
        src = (ROOT / "dddb/public/site.py").read_text(encoding="utf-8")
        self.assertIn("_cover_stem(key)", src)

    def test_cover_route_registered(self) -> None:
        src = (ROOT / "dddb/public/site.py").read_text(encoding="utf-8")
        self.assertIn('"album_cover"', src)


if __name__ == "__main__":
    unittest.main()
