"""会次详情页的图: 放大 + 不裁切.

用户截图: 会次页(.event-media)的图只有 72×72, 竖版海报被
``object-fit: cover`` 裁成中间一条 —— 表现为"显示不全而且太小"。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "dddb/public/static/public.css"


def _block(cls: str) -> str:
    css = CSS.read_text(encoding="utf-8")
    return css.split(cls + " {")[1].split("}")[0]


class TestEventMediaLogo(unittest.TestCase):
    """会次详情页的图要够大且完整显示。"""

    def test_width_enlarged(self) -> None:
        block = _block(".event-logo")
        w = int(re.search(r"width:\s*(\d+)px", block).group(1))
        self.assertGreaterEqual(w, 160, "会次图太窄(原 72)")

    def test_height_enlarged(self) -> None:
        block = _block(".event-logo")
        h = int(re.search(r"height:\s*(\d+)px", block).group(1))
        self.assertGreaterEqual(h, 220, "会次图太矮(原 72)")

    def test_no_crop(self) -> None:
        """必须 contain —— cover 会把竖版海报两侧裁掉。"""
        block = _block(".event-logo")
        self.assertIn("object-fit: contain", block,
                      "cover 会裁切海报(用户说'显示不全')")
        self.assertNotIn("object-fit: cover", block)

    def test_wrapper_matches(self) -> None:
        """外层容器尺寸要跟图片一致, 否则会被压回去。"""
        block = _block(".event-media-logo")
        w = int(re.search(r"width:\s*(\d+)px", block).group(1))
        h = int(re.search(r"height:\s*(\d+)px", block).group(1))
        self.assertGreaterEqual(w, 160)
        self.assertGreaterEqual(h, 220)

    def test_text_placeholder_bigger(self) -> None:
        """没有图时的首字占位也要跟着放大。"""
        block = _block(".event-logo-text")
        size = int(re.search(r"font-size:\s*(\d+)px", block).group(1))
        self.assertGreaterEqual(size, 40)

    def test_mobile_safer(self) -> None:
        """小屏不挤掉右侧链接(竖向排列, 图变矮)。"""
        css = CSS.read_text(encoding="utf-8")
        # 文件里有多个 @media 780px 块, 只看含 .event-media 的那块
        found = False
        for chunk in css.split("@media (max-width: 780px)")[1:]:
            block = chunk.split("\n}")[0]
            if ".event-media" in block:
                self.assertIn("flex-direction: column", block,
                              "小屏应竖向排列")
                found = True
                break
        self.assertTrue(found, "没找到 .event-media 的 780px 规则")



class TestTemplateUnchanged(unittest.TestCase):
    """模板不该写死尺寸(否则 CSS 改了也不生效)。"""

    def test_no_inline_size(self) -> None:
        src = (ROOT / "dddb/public/templates.py").read_text(encoding="utf-8")
        m = re.search(r'<img class="event-logo"[^>]*>', src)
        self.assertIsNotNone(m, "找不到 event-logo 的 img")
        self.assertNotIn("width=", m.group(0), "模板里不该写死 width")
        self.assertNotIn("height=", m.group(0), "模板里不该写死 height")


if __name__ == "__main__":
    unittest.main()
