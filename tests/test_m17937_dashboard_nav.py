"""用户 m17937: 看板 CPU / 访问 IP / 站务导航下拉.

三个问题各有各的根因, 分开钉住:

1. **CPU 永远显示「—」** —— 不是采不到, 是模板里 ``float(x or -1)``
   把**真空载的 0.0** 当成"没有值"(0.0 是假值), 再叠上 0.12 秒窗口
   太小、两次采样常常一个 jiffy 都没走。
2. **IP 全是 127.0.0.1** —— 线上 ``config.toml`` 根本没有 ``[public]``
   段, ``behind_proxy`` 取默认 False, 于是永远只信 TCP 对端; 而服务只
   监听 ``127.0.0.1:8940``, 对端必然是本机 Caddy。
3. **站务导航超出手机屏幕** —— 三个站务入口各占一格, 现在收进 ``<details>``
   下拉, 其余链接照旧平铺。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from dddb.public import ratelimit, visits

ROOT = Path(__file__).resolve().parent.parent
TPL = (ROOT / "dddb" / "public" / "templates.py").read_text(encoding="utf-8")
CSS = (ROOT / "dddb" / "public" / "static" / "public.css").read_text(encoding="utf-8")
JS = (ROOT / "dddb" / "public" / "static" / "public.js").read_text(encoding="utf-8")

sys.path.insert(0, str(ROOT))
from dddb.public import templates as T  # noqa: E402


def nav(**kwargs) -> str:
    params = dict(lang="zh", is_editor=True, is_superuser=True, logged_in=True)
    params.update(kwargs)
    return T._nav(params.pop("active", "albums"), **params)


# --------------------------------------------------------------------------
# 1. CPU 占用率
# --------------------------------------------------------------------------


class TestCpuSampling(unittest.TestCase):
    """``cpu_percent`` 不能在空载时谎报"读不到"."""

    def setUp(self) -> None:
        self._saved = visits.cpu_snapshot
        self._sleeps: list[float] = []
        self._real_sleep = visits.time.sleep
        visits.time.sleep = lambda s: self._sleeps.append(s)

    def tearDown(self) -> None:
        visits.cpu_snapshot = self._saved
        visits.time.sleep = self._real_sleep

    def _seq(self, *snapshots) -> None:
        values = list(snapshots)

        def fake():
            return values.pop(0) if values else snapshots[-1]

        visits.cpu_snapshot = fake

    def test_zero_jiffy_window_retries_instead_of_giving_up(self) -> None:
        # 前两次采样一模一样(空载小机器上很常见) —— 补采一次就够了。
        self._seq((100, 200), (100, 200), (110, 220))
        value = visits.cpu_percent()
        self.assertGreaterEqual(value, 0.0)
        self.assertAlmostEqual(value, 50.0)  # (220-200)=20 total, idle 10 -> 50%
        self.assertEqual(len(self._sleeps), 2)  # 一次常规 + 一次补采

    def test_still_unknown_when_no_ticks_at_all(self) -> None:
        self._seq((100, 200), (100, 200), (100, 200))
        self.assertEqual(visits.cpu_percent(), -1.0)

    def test_idle_machine_reads_as_zero_not_unknown(self) -> None:
        # idle 与 total 同步增长 = 完全空闲, 应该是 0.0 而不是 -1
        self._seq((100, 200), (120, 220))
        self.assertEqual(visits.cpu_percent(), 0.0)

    def test_window_is_no_longer_a_twelfth_of_a_second(self) -> None:
        self._seq((100, 200), (110, 220))
        visits.cpu_percent()
        self.assertGreaterEqual(self._sleeps[0], 0.25)


class TestDashboardGaugeShowsZero(unittest.TestCase):
    """模板里 ``0.0 or -1`` 曾把 0% 变成「—」."""

    def test_cpu_gauge_renders_zero_percent(self) -> None:
        html = T.dashboard_page(
            T.SiteContext(lang="zh", is_editor=True, is_superuser=True),
            data={"server": {
                "cpu_percent": 0.0, "uptime": "1 天",
                "memory": {"percent": 0.0},
                "disk": {"percent": 0.0},
                "load": {"one": 0.0, "five": 0.0, "fifteen": 0.0},
            }},
        )
        self.assertIn("<b>0%</b>", html)
        self.assertNotIn("<b>—</b>", html)

    def test_negative_still_means_unknown(self) -> None:
        html = T.dashboard_page(
            T.SiteContext(lang="zh", is_editor=True, is_superuser=True),
            data={"server": {"cpu_percent": -1.0, "load": {"one": -1.0}}},
        )
        self.assertIn("<b>—</b>", html)

    def test_zero_load_is_shown_as_numbers(self) -> None:
        html = T.dashboard_page(
            T.SiteContext(lang="zh", is_editor=True, is_superuser=True),
            data={"server": {
                "cpu_percent": 12.0,
                "load": {"one": 0.0, "five": 0.0, "fifteen": 0.0},
            }},
        )
        self.assertIn("0.00 / 0.00 / 0.00", html)

    def test_template_uses_explicit_none_check(self) -> None:
        # 注释里会提到老写法, 所以只查真正的代码行(以 raw_load / raw_cpu 开头的那几行).
        code = [line.strip() for line in TPL.splitlines()
                if line.strip().startswith(("raw_load =", "raw_cpu =", "load_one =", "cpu_percent ="))]
        self.assertTrue(code, "没找到取值的代码行")
        for line in code:
            self.assertNotIn("or -1", line, line)
        self.assertIn("raw_cpu = server.get(\"cpu_percent\")", code)


# --------------------------------------------------------------------------
# 2. 访问 IP
# --------------------------------------------------------------------------


class TestClientIpBehindLocalProxy(unittest.TestCase):
    """本机反代转发时必须认 ``X-Forwarded-For``, 否则线上全记 127.0.0.1."""

    def test_loopback_peer_trusts_forwarded_for(self) -> None:
        headers = {"x-forwarded-for": "203.0.113.9"}
        self.assertEqual(
            ratelimit.client_ip_from_headers(headers, "127.0.0.1"), "203.0.113.9"
        )

    def test_ipv6_loopback_too(self) -> None:
        headers = {"x-forwarded-for": "203.0.113.9, 127.0.0.1"}
        self.assertEqual(
            ratelimit.client_ip_from_headers(headers, "::1"), "203.0.113.9"
        )

    def test_non_loopback_peer_still_ignores_the_header(self) -> None:
        # 直连进来的请求不能靠伪造头绕过限速 —— 这条是安全底线。
        headers = {"x-forwarded-for": "1.2.3.4"}
        self.assertEqual(
            ratelimit.client_ip_from_headers(headers, "198.51.100.7"), "198.51.100.7"
        )

    def test_cloudflare_header_wins_over_forwarded_for(self) -> None:
        # XFF 是"追加"的, 客户端能把自己编的值留在链头; CF-Connecting-IP 由
        # 边缘节点覆盖写, 所以后者优先。
        headers = {"cf-connecting-ip": "203.0.113.9",
                   "x-forwarded-for": "1.2.3.4, 203.0.113.9"}
        self.assertEqual(
            ratelimit.client_ip_from_headers(headers, "127.0.0.1"), "203.0.113.9"
        )

    def test_loopback_without_headers_stays_loopback(self) -> None:
        self.assertEqual(ratelimit.client_ip_from_headers({}, "127.0.0.1"), "127.0.0.1")

    def test_explicit_trust_proxy_still_works(self) -> None:
        headers = {"x-real-ip": "203.0.113.9"}
        self.assertEqual(
            ratelimit.client_ip_from_headers(headers, "10.1.2.3", trust_proxy=True),
            "203.0.113.9",
        )

    def test_garbage_peer_is_unknown(self) -> None:
        self.assertEqual(ratelimit.client_ip_from_headers({}, "nope"), "unknown")


# --------------------------------------------------------------------------
# 3. 站务导航下拉
# --------------------------------------------------------------------------


class TestNavAdminDropdown(unittest.TestCase):
    def test_superuser_gets_the_dropdown(self) -> None:
        html = nav(active="dashboard")
        self.assertIn('<details class="nav-more" data-nav-more="1">', html)
        self.assertIn(">站务</summary>", html)

    def test_all_three_admin_links_are_inside_it(self) -> None:
        html = nav()
        start = html.index('class="nav-more"')
        menu = html[html.index("nav-more-menu", start):]
        for href, text in (("/review", "审核队列"),
                           ("/users", "用户管理"),
                           ("/dashboard", "数据看板")):
            self.assertIn(f'href="{href}"', menu, href)
            self.assertIn(text, menu, text)

    def test_active_page_highlights_inside_the_menu(self) -> None:
        html = nav(active="users_admin")
        self.assertIn('nav-more-item is-active" href="/users" aria-current="page"', html)
        self.assertIn("nav-more-btn is-active", html)

    def test_other_links_stay_flat_and_visible(self) -> None:
        # 用导航里不存在的 key 当 active, 免得某个链接带 is-active 干扰断言
        html = nav(active="no-such-page")
        for href in ("/albums", "/events", "/circles", "/artists",
                     "/tags", "/stats", "/edit", "/about", "/account"):
            self.assertIn(f'class="nav-link" href="{href}"', html, href)
        # 这些都不该被塞进下拉
        flat = html[:html.index('class="nav-more"')]
        self.assertNotIn("/review", flat)
        self.assertNotIn("/dashboard", flat)

    def test_plain_user_has_no_dropdown_at_all(self) -> None:
        html = nav(is_superuser=False, is_editor=False)
        self.assertNotIn("nav-more", html)
        self.assertNotIn("审核队列", html)
        self.assertNotIn("用户管理", html)
        self.assertNotIn("数据看板", html)

    def test_guest_has_no_dropdown(self) -> None:
        html = nav(is_superuser=False, is_editor=False, logged_in=False)
        self.assertNotIn("nav-more", html)
        self.assertIn("注册", html)

    def test_dropdown_works_without_javascript(self) -> None:
        # 原生 <details>/<summary>, 不是"按钮 + 脚本展开"。
        html = nav()
        self.assertIn("<details", html)
        self.assertIn("<summary", html)
        self.assertNotIn("onclick", html)


class TestNavAssets(unittest.TestCase):
    def test_css_has_nav_more_rules(self) -> None:
        for token in (".nav-more { position: relative; }",
                      ".nav-more-menu {",
                      ".nav-more-item {",
                      ".nav-more > summary::-webkit-details-marker"):
            self.assertIn(token, CSS, token)

    def test_mobile_nav_wraps_instead_of_scrolling(self) -> None:
        # 横向滚动会把下拉菜单裁掉, 而且用户说"看起来还是超出屏幕"。
        block = CSS[CSS.index("@media (max-width: 860px)"):]
        block = block[:block.index("@media (max-width: 520px)")] if "@media (max-width: 520px)" in block else block
        nav_block = block[block.index(".nav {"):block.index("}", block.index(".nav {"))]
        self.assertIn("flex-wrap: wrap", nav_block)
        self.assertNotIn("overflow-x: auto", nav_block)
        self.assertIn(".nav-more-menu", block)

    def test_js_closes_the_menu_on_outside_click_and_escape(self) -> None:
        self.assertIn("function setupNavMore()", JS)
        start = JS.index("function setupNavMore()")
        body = JS[start:JS.index("\n  }\n", start)]
        self.assertIn('addEventListener("click"', body)
        self.assertIn('addEventListener("keydown"', body)
        self.assertIn("Escape", body)
        self.assertIn("setupNavMore();", JS)

    def test_versions_bumped(self) -> None:
        for name, floor in (("public.css", 84), ("public.js", 73)):
            match = re.search(name.replace(".", r"\.") + r"\?v=(\d+)", TPL)
            self.assertIsNotNone(match, name)
            self.assertGreaterEqual(int(match.group(1)), floor, name)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()