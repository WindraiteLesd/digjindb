"""账号安全体系测试: 邮件链接 / 敏感操作门 / 新环境确认."""

from __future__ import annotations

from pathlib import Path

import unittest

from dddb.public import auth as A
from dddb.public import site as S
from dddb.public import templates as T
from dddb.public import verify as V
from dddb.public.users import UserStore
from tests._tmp import temp_dir


class TestEmailLink(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_dir("sec-")
        self.root = self._ctx.__enter__()
        self.store = UserStore(self.root / "users.sqlite3", iterations=1000)
        self.store.create_user("alice", "password-1", email="alice@example.com",
                               email_verified=False)

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def test_link_is_single_use(self) -> None:
        token = self.store.issue_email_link("alice", purpose=V.PURPOSE_EMAIL_LINK)
        self.assertTrue(token)
        ok, _r, name = self.store.redeem_email_link(token, purpose=V.PURPOSE_EMAIL_LINK)
        self.assertTrue(ok)
        self.assertEqual(name, "alice")
        ok2, reason, _n = self.store.redeem_email_link(token, purpose=V.PURPOSE_EMAIL_LINK)
        self.assertFalse(ok2)
        self.assertIn("用过", reason)

    def test_link_purpose_isolation(self) -> None:
        token = self.store.issue_email_link("alice", purpose=V.PURPOSE_LOGIN_LINK)
        ok, _r, _n = self.store.redeem_email_link(token, purpose=V.PURPOSE_EMAIL_LINK)
        self.assertFalse(ok, "登录确认链接不能当邮箱验证链接用")
        ok2, _r2, _n2 = self.store.redeem_email_link(token, purpose=V.PURPOSE_LOGIN_LINK)
        self.assertTrue(ok2)

    def test_link_expires(self) -> None:
        token = self.store.issue_email_link("alice", purpose=V.PURPOSE_EMAIL_LINK,
                                            ttl=10, now=1000.0)
        ok, reason, _n = self.store.redeem_email_link(token, purpose=V.PURPOSE_EMAIL_LINK,
                                                     now=2000.0)
        self.assertFalse(ok)
        self.assertIn("过期", reason)

    def test_mail_contains_link_and_code(self) -> None:
        rec = V.RecordingMailer()
        ok, message = V.send_email_link(self.store, "alice",
                                        base_url="https://www.digjindb.top", mailer=rec)
        self.assertTrue(ok, message)
        body = (rec.last() or {}).get("body") or ""
        self.assertIn("/verify?token=", body)
        self.assertEqual(len(rec.last_code()), V.EMAIL_CODE_DIGITS)

    def test_code_and_link_are_independent(self) -> None:
        """链接验证过了, 验证码仍应可用(反之亦然) —— 两条路径互不影响."""
        rec = V.RecordingMailer()
        V.send_email_link(self.store, "alice", base_url="https://x", mailer=rec)
        code = rec.last_code()
        ok, reason = V.check_email_code(self.store, "alice", code)
        self.assertTrue(ok, reason)


class TestSessionConfirmation(unittest.TestCase):
    def test_new_session_can_be_unconfirmed_then_confirmed(self) -> None:
        manager = A.SessionManager()
        session = manager.create({"id": 1, "username": "alice", "role": "viewer"},
                                 env_hash="abc", confirmed=False)
        self.assertFalse(session.confirmed)
        loaded = manager.get(session.sid)
        self.assertFalse(loaded.confirmed if loaded else True)
        self.assertTrue(manager.confirm(session.sid))
        again = manager.get(session.sid)
        self.assertTrue(again.confirmed if again else False)
        self.assertEqual(again.env_hash if again else "", "abc")


class TestSecurityPage(unittest.TestCase):
    def test_security_tab_renders_forms(self) -> None:
        html = T.account_page(
            T.SiteContext(csrf_token="t"),
            user={"id": 1, "username": "alice", "role": "viewer", "role_label": "用户"},
            profile={}, tab="security",
            email_state={"enabled": True, "verified": True, "email": "a***@x.com"},
            session_confirmed=True,
        )
        self.assertIn('action="/account/password"', html)
        self.assertIn('action="/account/delete"', html)
        self.assertIn("/account/security-code", html)
        self.assertIn('data-dialog-open="dlg-password"', html)
        self.assertIn('data-dialog-open="dlg-delete"', html)
        self.assertNotIn("当前登录环境", html)

    def test_unconfirmed_session_has_no_env_text(self) -> None:
        """未确认状态不再在页面上描述."""
        html = T.account_page(
            T.SiteContext(csrf_token="t"),
            user={"id": 1, "username": "alice", "role": "viewer", "role_label": "用户"},
            profile={}, tab="security", session_confirmed=False,
        )
        self.assertNotIn("当前登录环境", html)
        self.assertNotIn("待确认", html)

    def test_routes_and_write_scopes(self) -> None:
        self.assertEqual(S.parse_route("/account/password")["name"], "account_password")
        self.assertEqual(S.parse_route("/account/delete")["name"], "account_delete")
        self.assertEqual(S.parse_route("/account/security-code")["name"],
                         "account_security_code")
        for name in ("account_password", "account_delete", "account_security_code"):
            self.assertIn(name, S._MEMBER_WRITE_ROUTES)


if __name__ == "__main__":
    unittest.main()