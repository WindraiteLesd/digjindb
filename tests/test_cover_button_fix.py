"""修: 按钮重复 + 裁切弹窗图不显示.

用户截图第二张: 封面区有**两个**按钮(「选择文件 未选择文件」+「上传封面」)。

根因: 原生 ``<input type="file" class="cover-file">`` 是**可见**的
(width:100%), 那一行「选择文件 / 未选择文件」就是它。我加的按钮又露了一个。

用户截图第一张: 裁切弹窗里**空白**, 图没显示。

根因: ``layout()`` 在图片 ``naturalWidth`` 还是 0 的时候跑了 ——
``iw = naturalWidth || 1`` 算出 0 宽高, 图就看不见。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "dddb/public/static/public.css"
CROP = ROOT / "dddb/public/static/cover-crop.js"
TPL = ROOT / "dddb/public/templates.py"


class TestNoDuplicateButton(unittest.TestCase):
    """原生 file input 必须隐藏, 只露按钮。"""

    def test_input_hidden_by_clip(self) -> None:
        css = CSS.read_text(encoding="utf-8")
        m = re.search(r"\.edit-cover-row \.cover-file \{([^}]*)\}", css)
        self.assertIsNotNone(m, "找不到 cover-file 规则")
        body = m.group(1)
        self.assertIn("clip:", body, "没用 clip 隐藏(否则两个按钮并排)")
        self.assertIn("width: 1px", body)

    def test_not_display_none(self) -> None:
        """display:none 的 input 在部分浏览器 click() 无效。"""
        css = CSS.read_text(encoding="utf-8")
        body = re.search(r"\.edit-cover-row \.cover-file \{([^}]*)\}",
                         css).group(1)
        self.assertNotIn("display: none", body,
                         "display:none 会让 click() 失效")

    def test_only_one_button_in_form(self) -> None:
        tpl = TPL.read_text(encoding="utf-8")
        start = tpl.find("data-cover-token=")
        self.assertGreater(start, 0)
        seg = tpl[start:start + 1200]
        self.assertEqual(seg.count('type="file"'), 1)
        self.assertEqual(seg.count("data-cover-upload"), 1,
                         "表单里出现多个上传按钮")


class TestCropModalShowsImage(unittest.TestCase):
    def test_waits_for_natural_size(self) -> None:
        js = CROP.read_text(encoding="utf-8")
        self.assertIn("naturalWidth", js)
        self.assertIn("waitSize", js, "没有等图片解码完再布局")

    def test_layout_guards_zero_size(self) -> None:
        js = CROP.read_text(encoding="utf-8")
        # layout 里不该再用 "|| 1" 那种把 0 当 1 的写法
        self.assertNotIn("naturalWidth || 1", js,
                         "用 1 顶替 0 会算出 0 宽高的图")
        self.assertIn("if (!iw || !ih)", js)

    def test_stage_size_guard(self) -> None:
        js = CROP.read_text(encoding="utf-8")
        self.assertIn("if (!sw || !sh)", js,
                      "stage 没尺寸时会算出错布局")

    def test_clears_previous_size(self) -> None:
        """打开新图前要清掉上一张的尺寸。"""
        js = CROP.read_text(encoding="utf-8")
        self.assertIn('refs.img.style.width = ""', js)

    def test_has_error_handler(self) -> None:
        js = CROP.read_text(encoding="utf-8")
        self.assertIn("onerror", js, "图片读不出来时要提示")

    def test_no_object_url_at_all(self) -> None:
        """改用 FileReader 后不再需要 createObjectURL(它撞 CSP)。"""
        js = CROP.read_text(encoding="utf-8")
        code = "\n".join(
            ln for ln in js.split("\n") if not ln.strip().startswith("//"))
        self.assertNotIn("createObjectURL(file)", code)
        self.assertIn("FileReader", code)


class TestCssUsesVariables(unittest.TestCase):
    """不许硬编码深色背景(浅色主题会崩)。"""

    def test_no_hardcoded_dark(self) -> None:
        css = CSS.read_text(encoding="utf-8")
        self.assertNotIn("background: #14120f", css)

    def test_crop_stage_uses_var(self) -> None:
        css = CSS.read_text(encoding="utf-8")
        i = css.find(".crop-stage {")
        self.assertGreater(i, 0)
        block = css[i:i + 320]
        self.assertIn("var(--", block, "裁切舞台该用主题变量")


class TestStillThreeEntries(unittest.TestCase):
    def test_cover_entries(self) -> None:
        """封面入口: 「上传封面」按钮 + 左侧大图。"""
        js = CROP.read_text(encoding="utf-8")
        self.assertGreaterEqual(js.count('pick("cover")'), 2)

    def test_photo_entry(self) -> None:
        js = CROP.read_text(encoding="utf-8")
        self.assertIn('pick("photo")', js)


if __name__ == "__main__":
    unittest.main()
