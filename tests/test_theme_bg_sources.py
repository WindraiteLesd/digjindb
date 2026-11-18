"""测试: 明亮主题对比度 / 背景图上传 / 来源外链清洗."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestLightThemeTopbar(unittest.TestCase):
    def setUp(self) -> None:
        self.css = (ROOT / "dddb/public/static/public.css").read_text(encoding="utf-8")

    def test_topbar_uses_variable(self) -> None:
        self.assertIn("--topbar-bg", self.css, "顶栏背景要用变量")
        # 顶栏不能再有写死的深色
        import re

        block = re.search(r"\.topbar\s*\{[^}]*\}", self.css)
        self.assertIsNotNone(block)
        self.assertIn("var(--topbar-bg", block.group(0))

    def test_light_theme_defines_topbar(self) -> None:
        import re

        block = re.search(r':root\[data-theme="light"\]\s*\{([^}]*)\}', self.css)
        self.assertIsNotNone(block)
        self.assertIn("--topbar-bg", block.group(1), "明亮主题要单独定义顶栏")

    def test_light_theme_text_contrast(self) -> None:
        import re

        block = re.search(r':root\[data-theme="light"\]\s*\{([^}]*)\}', self.css)
        values = block.group(1)
        text = re.search(r"--text:\s*#([0-9a-f]{6})", values, re.I)
        bg = re.search(r"--bg:\s*#([0-9a-f]{6})", values, re.I)
        self.assertIsNotNone(text)
        self.assertIsNotNone(bg)

        def lum(hexv: str) -> float:
            rgb = [int(hexv[i:i + 2], 16) / 255 for i in (0, 2, 4)]
            linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
                      for c in rgb]
            return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

        l1, l2 = lum(text.group(1)), lum(bg.group(1))
        ratio = (max(l1, l2) + 0.05) / (min(l1, l2) + 0.05)
        self.assertGreater(ratio, 10, f"明暗对比度只有 {ratio:.1f}, 太低")

    def test_no_dark_backgrounds_left(self) -> None:
        import re

        offenders = []
        for match in re.finditer(r"background(?:-color)?:\s*#([0-9a-f]{6})",
                                 self.css, re.I):
            hexv = match.group(1)
            r, g, b = int(hexv[0:2], 16), int(hexv[2:4], 16), int(hexv[4:6], 16)
            if (r + g + b) / 3 < 60:
                offenders.append(match.group(0))
        self.assertEqual(offenders, [], f"仍有写死深色底: {offenders[:4]}")


class TestBackgroundUpload(unittest.TestCase):
    """背景图必须是本地上传, 不能用外链."""

    def setUp(self) -> None:
        self.templates = (ROOT / "dddb/public/templates.py").read_text(encoding="utf-8")
        self.site = (ROOT / "dddb/public/site.py").read_text(encoding="utf-8")
        self.js = (ROOT / "dddb/public/static/public.js").read_text(encoding="utf-8")

    def test_no_url_input_for_background(self) -> None:
        self.assertNotIn('id="pref-bg-url"', self.templates,
                         "不该再有背景图 URL 输入框")
        self.assertNotIn('id="fab-bg-url"', self.templates)

    def test_has_upload_form(self) -> None:
        self.assertIn('action="/account/background"', self.templates)
        self.assertIn('enctype="multipart/form-data"', self.templates)
        self.assertIn('name="background"', self.templates)

    def test_backend_routes(self) -> None:
        self.assertIn('"background": "background"', self.site)
        self.assertIn("account_background", self.site)
        self.assertIn("def background_response", self.site)
        self.assertIn("def _do_account_background", self.site)

    def test_js_uses_local_source(self) -> None:
        self.assertIn("data-bg-src", self.js, "JS 要从本地属性取背景")
        self.assertNotIn('prefs.bg_image', self.js, "JS 不该再读外链 URL")


class TestSourceLinkScrubbing(unittest.TestCase):
    """上传/入库的数据不能带 VGMDB/BGMTV/DoujinStyle 痕迹."""

    def test_doujinstyle_in_hosts(self) -> None:
        from dddb.db import SOURCE_HOSTS

        joined = " ".join(SOURCE_HOSTS)
        self.assertIn("doujinstyle", joined, "SOURCE_HOSTS 必须包含 doujinstyle")
        self.assertIn("vgmdb", joined)
        self.assertIn("bgm.tv", joined)

    def test_scrub_album_sources(self) -> None:
        from dddb.db import _scrub_album_sources
        from dddb.models import Album

        album = Album.from_dict({
            "key": "t", "title": "T",
            "description": "DoujinStyle 提供的格式: FLAC 真正的简介",
            "urls": {"doujinstyle": "https://doujinstyle.com/?p=album&id=1",
                     "官网": "https://example.com/"},
            "source_ids": {"vgmdb": "1", "bangumi": "2"},
            "tracks": [{"title": "a", "source": "vgmdb"}],
            "cover_url": "https://lain.bgm.tv/x.jpg",
        })
        _scrub_album_sources(album)
        # 只清"对外展示"的两类: 描述里的来源说明 + 指向来源 CDN 的封面
        self.assertEqual(album.description, "真正的简介", "来源说明要去掉, 正文保留")
        self.assertEqual(album.cover_url, "", "指向来源 CDN 的封面要清")
        # urls 在本项目里是**来源标识映射**, 跨源合并/信任判定依赖它, 不能删
        self.assertEqual(album.urls.get("doujinstyle"),
                         "https://doujinstyle.com/?p=album&id=1",
                         "urls 是来源映射, 写库时不能动")
        self.assertEqual(album.source_ids, {"vgmdb": "1", "bangumi": "2"},
                         "内部来源编号保持原样")
        self.assertEqual(album.tracks[0].source, "vgmdb",
                         "曲目来源标记属内部数据")

    def test_scrub_notes_keeps_normal_text(self) -> None:
        from dddb.db import scrub_source_notes

        self.assertEqual(scrub_source_notes("DoujinStyle 提供的格式: FLAC"), "")
        self.assertEqual(scrub_source_notes("DoujinStyle 提供的格式: FLAC 简介"),
                         "简介", "同一行也要能拆开")
        self.assertEqual(scrub_source_notes("这是一段正常简介"), "这是一段正常简介")
        self.assertEqual(scrub_source_notes("这张碟由 DoujinStyle 社团制作"),
                         "这张碟由 DoujinStyle 社团制作", "正文里出现词不该删")

    def test_write_album_scrubs(self) -> None:
        """写入路径必须调用清洗(保证以后入库也干净)."""
        source = (ROOT / "dddb/db.py").read_text(encoding="utf-8")
        self.assertIn("_scrub_album_sources(album)", source)


if __name__ == "__main__":
    unittest.main()