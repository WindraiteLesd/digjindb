"""外链解禁: 不再有域名黑名单, 只保留安全校验.

用户要求: "解禁数据来源网站的显示" + "删除禁止跳转的外链的相关代码"
"""

from __future__ import annotations

import unittest

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestNoHostBlacklist(unittest.TestCase):
    """任何站点都能显示和跳转。"""

    def test_source_sites_allowed(self) -> None:
        from dddb.public.data import outbound_link_reject_reason

        for url in (
            "https://某站点.net/album/1",
            "https://某站点/subject/1",
            "https://某站点.tv/subject/1",
            "https://chii.in/subject/1",
            "https://www.某站点.info/x",
            "https://某站点.com/?p=1",
            "https://dizzylab.net/album/1",
            "https://discogs.com/release/1",
            "https://touhoudb.com/A/1",
            "https://thwiki.cc/x",
            "https://example.com/x",
        ):
            self.assertEqual(outbound_link_reject_reason(url), "",
                             f"{url} 应该允许显示")

    def test_subdomains_allowed(self) -> None:
        from dddb.public.data import outbound_link_reject_reason

        for url in ("https://lain.某站点/pic/cover/x.jpg",
                    "https://m.某站点.com/x",
                    "https://www.某站点.net/album/1"):
            self.assertEqual(outbound_link_reject_reason(url), "")

    def test_security_still_enforced(self) -> None:
        """安全校验还在: 这是 XSS 防护, 不是站点政策。"""
        from dddb.public.data import outbound_link_reject_reason

        for url in ("javascript:alert(1)", "data:text/html,x",
                    "vbscript:x", "file:///etc/passwd", "not-a-url",
                    ""):
            self.assertNotEqual(outbound_link_reject_reason(url), "",
                                f"{url} 应该被拒")

    def test_blocked_link_host_always_empty(self) -> None:
        from dddb.public.data import blocked_link_host

        for url in ("https://某站点.net/album/1", "https://example.com/"):
            self.assertEqual(blocked_link_host(url), "")


class TestNoPolicyCode(unittest.TestCase):
    """代码层面: 黑名单名单本身不该再存在。"""

    def test_no_linkpolicy_module(self) -> None:
        """上游不带 linkpolicy; 主仓里它也不再被公开端导入。"""
        data_py = (ROOT / "dddb/public/data.py").read_text(encoding="utf-8")
        self.assertNotIn("from ..linkpolicy import", data_py,
                         "data.py 不该再导入黑名单")
        self.assertNotIn("BLOCKED_LINK_HOSTS", data_py)

    def test_embed_does_not_block_hosts(self) -> None:
        embed = (ROOT / "dddb/public/embed.py").read_text(encoding="utf-8")
        self.assertNotIn("is_blocked_host", embed,
                         "embed 不该再有站点黑名单判定")

    def test_db_import_is_tolerant(self) -> None:
        """db.py 对 netease 的导入必须容错(上游没有 netease)。"""
        db = (ROOT / "dddb/db.py").read_text(encoding="utf-8")
        self.assertIn("except ImportError", db)
        self.assertIn('MANUAL_METHOD = "manual"', db)


class TestLinksRendered(unittest.TestCase):
    """详情页把所有外链都渲染出来。"""

    def test_all_links_shown(self) -> None:
        from dddb.public import templates as T

        ctx = T.SiteContext(csrf_token="t", lang="zh")
        html = T.album_detail_page(ctx, album={
            "key": "d", "title": "T", "title_original": "T",
            "urls": [
                {"name": "某站点", "url": "https://某站点.net/album/1",
                 "href": "/link/d/1", "position": 1, "embed": "",
                 "embed_height": 0, "embed_src": ""},
                {"name": "某站点", "url": "https://某站点/subject/1",
                 "href": "/link/d/2", "position": 2, "embed": "",
                 "embed_height": 0, "embed_src": ""},
                {"name": "某站点",
                 "url": "https://某站点.com/?p=1",
                 "href": "/link/d/3", "position": 3, "embed": "",
                 "embed_height": 0, "embed_src": ""},
            ],
            "credits": [], "tags": [], "genres": [], "missing": [],
            "tracks": [], "track_count": 0,
        })
        self.assertEqual(html.count('class="link-pill"'), 3,
                         "三个来源站外链都该显示")


class TestLinksCentered(unittest.TestCase):
    """外链区**居中显示**(按钮行 + 播放器)。"""

    def _css(self) -> str:
        return (ROOT / "dddb/public/static/public.css").read_text(
            encoding="utf-8")

    def test_links_row_centered(self) -> None:
        css = self._css()
        import re
        m = re.search(r"\.links \{([^}]*)\}", css)
        self.assertIsNotNone(m, "找不到 .links")
        body = m.group(1)
        self.assertIn("justify-content: center", body,
                      "外链按钮行要居中")

    def test_embed_player_centered(self) -> None:
        css = self._css()
        import re
        m = re.search(r"\.embed-player \{([^}]*)\}", css)
        self.assertIsNotNone(m, "找不到 .embed-player")
        self.assertIn("auto", m.group(1), "播放器要 auto 外边距居中")

    def test_embed_list_centered(self) -> None:
        css = self._css()
        import re
        m = re.search(r"\.embed-list \{([^}]*)\}", css)
        self.assertIsNotNone(m)
        self.assertIn("align-items: center", m.group(1))


if __name__ == "__main__":
    unittest.main()
