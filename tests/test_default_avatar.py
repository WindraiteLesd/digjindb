"""测试: 未设置头像的用户要有默认头像(而不是 404)。"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestDefaultAvatarSvg(unittest.TestCase):
    """默认头像是一张程序生成的 SVG。"""

    def setUp(self) -> None:
        from dddb.public.site import default_avatar_svg

        self.make = default_avatar_svg

    def test_returns_svg_bytes(self) -> None:
        data = self.make(42, "A")
        self.assertIsInstance(data, bytes)
        self.assertTrue(data.startswith(b"<?xml"))
        self.assertIn(b"<svg", data)
        self.assertIn(b"</svg>", data)

    def test_has_letter(self) -> None:
        data = self.make(42, "W").decode()
        self.assertIn(">W<", data)

    def test_letter_uppercased(self) -> None:
        data = self.make(42, "w").decode()
        self.assertIn(">W<", data)

    def test_cjk_letter(self) -> None:
        data = self.make(42, "高").decode()
        self.assertIn(">高<", data)

    def test_no_letter_uses_silhouette(self) -> None:
        """没有首字母时画剪影, 而不是空白。"""
        data = self.make(42, "").decode()
        self.assertIn("<circle", data)
        self.assertIn("<path", data)

    def test_same_seed_same_output(self) -> None:
        """同一用户永远同一个头像(稳定)。"""
        self.assertEqual(self.make(7, "D"), self.make(7, "D"))

    def test_different_seed_different_color(self) -> None:
        """不同用户配色不同(可区分)。"""
        a = self.make(7, "D").decode()
        b = self.make(8, "D").decode()
        self.assertNotEqual(a, b, "不同用户应有不同配色")

    def test_palette_coverage(self) -> None:
        """配色应有足够多样性(不该所有用户一个色)。"""
        colors = set()
        for seed in range(20):
            data = self.make(seed, "A").decode()
            match = re.search(r'<rect[^>]*fill="(#[0-9a-fA-F]{6})"', data)
            if match:
                colors.add(match.group(1))
        self.assertGreaterEqual(len(colors), 5,
                                f"配色太单一: {len(colors)} 种")

    def test_square_canvas(self) -> None:
        """128x128 正方形, 圆形由 CSS border-radius 处理。"""
        data = self.make(1, "A").decode()
        self.assertIn('width="128"', data)
        self.assertIn('height="128"', data)
        self.assertIn('viewBox="0 0 128 128"', data)

    def test_letter_is_escaped(self) -> None:
        """首字母里的特殊字符要转义(防 SVG 注入)。"""
        data = self.make(1, "<").decode()
        self.assertNotIn("><<", data)
        self.assertIn("&lt;", data)


class TestAvatarResponse(unittest.TestCase):
    """`/avatar/<uid>` 在没有上传头像时也要返回图片。"""

    def test_no_404_when_missing(self) -> None:
        import inspect

        from dddb.public.site import PublicSite

        source = inspect.getsource(PublicSite.avatar_response)
        self.assertNotIn("status=404", source,
                         "没有头像时不该再返回 404")
        self.assertIn("default_avatar_svg", source,
                      "应回落到默认头像")

    def test_returns_svg_content_type(self) -> None:
        import inspect

        from dddb.public.site import PublicSite

        source = inspect.getsource(PublicSite.avatar_response)
        self.assertIn("image/svg+xml", source)

    def test_has_cache_control(self) -> None:
        import inspect

        from dddb.public.site import PublicSite

        source = inspect.getsource(PublicSite.avatar_response)
        self.assertIn("Cache-Control", source)

    def test_uploaded_avatar_still_wins(self) -> None:
        """有上传头像时仍优先发用户的图。"""
        import inspect

        from dddb.public.site import PublicSite

        source = inspect.getsource(PublicSite.avatar_response)
        # avatar_bytes 必须在前, 默认头像在后
        upload_pos = source.find("avatar_bytes")
        default_pos = source.find("default_avatar_svg")
        self.assertGreater(upload_pos, 0)
        self.assertGreater(default_pos, 0)
        self.assertLess(upload_pos, default_pos,
                        "要先尝试用户头像, 取不到再用默认")


class TestAvatarTemplates(unittest.TestCase):
    """模板里所有头像都走 /avatar/<uid>(这样默认头像能覆盖到)。"""

    def test_all_avatar_imgs_use_avatar_route(self) -> None:
        source = (ROOT / "dddb/public/templates.py").read_text(encoding="utf-8")
        tags = re.findall(r'<img[^>]*class="[^"]*avatar[^"]*"[^>]*>', source)
        self.assertGreater(len(tags), 0, "找不到头像 img")
        for tag in tags:
            src = re.search(r'src="([^"]*)"', tag)
            if not src:
                continue
            # 允许 /avatar/ 或已解析的 image 变量
            self.assertTrue(
                "/avatar/" in src.group(1) or "{" in src.group(1),
                f"头像 img 没走 /avatar: {tag[:100]}")


class TestNoTestComments(unittest.TestCase):
    """代码里不该再留着测试评论的探针标记(防止以后再写进生产库)。"""

    def test_no_probe_marker_in_source(self) -> None:
        """源码里不该出现 probe-comment 这类探针字符串。"""
        bad = []
        for path in (ROOT / "dddb").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "probe-comment" in text:
                bad.append(path.name)
        self.assertEqual(bad, [], f"源码里还有探针标记: {bad}")


if __name__ == "__main__":
    unittest.main()