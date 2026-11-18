"""本轮四项修正的回归测试.

覆盖:
* Resend 发信器(用户选定的方案: Resend + Cloudflare) —— 配置解析、请求形状、失败语义
* `mailer_from_env` 优先 Resend、其次 SMTP、都没有则 None
* 页头**不再重复**渲染登录/注册(auth_strip 只留身份提示)
* favicon / logo 的 URL 带版本参数(破 CF 与浏览器缓存)
"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error

from dddb.public import templates as T
from dddb.public import verify as V

RESEND_ENV = {"RESEND_API_KEY": "re_test_123", "DDDB_MAIL_FROM": "DDDB <noreply@doujindigdb.top>"}
SMTP_ENV = {"DDDB_SMTP_HOST": "smtp.example.com", "DDDB_SMTP_FROM": "a@example.com",
            "DDDB_SMTP_USER": "u", "DDDB_SMTP_PASSWORD": "p"}


class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b'{"id":"x"}') -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


class TestResendSettings(unittest.TestCase):
    def test_needs_key_and_sender(self) -> None:
        self.assertEqual(V.resend_settings({"RESEND_API_KEY": "k"}), {})
        self.assertEqual(V.resend_settings({"DDDB_MAIL_FROM": "a@b.c"}), {})
        self.assertTrue(V.resend_settings(RESEND_ENV))

    def test_sender_falls_back_to_smtp_from(self) -> None:
        got = V.resend_settings({"RESEND_API_KEY": "k", "DDDB_SMTP_FROM": "x@y.z"})
        self.assertEqual(got["from"], "x@y.z")

    def test_email_enabled_true_with_resend(self) -> None:
        self.assertTrue(V.email_enabled(RESEND_ENV))

    def test_email_enabled_respects_kill_switch(self) -> None:
        self.assertFalse(V.email_enabled({**RESEND_ENV, "DDDB_EMAIL_VERIFY": "0"}))

    def test_reason_mentions_both_providers(self) -> None:
        reason = V.email_disabled_reason({})
        self.assertIn("Resend", reason)
        self.assertIn("SMTP", reason)


class TestResendMailer(unittest.TestCase):
    def _mailer(self, status: int = 200):
        captured: dict = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse(status)

        return V.ResendMailer(environ=RESEND_ENV, urlopen=fake_urlopen), captured

    def test_request_shape(self) -> None:
        mailer, cap = self._mailer()
        mailer.send("user@example.com", "主题", "正文")
        self.assertEqual(cap["url"], V.RESEND_API_URL)
        self.assertEqual(cap["method"], "POST")
        self.assertIn("authorization", cap["headers"])
        self.assertEqual(cap["headers"]["authorization"], "Bearer re_test_123")
        self.assertEqual(cap["body"]["to"], ["user@example.com"])
        self.assertEqual(cap["body"]["subject"], "主题")
        self.assertEqual(cap["body"]["text"], "正文")
        self.assertIn("from", cap["body"])

    def test_http_error_raises_mailer_error(self) -> None:
        mailer, _ = self._mailer(status=422)
        with self.assertRaises(V.MailerError):
            mailer.send("a@b.c", "s", "b")

    def test_network_error_raises_mailer_error(self) -> None:
        def boom(request, timeout=None):
            raise urllib.error.URLError("no network")

        mailer = V.ResendMailer(environ=RESEND_ENV, urlopen=boom)
        with self.assertRaises(V.MailerError):
            mailer.send("a@b.c", "s", "b")

    def test_unconfigured_raises(self) -> None:
        with self.assertRaises(V.MailerError):
            V.ResendMailer({})


class TestMailerFromEnv(unittest.TestCase):
    def test_prefers_resend(self) -> None:
        mailer = V.mailer_from_env({**RESEND_ENV, **SMTP_ENV})
        self.assertIsInstance(mailer, V.ResendMailer)

    def test_smtp_used_when_resend_absent(self) -> None:
        self.assertIsInstance(V.mailer_from_env(SMTP_ENV), V.SmtpMailer)

    def test_none_when_nothing_configured(self) -> None:
        self.assertIsNone(V.mailer_from_env({}))


class TestHeaderHasNoDuplicateAuthLinks(unittest.TestCase):
    def _page(self, **ctx_overrides) -> str:
        ctx = T.SiteContext(**ctx_overrides)
        return T.layout(ctx, title="首页", body="")

    def test_anonymous_has_single_login_and_register(self) -> None:
        page = self._page()
        self.assertEqual(page.count('href="/login"'), 1, "登录入口只能出现一次")
        self.assertEqual(page.count('href="/register"'), 1, "注册入口只能出现一次")

    def test_auth_strip_is_empty_for_anonymous(self) -> None:
        self.assertEqual(T.auth_strip(T.SiteContext()), "")

    def test_logged_in_has_no_duplicate_account_link(self) -> None:
        ctx = T.SiteContext(user={"username": "alice", "role": "viewer",
                                  "role_label": "普通用户"})
        page = T.layout(ctx, title="首页", body="")
        # 导航里有「我的账号」, auth_strip 里不该再来一个
        self.assertEqual(page.count('href="/account"'), 1)
        self.assertIn("auth-chip", T.auth_strip(ctx))

    def test_logout_only_in_nav_form(self) -> None:
        ctx = T.SiteContext(user={"username": "alice", "role": "editor",
                                  "role_label": "编辑者"}, csrf_token="tok")
        page = T.layout(ctx, title="首页", body="")
        self.assertEqual(page.count('action="/logout"'), 1)


class TestIconCacheBusting(unittest.TestCase):
    def test_favicon_link_is_versioned(self) -> None:
        page = T.layout(T.SiteContext(), title="首页", body="")
        self.assertRegex(page, r'href="/static/favicon\.svg\?v=\d+"')

    def test_logo_img_is_versioned(self) -> None:
        page = T.layout(T.SiteContext(), title="首页", body="")
        self.assertRegex(page, r'src="/static/logo\.svg\?v=\d+"')


if __name__ == "__main__":
    unittest.main()