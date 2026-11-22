"""修: CSP 缺 connect-src -> 所有 fetch() 报 "Failed to fetch".

用户截图: 裁切弹窗里点「上传中...」后浏览器弹
``www.digjindb.top 显示 / 上传失败: Failed to fetch``。

根因: ``_CSP`` 里有 ``default-src 'none'`` 但**没有** ``connect-src``。
按 CSP 规范, 未列出的指令回落到 ``default-src``, 于是 XHR/fetch/WebSocket
全部被拒 —— 浏览器把这种拦截报成 ``TypeError: Failed to fetch``,
看起来像网络问题, 实际请求根本没发出去。

修法: ``connect-src 'self'``。所有上传接口(封面/附图/裁切结果)都是同源
的 ``/<entity>/<key>/cover`` 与 ``/<entity>/<key>/photo``, 不需要放开别的域。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "dddb/public/site.py"
CROP = ROOT / "dddb/public/static/cover-crop.js"


def _csp() -> str:
    from dddb.public.site import _CSP

    return _CSP


class TestConnectSrcPresent(unittest.TestCase):
    def test_has_connect_src(self) -> None:
        self.assertIn("connect-src", _csp(),
                      "CSP 没有 connect-src -> fetch() 全被 default-src 'none' 拦掉")

    def test_connect_src_allows_self(self) -> None:
        m = re.search(r"connect-src([^;]*);", _csp())
        self.assertIsNotNone(m, "connect-src 格式不对")
        self.assertIn("'self'", m.group(1), "connect-src 没放行本站")

    def test_connect_src_not_wildcard(self) -> None:
        """不能图省事写 *, 那等于放弃了这条指令。"""
        m = re.search(r"connect-src([^;]*);", _csp())
        tokens = m.group(1).split()
        self.assertNotIn("*", tokens, "connect-src 不能放通配符")
        self.assertNotIn("http:", tokens, "connect-src 不能放通任意 http")
        self.assertNotIn("https:", tokens, "connect-src 不能放通任意 https")

    def test_keeps_other_directives(self) -> None:
        policy = _csp()
        for token in ("default-src 'none'", "script-src 'self'",
                      "frame-ancestors 'none'", "img-src"):
            self.assertIn(token, policy, "改 connect-src 时误删了别的指令")

    def test_no_duplicate_connect_src(self) -> None:
        self.assertEqual(_csp().count("connect-src"), 1,
                         "connect-src 重复声明")


class TestHeaderActuallySent(unittest.TestCase):
    def test_header_contains_connect_src(self) -> None:
        """真正写进响应头的那份也要有(不是只有常量里有)。"""
        src = SITE.read_text(encoding="utf-8")
        idx = src.find("Content-Security-Policy")
        self.assertGreater(idx, 0)
        body = src[idx - 200:idx + 400]
        self.assertIn("_CSP", body, "响应头没用 _CSP 常量")


class TestUploadIsSameOrigin(unittest.TestCase):
    def test_cover_action_relative(self) -> None:
        """上传地址必须同源(相对路径), 否则请 connect-src 'self' 也发不出。"""
        js = CROP.read_text(encoding="utf-8")
        idx = js.find("function uploadBlob(")
        self.assertGreater(idx, 0)
        body = js[idx:idx + 1200]
        self.assertIn("data-cover-action", body)
        # 动作地址来自表单属性(服务端渲染的相对路径), 不能在前端拼绝对域
        self.assertNotIn("http://", body)
        self.assertNotIn("https://", body)

    def test_fetch_uses_credentials(self) -> None:
        js = CROP.read_text(encoding="utf-8")
        idx = js.find("function uploadBlob(")
        body = js[idx:idx + 1200]
        self.assertIn('credentials: "same-origin"', body,
                      "缺 credentials 会丢 cookie, 上传被判未登录")


if __name__ == "__main__":
    unittest.main()
