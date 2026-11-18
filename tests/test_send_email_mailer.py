"""回归: 配了 Resend 时 `send_email_code` 必须用 Resend, 不能抛"未配置 SMTP".

线上真实故障: 配好 RESEND_API_KEY 后注册仍 500, 日志 `POST /register 异常: 未配置 SMTP`
—— 因为 `send_email_code` 里写死了 `SmtpMailer(environ=environ)`。
"""

from __future__ import annotations

import unittest
from unittest import mock

from dddb.public import verify as V
from dddb.public.users import UserStore
from tests._tmp import temp_dir

RESEND_ENV = {"RESEND_API_KEY": "re_test_x", "DDDB_MAIL_FROM": "DDDB <a@b.c>"}
SMTP_ENV = {"DDDB_SMTP_HOST": "smtp.example.com", "DDDB_SMTP_FROM": "a@example.com"}


class TestSendEmailCodeChoosesMailer(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_dir("verify-mailer-")
        self.root = self._ctx.__enter__()
        self.store = UserStore(self.root / "users.sqlite3", iterations=1000)
        self.store.create_user("alice", "password-abc123", email="alice@example.com",
                               email_verified=False)

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def test_resend_configured_uses_resend_mailer(self) -> None:
        """**核心回归**: 配了 Resend 就不该抛「未配置 SMTP」."""
        recorder = V.RecordingMailer()
        with mock.patch.object(V, "mailer_from_env", return_value=recorder) as spy:
            ok, message = V.send_email_code(self.store, "alice", environ=RESEND_ENV)
        self.assertTrue(ok, message)
        spy.assert_called_once()
        self.assertEqual(len(recorder.outbox), 1)
        self.assertTrue(recorder.last_code(), "邮件里应有 6 位验证码")

    def test_smtp_only_still_uses_smtp_mailer(self) -> None:
        recorder = V.RecordingMailer()
        with mock.patch.object(V, "mailer_from_env", return_value=recorder) as spy:
            ok, message = V.send_email_code(self.store, "alice", environ=SMTP_ENV)
        self.assertTrue(ok, message)
        spy.assert_called_once()

    def test_not_configured_degrades(self) -> None:
        ok, message = V.send_email_code(self.store, "alice", environ={})
        self.assertFalse(ok)
        self.assertIn("未配置发信", message)

    def test_injected_mailer_wins(self) -> None:
        recorder = V.RecordingMailer()
        ok, _ = V.send_email_code(self.store, "alice", mailer=recorder, environ={})
        self.assertTrue(ok)
        self.assertEqual(len(recorder.outbox), 1)


if __name__ == "__main__":
    unittest.main()