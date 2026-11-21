"""移动端曲目表行高.

用户截图反馈: 手机上曲目表每行太高, 10 只要滚两屏多。

原因: 每行 padding 9px + gap 2px + 曲名无限换行 + 艺人固定第二行。
修法: padding 5px、gap 0、曲名最多两行、艺人单行省略。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "dddb/public/static/public.css"


def _css() -> str:
    return CSS.read_text(encoding="utf-8")


def _mobile_track_block() -> str:
    """取 max-width:820px 里**真正定义曲目行**的那一块.

    注意: 文件里有两个 820px 媒体块, 两个都含 .tracklist 字样
    (前一个是别的东西带到了这个词), 必须用"有 tr.track-item 规则"
    来判断。
    """
    css = _css()
    for chunk in css.split("@media (max-width: 820px)")[1:]:
        if re.search(r"\.tracklist tr\.track-item\s*\{", chunk):
            return chunk
    return ""


class TestMobileTracklistCompact(unittest.TestCase):
    def test_padding_reduced(self) -> None:
        block = _mobile_track_block()
        m = re.search(r"\.tracklist tr\.track-item\s*\{([^}]*)\}", block)
        self.assertIsNotNone(m, "找不到行容器规则")
        body = m.group(1)
        pad = re.search(r"padding:\s*(\d+)px", body)
        self.assertIsNotNone(pad, "没有 padding")
        self.assertLessEqual(int(pad.group(1)), 6,
                             "上下 padding 还是太大(原 9px)")

    def test_gap_removed(self) -> None:
        block = _mobile_track_block()
        m = re.search(r"\.tracklist tr\.track-item\s*\{([^}]*)\}", block)
        gap = re.search(r"gap:\s*([^;]+);", m.group(1))
        self.assertIsNotNone(gap)
        self.assertTrue(gap.group(1).strip().startswith("0"),
                        "行内 gap 应归零")

    def test_line_height_set(self) -> None:
        block = _mobile_track_block()
        m = re.search(r"\.tracklist tr\.track-item\s*\{([^}]*)\}", block)
        self.assertIn("line-height", m.group(1),
                      "没设 line-height, 行高会随字体漂移")

    def test_title_clamped(self) -> None:
        """曲名最多两行 —— 无限换行是行高爆炸的主因。"""
        block = _mobile_track_block()
        self.assertIn("-webkit-line-clamp: 2", block,
                      "曲名没有行数限制")

    def test_artist_single_line(self) -> None:
        """艺人单行省略 —— 文件里同名选择器出现多次, 取并集判断。"""
        block = _mobile_track_block()
        bodies = re.findall(
            r"\.tracklist td\.track-artists\s*\{([^}]*)\}", block)
        self.assertTrue(bodies, "找不到艺人规则")
        merged = " ".join(bodies)
        self.assertIn("nowrap", merged, "艺人该单行省略, 不该换行")
        self.assertIn("text-overflow: ellipsis", merged)

    def test_num_and_dur_nowrap(self) -> None:
        block = _mobile_track_block()
        m = re.search(
            r"\.tracklist td\.num:first-child,\s*\n?\s*"
            r"\.tracklist td\.dur \{([^}]*)\}", block)
        self.assertIsNotNone(m, "找不到序号/时长规则")
        self.assertIn("nowrap", m.group(1))

    def test_fixed_num_column(self) -> None:
        """序号列定宽, 避免随内容抖动。"""
        block = _mobile_track_block()
        m = re.search(r"\.tracklist tr\.track-item\s*\{([^}]*)\}", block)
        cols = re.search(r"grid-template-columns:\s*([^;]+);", m.group(1))
        self.assertIsNotNone(cols)
        self.assertIn("24px", cols.group(1), "序号列没有定宽")

    def test_num_width_reset(self) -> None:
        """序号列必须重置 width —— 桌面 52/56px 在 grid 里会溢出重叠。"""
        block = _mobile_track_block()
        bodies = re.findall(
            r"\.tracklist td\.num[^{]*\{([^}]*)\}", block)
        self.assertTrue(bodies, "找不到序号规则")
        merged = " ".join(bodies)
        self.assertIn("width: auto", merged,
                      "没重置 width, 会和曲名重叠")
        self.assertIn("min-width: 0", merged)
        self.assertIn("max-width: none", merged)

    def test_dur_width_reset(self) -> None:
        block = _mobile_track_block()
        bodies = re.findall(
            r"\.tracklist td\.dur\s*\{([^}]*)\}", block)
        self.assertTrue(bodies, "找不到时长规则")
        merged = " ".join(bodies)
        self.assertIn("width: auto", merged, "时长列也要重置宽度")

    def test_num_left_aligned_on_mobile(self) -> None:
        """窄屏序号靠左(桌面是右对齐, 靠右会顶到曲名)。"""
        block = _mobile_track_block()
        bodies = re.findall(
            r"\.tracklist td\.num:first-child\s*\{([^}]*)\}", block)
        self.assertTrue(bodies, "找不到序号对齐规则")
        self.assertIn("text-align: left", " ".join(bodies))


class TestDesktopUnchanged(unittest.TestCase):
    """桌面端曲目表不受影响。"""

    def test_no_clamp_outside_mobile(self) -> None:
        """``-webkit-line-clamp`` 只能出现在手机媒体块内部。

        做法: 找出所有 clamp 出现的位置, 逐个确认它落在一个
        ``@media (max-width: 820px)`` 块里面。
        """
        css = _css()
        # 切出每个 820px 块的 [start, end)
        spans = []
        for m in re.finditer(r"@media \(max-width: 820px\) \{", css):
            start = m.end()
            depth = 1
            i = start
            while i < len(css) and depth:
                if css[i] == "{":
                    depth += 1
                elif css[i] == "}":
                    depth -= 1
                i += 1
            spans.append((m.start(), i))

        # 只看**曲目表**里的 clamp —— 专辑标题卡片也用 clamp 是正常的
        track_clamps = [
            m.start() for m in re.finditer(r"-webkit-line-clamp", css)
            if ".tracklist" in css[max(0, m.start() - 400):m.start()]
        ]
        self.assertTrue(track_clamps, "没找到曲目表的 clamp")
        for pos in track_clamps:
            inside = any(a <= pos < b for a, b in spans)
            self.assertTrue(
                inside,
                "曲目表的 clamp 出现在媒体块外(位置 {})".format(pos))


if __name__ == "__main__":
    unittest.main()
