"""测试: 外链嵌入播放器 + 修订记录折叠."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from dnh.public import embed as EB

ROOT = Path(__file__).resolve().parents[1]

BASE_TS = 1794900000.0


def _detail(urls):
    from dnh.public import templates as T

    ctx = T.SiteContext(csrf_token="t", is_editor=False, lang="zh")
    return T.album_detail_page(ctx, album={
        "key": "d", "title": "T", "title_original": "T", "urls": urls,
        "credits": [], "tags": [], "genres": [], "missing": [],
        "tracks": [], "track_count": 0,
    })


def _detail(urls):
    from dnh.public import templates as T

    ctx = T.SiteContext(csrf_token="t", is_editor=False, lang="zh")
    return T.album_detail_page(ctx, album={
        "key": "d", "title": "T", "title_original": "T", "urls": urls,
        "credits": [], "tags": [], "genres": [], "missing": [],
        "tracks": [], "track_count": 0,
    })


YT_CODE = (
    '<iframe width="560" height="315" src="https://www.youtube.com/embed/'
    'l1JRKYl5CeM?si=KFM1kIzXR5ng0_K3" title="YouTube video player" '
    'frameborder="0" allowfullscreen></iframe>'
)
SC_CODE = (
    '<iframe width="100%" height="300" scrolling="no" frameborder="no" '
    'allow="autoplay; encrypted-media" src="https://w.soundcloud.com/player/'
    '?url=https%3A//api.soundcloud.com/tracks/soundcloud%253Atracks'
    '%253A2420842803&color=%23ff5500"></iframe>'
    '<div style="font-size: 10px"><a href="https://soundcloud.com/aoivcelica">'
    "MALVA.</a></div>"
)


class TestEmbedOnlyFromCode(unittest.TestCase):
    """**只认用户粘贴的 embed code**, 纯链接不生成播放器。"""

    def test_plain_youtube_url_no_embed(self) -> None:
        for url in ("https://www.youtube.com/watch?v=l1JRKYl5CeM",
                    "https://youtu.be/l1JRKYl5CeM",
                    "https://www.youtube.com/shorts/abc123"):
            self.assertIsNone(EB.extract_embed(url), url)

    def test_plain_soundcloud_url_no_embed(self) -> None:
        for url in ("https://soundcloud.com/aoivcelica/quinceblume",
                    "https://soundcloud.com/a/b"):
            self.assertIsNone(EB.extract_embed(url), url)

    def test_youtube_embed_code(self) -> None:
        info = EB.extract_embed(YT_CODE)
        self.assertIsNotNone(info)
        self.assertEqual(info["platform"], EB.EMBED_YOUTUBE)
        self.assertIn("youtube.com/embed/l1JRKYl5CeM", info["src"])
        # YouTube 不用 code 里的 height(315 对 560px 宽算的, 我们面板更宽会变扁条)
        self.assertEqual(info["height"], 0, "YouTube 用 16:9 自适应")

    def test_soundcloud_embed_code(self) -> None:
        info = EB.extract_embed(SC_CODE)
        self.assertIsNotNone(info)
        self.assertEqual(info["platform"], EB.EMBED_SOUNDCLOUD)
        self.assertIn("w.soundcloud.com/player", info["src"])
        self.assertEqual(info["height"], 300)
        # embed code 里的 <a> 文字被丢弃(不进入 src/height), 只留纯文本
        self.assertEqual(info["plain_text"], "MALVA.")

    def test_looks_like_embed(self) -> None:
        self.assertTrue(EB.looks_like_embed(YT_CODE))
        self.assertFalse(EB.looks_like_embed("https://youtu.be/x"))

    def test_reject_blocked_host_embed(self) -> None:
        """被禁站点连嵌入也不行。"""
        bad = '<iframe src="https://vgmdb.net/album/1"></iframe>'
        self.assertIsNone(EB.extract_embed(bad))

    def test_reject_non_http_src(self) -> None:
        for bad in ('<iframe src="javascript:alert(1)"></iframe>',
                    '<iframe src="data:text/html,x"></iframe>',
                    '<iframe src=""></iframe>',
                    '<iframe></iframe>'):
            self.assertIsNone(EB.extract_embed(bad), bad)

    def test_height_clamped(self) -> None:
        huge = '<iframe height="2000" src="https://w.soundcloud.com/x"></iframe>'
        self.assertEqual(EB.extract_embed(huge)["height"], EB.MAX_HEIGHT)
        tiny = '<iframe height="10" src="https://w.soundcloud.com/x"></iframe>'
        self.assertEqual(EB.extract_embed(tiny)["height"], EB.MIN_HEIGHT)

    def test_soundcloud_track_url_converted(self) -> None:
        """SoundCloud 轨道页要转成 widget 地址。"""
        target = EB.embed_target("https://soundcloud.com/a/b")
        self.assertIn("w.soundcloud.com/player", target)

    def test_youtube_src_used_as_is(self) -> None:
        target = EB.embed_target("https://www.youtube.com/embed/abc")
        self.assertEqual(target, "https://www.youtube.com/embed/abc")


class TestEmbedRender(unittest.TestCase):
    """详情页渲染."""

    def _urls(self):
        # embed_src 由 data 层从 embed code 抽出后提供(模板直接用它当 iframe src)
        from dnh.public.embed import extract_embed

        def row(name, raw, href, pos):
            info = extract_embed(raw) or {}
            return {"name": name, "url": raw, "href": href, "position": pos,
                    "embed": info.get("platform") or "",
                    "embed_height": info.get("height") or 0,
                    "embed_src": info.get("src") or ""}

        return [
            row("XFD (SoundCloud)", SC_CODE, "/link/d/1", 1),
            row("XFD (Youtube)", YT_CODE, "/link/d/2", 2),
            {"name": "特设", "url": "https://e.com/", "href": "/link/d/3",
             "position": 3, "embed": "", "embed_height": 0,
             "embed_src": ""},
        ]

    def test_iframes_rendered(self) -> None:
        html = _detail(self._urls())
        self.assertEqual(html.count("<iframe"), 2)
        self.assertEqual(html.count('class="embed-player"'), 2)

    def test_iframe_src_points_to_platform(self) -> None:
        """iframe 必须**直连平台** —— 走本站 /embed 中转会被 CSP 在跳转目标上
        拦掉, 浏览器显示「已阻止此内容」。"""
        html = _detail(self._urls())
        srcs = re.findall(r'<iframe src="([^"]*)"', html)
        self.assertEqual(len(srcs), 2)
        self.assertTrue(any("w.soundcloud.com" in s for s in srcs))
        self.assertTrue(any("youtube.com" in s for s in srcs))
        for src in srcs:
            # 本站中转是相对路径 /embed/<key>/<n>; 平台地址一定是绝对 URL
            self.assertTrue(src.startswith("https://"),
                            f"必须是平台绝对地址: {src}")
            self.assertFalse(src.startswith("/embed/"),
                             f"不能再用本站中转: {src}")

    def test_no_source_site_names_in_html(self) -> None:
        """**数据来源站**仍然不许出现(合规); 播放器平台域名允许。"""
        html = _detail(self._urls())
        for domain in ("vgmdb.net", "bgm.tv", "bangumi.tv", "chii.in",
                       "dojin-music.info", "doujinstyle.com",
                       "dizzylab.net", "discogs.com", "touhoudb.com",
                       "thwiki.cc", "vgm.io"):
            self.assertNotIn(domain, html, f"页面不该出现来源站 {domain}")

    def test_embeds_below_links(self) -> None:
        """**播放器必须在链接按钮下方**, 不能在 .links 里面。"""
        html = _detail(self._urls())
        links_start = html.find('<div class="links">')
        links_end = html.find("</div>", links_start)
        embeds_start = html.find('<div class="embed-list">')
        self.assertGreater(links_start, 0)
        self.assertGreater(embeds_start, 0)
        self.assertGreater(embeds_start, links_end,
                           "embed-list 要在 .links 闭合之后")
        # .links 内部不能有 embed-player(否则 flex 会把按钮拉长)
        self.assertNotIn("embed-player", html[links_start:links_end])

    def test_plain_url_no_iframe(self) -> None:
        html = _detail([{"name": "site", "url": "https://example.com/",
                         "href": "/link/d/1", "position": 1,
                         "embed": "", "embed_height": 0}])
        self.assertNotIn("<iframe", html)
        self.assertIn('class="link-pill"', html)

    def test_embed_entries_have_no_pill(self) -> None:
        """embed code 的条目**不出按钮**, 名字完全不外显。"""
        html = _detail(self._urls())
        # 3 条里 2 条是 embed -> 只剩 1 个普通按钮(特设)
        self.assertEqual(html.count('class="link-pill"'), 1)
        self.assertIn("特设", html)
        # embed 条目的名字(测试数据里叫 XFD (...))绝不出现
        self.assertNotIn("XFD", html, "embed 条目的链接名不该外显")

    def test_all_embed_no_empty_links_div(self) -> None:
        """全条都为 embed 时不留空的 .links 容器。"""
        html = _detail(self._urls()[:2])
        self.assertNotIn('<div class="links"></div>', html)
        self.assertEqual(html.count("<iframe"), 2)

    def test_iframe_attrs(self) -> None:
        html = _detail(self._urls())
        for m in re.finditer(r"<iframe[^>]*>", html):
            tag = m.group(0)
            for attr in ("referrerpolicy=", "allowfullscreen", "loading=",
                         "allow="):
                self.assertIn(attr, tag)

    def test_height_from_code_used(self) -> None:
        """SoundCloud 用 code 里的固定高度; YouTube 用 16:9 自适应。"""
        html = _detail(self._urls())
        self.assertIn("height:300px", html, "SoundCloud 用固定高度")
        # YouTube 不写内联 height -> CSS aspect-ratio 接管
        self.assertNotIn("height:315px", html, "YouTube 不该固定 315px")
        yt_iframe = [m for m in re.findall(r"<iframe[^>]*>", html)
                     if "youtube" in m]
        self.assertTrue(yt_iframe)
        self.assertNotIn("style=", yt_iframe[0], "YouTube 不写内联 height")


class TestEmbedRoute(unittest.TestCase):
    """服务端 /embed 路由."""

    def test_route_registered(self) -> None:
        from dnh.public import site as SITE

        self.assertEqual(SITE._ROUTE_MAP.get("embed"), "embed")
        self.assertIn("embed", SITE._ENTITY_ROUTES)
        self.assertTrue(hasattr(SITE.PublicSite, "serve_embed"))

    def test_dispatch_in_handle_read(self) -> None:
        """必须在 _handle_read(返回 Response)里分发, 不能在 render_page."""
        import inspect

        from dnh.public import site as SITE

        self.assertIn('"embed"',
                      inspect.getsource(SITE.PublicSite._handle_read))
        self.assertNotIn('"embed"',
                         inspect.getsource(SITE.PublicSite.render_page))

    def test_only_embed_code_served(self) -> None:
        """纯链接没有播放器 -> /embed 也 404。"""
        import inspect

        from dnh.public import site as SITE

        src = inspect.getsource(SITE.PublicSite.serve_embed)
        self.assertIn("extract_embed", src)
        self.assertIn("_plain_404", src)

    def test_csp_allows_platform_frames(self) -> None:
        """CSP 必须放行平台域, 否则 iframe 被拦。"""
        from dnh.public import site as SITE

        self.assertIn("frame-src 'self'", SITE._CSP)
        for domain in ("https://www.youtube.com",
                       "https://www.youtube-nocookie.com",
                       "https://w.soundcloud.com"):
            self.assertIn(domain, SITE._CSP,
                          f"CSP 必须放行 {domain}")

    def test_csp_does_not_allow_blocked_hosts(self) -> None:
        """被禁来源站不能进 CSP。"""
        from dnh.public import site as SITE

        for domain in ("vgmdb.net", "bgm.tv", "doujinstyle.com"):
            self.assertNotIn(domain, SITE._CSP)


class TestGroupRevisions(unittest.TestCase):
    """同一人短时间内的修改折叠."""

    def _revs(self):
        return [
            {"id": 9, "username": "alice", "created_at": BASE_TS,
             "created_at_text": "01:00", "summary": "改标题"},
            {"id": 8, "username": "alice", "created_at": BASE_TS - 120,
             "created_at_text": "00:58", "summary": "改曲目"},
            {"id": 7, "username": "alice", "created_at": BASE_TS - 300,
             "created_at_text": "00:55", "summary": "改艺人"},
            {"id": 6, "username": "bob", "created_at": BASE_TS - 1000,
             "created_at_text": "00:43", "summary": "改社团"},
        ]

    def test_same_user_folded(self) -> None:
        from dnh.public.templates import group_revisions

        groups = group_revisions(self._revs())
        alice = groups[0]
        self.assertTrue(alice["folded"])
        self.assertEqual(alice["count"], 3)
        self.assertEqual([it["id"] for it in alice["items"]], [9, 8, 7])
        self.assertEqual(alice["first"]["id"], 9, "first 是最新那条")

    def test_different_user_breaks_group(self) -> None:
        from dnh.public.templates import group_revisions

        groups = group_revisions(self._revs())
        self.assertEqual(len(groups), 2)
        self.assertFalse(groups[1]["folded"])
        self.assertEqual(groups[1]["first"]["id"], 6)

    def test_far_apart_not_folded(self) -> None:
        """超出 10 分钟不折叠。"""
        from dnh.public.templates import group_revisions

        revs = [
            {"id": 2, "username": "alice", "created_at": BASE_TS,
             "created_at_text": "01:00", "summary": "a"},
            {"id": 1, "username": "alice", "created_at": BASE_TS - 3600,
             "created_at_text": "00:00", "summary": "b"},
        ]
        groups = group_revisions(revs)
        self.assertEqual(len(groups), 2)
        self.assertFalse(groups[0]["folded"])

    def test_single_revision(self) -> None:
        from dnh.public.templates import group_revisions

        groups = group_revisions([self._revs()[0]])
        self.assertEqual(len(groups), 1)
        self.assertFalse(groups[0]["folded"])

    def test_empty(self) -> None:
        from dnh.public.templates import group_revisions

        self.assertEqual(group_revisions([]), [])

    def test_span_calculated(self) -> None:
        from dnh.public.templates import group_revisions

        groups = group_revisions(self._revs())
        self.assertAlmostEqual(groups[0]["span"], 300.0, delta=1)


class TestHistoryRender(unittest.TestCase):
    """修订页渲染折叠行。"""

    def _render(self, revs):
        from dnh.public import templates as T

        ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh")
        return T.history_page(ctx, album={"key": "k", "title": "T"},
                              revisions=revs)

    def test_folded_row(self) -> None:
        revs = [
            {"id": 9, "username": "alice", "created_at": BASE_TS,
             "created_at_text": "01:00", "summary": "改标题"},
            {"id": 8, "username": "alice", "created_at": BASE_TS - 120,
             "created_at_text": "00:58", "summary": "改曲目"},
        ]
        html = self._render(revs)
        self.assertIn("hist-fold-badge", html)
        self.assertIn("2 次", html)
        self.assertIn("hist-fold-items", html, "可展开逐条")

    def test_version_range_shown(self) -> None:
        revs = [
            {"id": 9, "username": "a", "created_at": BASE_TS,
             "created_at_text": "01:00", "summary": "x"},
            {"id": 8, "username": "a", "created_at": BASE_TS - 60,
             "created_at_text": "00:59", "summary": "y"},
        ]
        html = self._render(revs)
        self.assertIn("#8–#9", html, "版本号应显示范围")

    def test_unfolded_single_row(self) -> None:
        revs = [
            {"id": 5, "username": "bob", "created_at": BASE_TS,
             "created_at_text": "01:00", "summary": "单独一次"},
        ]
        html = self._render(revs)
        self.assertNotIn("hist-fold-badge", html)
        self.assertIn("#5", html)

    def test_each_item_still_comparable(self) -> None:
        """折叠后组内每一条仍能单独对比。"""
        revs = [
            {"id": 9, "username": "a", "created_at": BASE_TS,
             "created_at_text": "01:00", "summary": "x"},
            {"id": 8, "username": "a", "created_at": BASE_TS - 60,
             "created_at_text": "00:59", "summary": "y"},
        ]
        html = self._render(revs)
        self.assertIn("rev=9", html)
        self.assertIn("rev=8", html)


class TestHistoryCss(unittest.TestCase):
    def test_styles(self) -> None:
        css = (ROOT / "dnh/public/static/public.css").read_text(
            encoding="utf-8")
        for token in (".hist-fold-badge", ".hist-fold-span",
                      ".hist-fold-detail", ".hist-fold-items",
                      ".embed-player", ".embed-list"):
            self.assertIn(token, css, f"缺样式 {token}")


class TestEmbedSave(unittest.TestCase):
    """保存表单时 embed code 必须能被接受(否则用户存不进播放器)."""

    SC = ('<iframe width="100%" height="300" src="https://w.soundcloud.com/'
          'player/?url=https%3A//api.soundcloud.com/tracks/x"></iframe>')
    YT = ('<iframe width="560" height="315" src="https://www.youtube.com/'
          'embed/l1JRKYl5CeM" frameborder="0" allowfullscreen></iframe>')

    def _parse(self, urls):
        from dnh.public.site import parse_album_form

        return parse_album_form({
            "title_original": "T",
            "url_key": ["k{}".format(i) for i in range(len(urls))],
            "url_value": list(urls),
        })

    def test_plain_url_accepted(self) -> None:
        values, errors = self._parse(["https://example.com/x"])
        self.assertFalse(errors, errors)
        self.assertEqual(values["urls"], {"k0": "https://example.com/x"})

    def test_soundcloud_code_accepted(self) -> None:
        values, errors = self._parse([self.SC])
        self.assertFalse(errors, f"embed code 被拒: {errors}")
        self.assertEqual(values["urls"], {"k0": self.SC})

    def test_youtube_code_accepted(self) -> None:
        values, errors = self._parse([self.YT])
        self.assertFalse(errors, f"embed code 被拒: {errors}")
        self.assertEqual(values["urls"], {"k0": self.YT})

    def test_mixed_accepted(self) -> None:
        values, errors = self._parse(["https://example.com/x", self.SC])
        self.assertFalse(errors, errors)
        self.assertEqual(len(values["urls"]), 2)

    def test_blocked_host_embed_rejected(self) -> None:
        """被禁站点的 embed code 仍要拒。"""
        values, errors = self._parse(
            ['<iframe src="https://vgmdb.net/album/1"></iframe>'])
        self.assertIn("urls", errors)
        self.assertEqual(values["urls"], {})

    def test_javascript_src_rejected(self) -> None:
        values, errors = self._parse(
            ['<iframe src="javascript:alert(1)"></iframe>'])
        self.assertIn("urls", errors)
        self.assertEqual(values["urls"], {})

    def test_garbage_rejected(self) -> None:
        values, errors = self._parse(["not-a-url"])
        self.assertIn("urls", errors)
        self.assertEqual(values["urls"], {})

    def test_error_message_mentions_iframe(self) -> None:
        """报错文案要提示可以粘贴 iframe 代码。"""
        _, errors = self._parse(["not-a-url"])
        self.assertIn("iframe", errors["urls"])


if __name__ == "__main__":
    unittest.main()