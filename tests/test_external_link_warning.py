"""测试: 外链跳转警告."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _album(urls=None):
    return {
        "key": "k", "href": "/album/k", "title": "T", "title_original": "T",
        "title_alt": "", "romaji": "", "circle": "", "circle_href": "",
        "artists": [], "credits": [],
        "urls": urls if urls is not None else [
            {"name": "circle_site", "url": "https://a.com/",
             "href": "/link/k/1", "position": 1},
            {"name": "x", "url": "https://x.com/b",
             "href": "/link/k/2", "position": 2},
        ],
        "tags": [], "genres": [], "tracks": [], "date": "", "event": "",
        "catalog_no": "", "price": "", "hall": "", "booth": "",
        "description": "", "missing": [], "track_count": 0,
    }


def _detail(album=None):
    from dddb.public import templates as T

    ctx = T.SiteContext(csrf_token="t", is_editor=False, lang="zh")
    return T.album_detail_page(ctx, album=album or _album())


class TestExternalLinkWarning(unittest.TestCase):
    """外链点击要弹"跳转站外"确认框.

    public.js 的 setupExternalLinks 只监听 ``a[data-external]`` ——
    按钮上漏了这个属性就不会有警告(用户反馈"警告没了")。
    """

    def setUp(self) -> None:
        self.html = _detail()
        self.js = (ROOT / "dddb/public/static/public.js").read_text(
            encoding="utf-8")

    def test_link_pills_have_data_external(self) -> None:
        pills = re.findall(r'<a class="link-pill"[^>]*>', self.html)
        self.assertGreater(len(pills), 0, "没有外链按钮")
        for pill in pills:
            self.assertIn("data-external", pill,
                          f"外链按钮缺 data-external: {pill[:80]}")

    def test_js_listens_for_data_external(self) -> None:
        self.assertIn('a[data-external]', self.js)
        self.assertIn("function setupExternalLinks(", self.js)

    def test_js_registered_at_boot(self) -> None:
        self.assertIn("setupExternalLinks()", self.js)

    def test_warning_uses_confirm(self) -> None:
        start = self.js.find("function setupExternalLinks(")
        end = self.js.find("\n  function ", start + 10)
        block = self.js[start:end if end > 0 else start + 1500]
        self.assertIn("window.confirm", block, "要用 confirm 弹确认框")
        self.assertIn("preventDefault", block, "取消时要阻止跳转")

    def test_warning_mentions_leaving_site(self) -> None:
        start = self.js.find("function setupExternalLinks(")
        end = self.js.find("\n  function ", start + 10)
        block = self.js[start:end if end > 0 else start + 1500]
        self.assertTrue(
            "站外" in block or "离开本站" in block or "跳转" in block,
            "提示文案要说明跳转站外")

    def test_relay_path_handled(self) -> None:
        """href 是 /link/ 中转时, 提示不该直接打印内部路径."""
        start = self.js.find("function setupExternalLinks(")
        end = self.js.find("\n  function ", start + 10)
        block = self.js[start:end if end > 0 else start + 1500]
        self.assertIn("/link/", block, "要识别中转路径")

    def test_no_external_when_no_urls(self) -> None:
        html = _detail(_album(urls=[]))
        self.assertNotIn("data-external", html)
        self.assertIn("没有外链记录", html)


class TestExternalAttributePermutation(unittest.TestCase):
    """属性顺序变了也要生效(正则要能覆盖)."""

    def test_all_pills_covered(self) -> None:
        album = _album(urls=[
            {"name": f"k{i}", "url": f"https://x{i}.com/",
             "href": f"/link/k/{i}", "position": i}
            for i in range(1, 6)
        ])
        html = _detail(album)
        pills = re.findall(r'<a class="link-pill"[^>]*>', html)
        self.assertEqual(len(pills), 5)
        self.assertEqual(sum(1 for p in pills if "data-external" in p), 5)


if __name__ == "__main__":
    unittest.main()
