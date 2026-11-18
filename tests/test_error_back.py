"""报错页的按钮:“回到上一页”。

用户要求: "让所有报错界面的浏览专辑按钮改为回到上一页"

设计:
    * 用 ``history.back()`` 而不是写死 ``/albums`` —— "我刚才在看什么就回哪里"
    * 没有上一页(直接打开链接)时退到首页，否则点了没反应像坏了
    * 「回到首页」作为次要入口保留
    * 401/403 的「登录/注册」不动
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STATUSES = (400, 401, 403, 404, 413, 500, 503)


def _ctx():
    from dddb.public import templates as T

    return T.SiteContext(csrf_token="t", lang="zh")


class TestErrorPageBackButton(unittest.TestCase):
    def test_no_browse_albums_button(self) -> None:
        """报错区域的按钮里不该再有「浏览专辑」。

        注意: ``layout`` 的**导航栏**本来就有 /albums 入口 —— 那是全站导航,
        不是报错页的操作按钮, 不能一起断言掉。
        """
        from dddb.public import templates as T

        for status in STATUSES:
            with self.subTest(status=status):
                html = T.error_page(_ctx(), status=status, message="x")
                self.assertNotIn("浏览专辑", html, f"{status} 页还有「浏览专辑」")
                # 只看报错块内部的链接
                block = html.split('class="empty empty-error"')[1]
                block = block.split("</div>")[0]
                self.assertNotIn('href="/albums"', block,
                                 f"{status} 报错块里还链到 /albums")

    def test_has_go_back_button(self) -> None:
        from dddb.public import templates as T

        for status in STATUSES:
            with self.subTest(status=status):
                html = T.error_page(_ctx(), status=status, message="x")
                self.assertIn("data-go-back", html, f"{status} 页缺回到上一页")
                self.assertIn("回到上一页", html)

    def test_keeps_home_link(self) -> None:
        from dddb.public import templates as T

        html = T.error_page(_ctx(), status=404, message="x")
        self.assertIn("回到首页", html)
        self.assertIn('href="/"', html)

    def test_auth_pages_keep_login_register(self) -> None:
        """401/403 的登录/注册入口不能被这次改动带走。"""
        from dddb.public import templates as T

        for status in (401, 403):
            html = T.error_page(_ctx(), status=status, message="x")
            self.assertIn('href="/login"', html)
            self.assertIn('href="/register"', html)
            self.assertIn("data-go-back", html)

    def test_button_is_not_a_link(self) -> None:
        """必须是 <button>, 不是指向 /albums 的 <a>。"""
        from dddb.public import templates as T

        html = T.error_page(_ctx(), status=404, message="x")
        self.assertIn('<button type="button" class="btn" data-go-back="1">', html)


class TestGoBackScript(unittest.TestCase):
    def test_script_defined_and_registered(self) -> None:
        js = (ROOT / "dddb/public/static/public.js").read_text(encoding="utf-8")
        self.assertIn("function setupGoBack(", js)
        boot = js.split("function boot()")[1].split("\n  }")[0]
        self.assertIn("setupGoBack()", boot, "未在 boot 里注册就不生效")

    @staticmethod
    def _block() -> str:
        """取出 setupGoBack 的函数体(按 `\n  function ` 切)．"""
        js = (ROOT / "dddb/public/static/public.js").read_text(encoding="utf-8")
        start = js.find("function setupGoBack(")
        assert start >= 0, "找不到 setupGoBack"
        tail = js[start + 1:]
        m = re.search(r"\n  function ", tail)
        return js[start:start + 1 + (m.start() if m else len(tail))]

    def test_uses_history_back(self) -> None:
        self.assertIn("history.back()", self._block())

    def test_falls_back_to_home(self) -> None:
        """history.length <= 1 时退首页, 否则点了没反应。"""
        block = self._block()
        self.assertIn("history.length > 1", block)
        self.assertIn('window.location.href = "/"', block)


class TestStyle(unittest.TestCase):
    def test_error_actions_css(self) -> None:
        css = (ROOT / "dddb/public/static/public.css").read_text(encoding="utf-8")
        self.assertIn(".error-actions", css)


if __name__ == "__main__":
    unittest.main()
