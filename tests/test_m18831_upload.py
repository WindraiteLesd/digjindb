"""用户 m18831: 手机端曲目表 + 封面 2MB 阈值.

两条诉求:
1. 「手机页面曲目表没生效」—— 上一轮把 tr 改成 block 之后, 作者样式的
   display:block 盖掉了 hidden 属性自带的 display:none, 于是每首歌的补充
   信息行在手机上一直显示、又撑不开, 看着就像规则没生效。
2. 「大部分导出的封面都超过 2M 了」—— 单图上限从 2MB 提到 8MB, 并且前端
   对超过 1.2MB 的图先压一次再传(服务端是纯标准库, 压不了图)。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "dddb/public/static/public.css"
JS = ROOT / "dddb/public/static/public.js"
CROP_JS = ROOT / "dddb/public/static/cover-crop.js"
TPL = ROOT / "dddb/public/templates.py"
SITE = ROOT / "dddb/public/site.py"


def _mobile_block() -> str:
    """取 max-width:820px 里真正定义曲目行的那一块."""
    css = CSS.read_text(encoding="utf-8")
    for chunk in css.split("@media (max-width: 820px)")[1:]:
        if re.search(r"\.tracklist tr\.track-item\s*\{", chunk):
            return chunk
    return ""


class TestHiddenExtraRowStaysHidden(unittest.TestCase):
    """展开行默认 hidden, 作者样式不能把它顶开."""

    def test_hidden_rule_present_in_mobile_block(self) -> None:
        block = _mobile_block()
        self.assertTrue(block, "找不到曲目表的 820px 块")
        self.assertIn("tr.track-extra-row[hidden]", block,
                      "缺少把 hidden 重新按住的规则")

    def test_hidden_rule_wins(self) -> None:
        block = _mobile_block()
        rule = re.search(
            r"\.tracklist tr\.track-extra-row\[hidden\]\s*\{([^}]*)\}", block)
        self.assertIsNotNone(rule, "找不到 [hidden] 规则体")
        self.assertIn("display: none", rule.group(1))

    def test_still_block_when_visible(self) -> None:
        """展开之后仍要是块级(用户 m18302 的修法不能被打回)."""
        block = _mobile_block()
        rule = re.search(
            r"\.tracklist tr\.track-extra-row\s*\{([^}]*)\}", block)
        self.assertIsNotNone(rule)
        self.assertIn("display: block", rule.group(1))

    def test_extra_items_stack_vertically(self) -> None:
        """窄屏里补充信息竖排, 长值不会被挤成一条竖排单字."""
        block = _mobile_block()
        self.assertIn("flex-direction: column", block,
                      "补充信息的 flex 容器该竖排")
        self.assertRegex(block, r"\.track-extra-item\s*\{[^}]*display:\s*flex")


class TestImageLimitRaised(unittest.TestCase):
    def test_site_constants(self) -> None:
        from dddb.public.site import MAX_BODY, MAX_IMAGE_BODY

        self.assertGreaterEqual(MAX_IMAGE_BODY, 8 * 1024 * 1024,
                                "单图上限应提到 8MB")
        self.assertGreater(MAX_BODY, MAX_IMAGE_BODY,
                           "请求体上限必须大于单图上限")

    def test_photos_constant(self) -> None:
        from dddb.public.photos import MAX_PHOTO_BYTES

        self.assertGreaterEqual(MAX_PHOTO_BYTES, 8 * 1024 * 1024,
                                "附图上限也该是 8MB")

    def test_no_hardcoded_two_megabytes_left(self) -> None:
        src = SITE.read_text(encoding="utf-8")
        self.assertNotIn("len(blob) > 2 * 1024 * 1024", src)
        self.assertNotIn("len(data) > 2 * 1024 * 1024", src)

    def test_error_message_updated(self) -> None:
        src = SITE.read_text(encoding="utf-8")
        self.assertIn("上限 20MB", src)
        self.assertIn("压缩到 8MB 以内", src)


class TestFrontendCompression(unittest.TestCase):
    def test_upload_limit_constant(self) -> None:
        js = JS.read_text(encoding="utf-8")
        self.assertIn("var MAX_UPLOAD_BYTES = 8 * 1024 * 1024;", js)

    def test_no_two_megabyte_gate_left(self) -> None:
        js = JS.read_text(encoding="utf-8")
        self.assertNotIn("file.size > 2 * 1024 * 1024", js)

    def test_compress_helper_exists(self) -> None:
        js = JS.read_text(encoding="utf-8")
        self.assertIn("function compressImageFile(", js)
        self.assertIn("function maybeCompress(", js)
        self.assertIn("canvas.toBlob", js)
        self.assertIn('"image/jpeg"', js)

    def test_upload_path_compresses_first(self) -> None:
        js = JS.read_text(encoding="utf-8")
        idx = js.index("maybeCompress(file).then(")
        window = js[idx:idx + 700]
        self.assertIn("FormData", window)
        self.assertIn('body.append("cover"', window)

    def test_crop_limit_raised(self) -> None:
        js = CROP_JS.read_text(encoding="utf-8")
        self.assertIn("8 * 1024 * 1024", js)
        self.assertNotIn("4 * 1024 * 1024", js)


class TestVersionsBumped(unittest.TestCase):
    def test_assets(self) -> None:
        tpl = TPL.read_text(encoding="utf-8")

        def ver(name: str) -> int:
            m = re.search(re.escape(name) + r"\?v=(\d+)", tpl)
            self.assertIsNotNone(m, "找不到 " + name + " 的版本号")
            return int(m.group(1))

        self.assertGreaterEqual(ver("public.css"), 86)
        self.assertGreaterEqual(ver("public.js"), 75)
        self.assertGreaterEqual(ver("cover-crop.js"), 9)


if __name__ == "__main__":
    unittest.main()
