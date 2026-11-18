"""本轮测试: 忘记密码/重置令牌、未验证账号 24h 清理、模板结构."""

from __future__ import annotations

import time
import unittest
from pathlib import Path

from tests._tmp import temp_dir

from dddb.public import templates as T
from dddb.public import verify as V
from dddb.public.users import UserStore


class TestResetTokens(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_dir("reset-")
        self.root = self._ctx.__enter__()
        self.store = UserStore(self.root / "users.sqlite3", iterations=1000)
        self.store.create_user("bob", "password-123", email="bob@example.com",
                               email_verified=True)

    def tearDown(self) -> None:
        self.store.close()
        self._ctx.__exit__(None, None, None)

    def test_verify_link_token_does_not_consume(self) -> None:
        token = self.store.issue_email_link("bob", purpose=V.PURPOSE_PASSWORD_RESET)
        ok, reason, name = self.store.verify_link_token(
            token, purpose=V.PURPOSE_PASSWORD_RESET)
        self.assertTrue(ok, reason)
        self.assertEqual(name, "bob")
        # 再看一次仍然有效(没消费)
        ok2, _r, _n = self.store.verify_link_token(
            token, purpose=V.PURPOSE_PASSWORD_RESET)
        self.assertTrue(ok2)
        # 消费后才失效
        ok3, _r, _n = self.store.redeem_email_link(
            token, purpose=V.PURPOSE_PASSWORD_RESET)
        self.assertTrue(ok3)
        ok4, reason4, _n = self.store.verify_link_token(
            token, purpose=V.PURPOSE_PASSWORD_RESET)
        self.assertFalse(ok4)
        self.assertIn("用过", reason4)

    def test_reset_link_purpose_isolation(self) -> None:
        token = self.store.issue_email_link("bob", purpose=V.PURPOSE_PASSWORD_RESET)
        ok, _r, _n = self.store.verify_link_token(token, purpose=V.PURPOSE_EMAIL_LINK)
        self.assertFalse(ok)

    def test_find_by_email(self) -> None:
        user = self.store.find_by_email("bob@example.com")
        self.assertIsNotNone(user)
        self.assertEqual(user["username"], "bob")
        self.assertIsNone(self.store.find_by_email("nobody@example.com"))


class TestPurgeUnverified(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_dir("purge-")
        self.root = self._ctx.__enter__()
        self.store = UserStore(self.root / "users.sqlite3", iterations=1000)
        self.store.create_user("old_uv", "password-123", email="a@example.com",
                               email_verified=False)
        self.store.create_user("new_uv", "password-123", email="b@example.com",
                               email_verified=False)
        self.store.create_user("verified", "password-123", email="c@example.com",
                               email_verified=True)
        # 把 old_uv 的创建时间拨到 2 天前
        self.store._execute("UPDATE users SET created_at = ? WHERE username = 'old_uv'",
                            (time.time() - 2 * 86400,))

    def tearDown(self) -> None:
        self.store.close()
        self._ctx.__exit__(None, None, None)

    def test_only_stale_unverified_are_deleted(self) -> None:
        removed = self.store.purge_unverified()
        self.assertEqual(removed, 1)
        self.assertIsNone(self.store.get("old_uv"))
        self.assertIsNotNone(self.store.get("new_uv"))
        self.assertIsNotNone(self.store.get("verified"))


class TestRoundTemplates(unittest.TestCase):
    def _ctx(self) -> T.SiteContext:
        return T.SiteContext(csrf_token="tok")

    def test_login_page_has_forgot_link(self) -> None:
        html = T.login_page(self._ctx())
        self.assertIn('href="/forgot"', html)
        self.assertIn("忘记密码?", html)

    def test_forgot_and_reset_pages(self) -> None:
        forgot = T.forgot_page(self._ctx())
        self.assertIn('action="/forgot"', forgot)
        reset = T.reset_page(self._ctx(), token="abc", username="bob")
        self.assertIn('action="/reset"', reset)
        self.assertIn('name="token" value="abc"', reset)

    def test_security_page_is_buttons_and_dialogs_only(self) -> None:
        """安全设置只留两个按钮 + 对话框里的表单, 不写实现说明."""
        html = T.account_page(
            T.SiteContext(csrf_token="tok"),
            user={"id": 1, "username": "bob", "role": "viewer", "role_label": "用户"},
            profile={}, tab="security", session_confirmed=True,
        )
        self.assertNotIn('name="old_password"', html)
        self.assertIn('data-dialog-open="dlg-password"', html)
        self.assertIn('data-dialog-open="dlg-delete"', html)
        self.assertIn('action="/account/password"', html)
        self.assertIn('action="/account/delete"', html)
        self.assertIn('action="/account/security-code"', html)
        # 网页不写实现说明
        self.assertNotIn("不需要输入当前密码", html)
        self.assertNotIn("当前登录环境", html)
        self.assertNotIn("修改密码与注销账号都需要邮箱验证码", html)

    def test_tracklist_title_is_outside_table(self) -> None:
        html = T.album_detail_page(
            self._ctx(),
            album={"key": "k", "title": "T", "circle": "C",
                   "tracks": [{"title": "a", "index": 1, "duration": 120}]},
        )
        self.assertIn('class="tracklist-head"', html)
        self.assertNotIn("<caption>", html)
        self.assertIn("共 1 首", html)


if __name__ == "__main__":
    unittest.main()