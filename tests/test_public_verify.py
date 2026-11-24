"""注册验证(验证码 + 邮箱验证码)的测试 —— 全部离线, 不联网.

覆盖:

1. 验证码: 题型 / 正确答案 / 答错计数 / 限次作废 / 一次性(重放) / 过期 /
   存储隔离 / 上限淘汰 / 统计;
2. 邮箱验证的**开关**: 没配 SMTP 时降级(返回原因而不是抛异常), 配了才启用,
   ``DDDB_EMAIL_VERIFY=0`` 能强制关闭;
3. 邮箱验证码: 发送(注入 RecordingMailer) / 正确 / 错误计数 / 过期 / 重放 /
   限次 / 重发间隔 / 各种边界(没邮箱、已验证、用户不存在、邮件发送失败);
4. 端到端: 验证码 + 邮箱验证 → 账号可登录; 未验证 → 登录被拒但原因明确;
5. ``SmtpMailer`` / ``mailer_from_env`` 的配置行为(不真连 SMTP)。
"""

from __future__ import annotations

import re
import unittest

from dddb.public import verify as V
from dddb.public.users import ROLE_VIEWER, UserStore, hash_password
from tests._tmp import temp_dir

ARITH = re.compile(r"^(\d+) ([-+×]) (\d+) = \?$")
CHARS = re.compile(r"照抄一遍: ([A-Z0-9]+)$")


def answer_of(question: str) -> str:
    """从算术题文本算出正确答案(测试只依赖题面, 不读内部状态)."""
    match = ARITH.match(question.strip())
    if match is None:
        raise AssertionError(f"不是算术题: {question!r}")
    left, operator, right = int(match.group(1)), match.group(2), int(match.group(3))
    if operator == "+":
        return str(left + right)
    if operator == "-":
        return str(left - right)
    return str(left * right)


# --------------------------------------------------------------------------
# 1. 验证码
# --------------------------------------------------------------------------


class TestCaptcha(unittest.TestCase):
    def setUp(self) -> None:
        self.store = V.CaptchaStore()

    def test_new_captcha_shape(self) -> None:
        challenge = V.new_captcha(store=self.store)
        self.assertEqual(set(challenge), {"id", "question", "kind", "ttl", "expires_at", "max_attempts"})
        self.assertTrue(challenge["id"])
        self.assertEqual(challenge["kind"], "arithmetic")
        self.assertEqual(challenge["ttl"], int(V.CAPTCHA_TTL))
        self.assertIn("= ?", challenge["question"])
        self.assertEqual(challenge["max_attempts"], V.CAPTCHA_MAX_ATTEMPTS)

    def test_each_challenge_gets_a_unique_id(self) -> None:
        ids = {V.new_captcha(store=self.store)["id"] for _ in range(30)}
        self.assertEqual(len(ids), 30)

    def test_arithmetic_answer_is_accepted_once(self) -> None:
        challenge = V.new_captcha(store=self.store, now=1000.0)
        ok, reason = V.check_captcha(challenge["id"], answer_of(challenge["question"]),
                                     store=self.store, now=1001.0)
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")
        # 一次性: 同一个 id 不能再过
        again, reason = V.check_captcha(challenge["id"], answer_of(challenge["question"]),
                                        store=self.store, now=1002.0)
        self.assertFalse(again)
        self.assertIn("失效", reason)

    def test_wrong_answer_counts_down_and_locks_out(self) -> None:
        challenge = V.new_captcha(store=self.store, now=1000.0)
        ok, reason = V.check_captcha(challenge["id"], "999999", store=self.store, now=1001.0)
        self.assertFalse(ok)
        self.assertIn("还可以再试 2 次", reason)
        ok, reason = V.check_captcha(challenge["id"], "999999", store=self.store, now=1002.0)
        self.assertIn("还可以再试 1 次", reason)
        ok, reason = V.check_captcha(challenge["id"], "999999", store=self.store, now=1003.0)
        self.assertIn("错误次数过多", reason)
        # 锁掉之后连正确答案也不认
        ok, reason = V.check_captcha(challenge["id"], answer_of(challenge["question"]),
                                     store=self.store, now=1004.0)
        self.assertFalse(ok)
        self.assertIn("失效", reason)

    def test_expiry(self) -> None:
        challenge = V.new_captcha(store=self.store, now=1000.0)
        ok, reason = V.check_captcha(
            challenge["id"], answer_of(challenge["question"]),
            store=self.store, now=1000.0 + V.CAPTCHA_TTL + 1,
        )
        self.assertFalse(ok)
        self.assertIn("过期", reason)

    def test_empty_and_unknown_ids(self) -> None:
        self.assertFalse(V.check_captcha("", "1", store=self.store)[0])
        self.assertFalse(V.check_captcha("not-a-real-captcha", "1", store=self.store)[0])
        challenge = V.new_captcha(store=self.store)
        ok, reason = V.check_captcha(challenge["id"], "", store=self.store)
        self.assertFalse(ok)
        self.assertIn("不正确", reason)

    def test_answer_tolerates_spaces_and_trailing_equals(self) -> None:
        challenge = V.new_captcha(store=self.store, now=1.0)
        good = answer_of(challenge["question"])
        ok, reason = V.check_captcha(challenge["id"], f"  {good}  = ", store=self.store, now=2.0)
        self.assertTrue(ok, reason)

    def test_chars_kind_is_case_insensitive(self) -> None:
        challenge = V.new_captcha("chars", store=self.store, now=1000.0)
        self.assertEqual(challenge["kind"], "chars")
        match = CHARS.search(challenge["question"])
        self.assertIsNotNone(match, challenge["question"])
        code = match.group(1)
        ok, reason = V.check_captcha(challenge["id"], code.lower(), store=self.store, now=1001.0)
        self.assertTrue(ok, reason)

    def test_stores_are_isolated(self) -> None:
        first = V.CaptchaStore()
        second = V.CaptchaStore()
        challenge = V.new_captcha(store=first)
        ok, _ = V.check_captcha(challenge["id"], answer_of(challenge["question"]), store=second)
        self.assertFalse(ok, "别的 store 不该认这道题")
        self.assertTrue(V.check_captcha(challenge["id"], answer_of(challenge["question"]),
                                        store=first)[0])

    def test_max_entries_prunes_oldest(self) -> None:
        store = V.CaptchaStore(max_entries=16)
        for _ in range(40):
            V.new_captcha(store=store, now=1000.0)
        self.assertLessEqual(store.outstanding(now=1000.0), 16)

    def test_stats_and_reset(self) -> None:
        V.new_captcha(store=self.store, now=1000.0)
        V.new_captcha(store=self.store, now=1000.0)
        stats = V.captcha_stats(store=self.store, now=1000.0)
        self.assertEqual(stats["issued"], 2)
        self.assertEqual(stats["outstanding"], 2)
        self.assertEqual(stats["ttl"], V.CAPTCHA_TTL)
        V.reset_captchas(store=self.store)
        self.assertEqual(V.captcha_stats(store=self.store, now=1000.0)["outstanding"], 0)

    def test_default_store_is_used_when_not_passed(self) -> None:
        V.reset_captchas()
        challenge = V.new_captcha()
        ok, reason = V.check_captcha(challenge["id"], answer_of(challenge["question"]))
        self.assertTrue(ok, reason)
        self.assertGreaterEqual(V.captcha_stats()["issued"], 1)
        V.reset_captchas()


# --------------------------------------------------------------------------
# 2. 邮箱验证的开关(不含真实 SMTP)
# --------------------------------------------------------------------------


class TestEmailPolicy(unittest.TestCase):
    def test_not_configured_means_disabled(self) -> None:
        self.assertFalse(V.email_enabled({}))
        self.assertEqual(V.smtp_settings({}), {})
        self.assertIn("未配置发信", V.email_disabled_reason({}))
        self.assertIn("DDDB_SMTP_HOST", V.email_disabled_reason({}))

    def test_configured_means_enabled(self) -> None:
        env = {"DDDB_SMTP_HOST": "smtp.example.com", "DDDB_SMTP_FROM": "noreply@example.com"}
        self.assertTrue(V.email_enabled(env))
        settings = V.smtp_settings(env)
        self.assertEqual(settings["host"], "smtp.example.com")
        self.assertEqual(settings["port"], 587, "默认 587")
        self.assertEqual(settings["from"], "noreply@example.com")
        self.assertTrue(settings["tls"], "默认启用 STARTTLS")
        self.assertFalse(settings["ssl"])
        self.assertEqual(V.email_disabled_reason(env), "")

    def test_port_ssl_and_timeout_are_parsed(self) -> None:
        env = {
            "DDDB_SMTP_HOST": "smtp.example.com",
            "DDDB_SMTP_PORT": "465",
            "DDDB_SMTP_USER": "bot@example.com",
            "DDDB_SMTP_PASSWORD": "secret",
            "DDDB_SMTP_SSL": "1",
            "DDDB_SMTP_TLS": "0",
            "DDDB_SMTP_TIMEOUT": "3",
        }
        settings = V.smtp_settings(env)
        self.assertEqual(settings["port"], 465)
        self.assertEqual(settings["from"], "bot@example.com", "没给 FROM 时回退到 USER")
        self.assertTrue(settings["ssl"])
        self.assertFalse(settings["tls"])
        self.assertEqual(settings["timeout"], 3.0)
        self.assertEqual(settings["user"], "bot@example.com")

    def test_bad_numbers_fall_back_to_defaults(self) -> None:
        env = {"DDDB_SMTP_HOST": "h", "DDDB_SMTP_FROM": "a@b.co",
               "DDDB_SMTP_PORT": "abc", "DDDB_SMTP_TIMEOUT": "x"}
        settings = V.smtp_settings(env)
        self.assertEqual(settings["port"], 587)
        self.assertEqual(settings["timeout"], 10.0)

    def test_explicit_kill_switch(self) -> None:
        env = {"DDDB_SMTP_HOST": "smtp.example.com", "DDDB_SMTP_FROM": "a@b.co",
               "DDDB_EMAIL_VERIFY": "0"}
        self.assertFalse(V.email_enabled(env))
        self.assertIn("DDDB_EMAIL_VERIFY", V.email_disabled_reason(env))

    def test_host_without_sender_is_not_enough(self) -> None:
        self.assertFalse(V.email_enabled({"DDDB_SMTP_HOST": "smtp.example.com"}))
        self.assertTrue(V.smtp_settings({"DDDB_SMTP_HOST": "smtp.example.com"}) == {})

    def test_mask_email(self) -> None:
        self.assertEqual(V.mask_email("alice@example.com"), "a***e@example.com")
        self.assertEqual(V.mask_email("ab@x.io"), "a***@x.io")
        self.assertEqual(V.mask_email(""), "")
        self.assertEqual(V.mask_email("not-an-email"), "not-an-email")


# --------------------------------------------------------------------------
# 3. 邮箱验证码(注入假发信器, 不联网)
# --------------------------------------------------------------------------


class EmailCase(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_dir("verify-")
        self.root = self._ctx.__enter__()
        self.store = UserStore(self.root / "users.sqlite3")
        ok, message = self.store.create_user(
            "alice", "password-123", email="alice@example.com", email_verified=False)
        self.assertTrue(ok, message)
        self.mailer = V.RecordingMailer()

    def tearDown(self) -> None:
        try:
            self.store.close()
        finally:
            self._ctx.__exit__(None, None, None)

    def send(self, *, now: float = 1000.0, mailer=None):
        return V.send_email_code(
            self.store, "alice", mailer=mailer or self.mailer, environ={}, now=now)


class TestEmailCodes(EmailCase):
    def test_send_puts_a_six_digit_code_in_the_mail(self) -> None:
        ok, message = self.send()
        self.assertTrue(ok, message)
        self.assertEqual(len(self.mailer.outbox), 1)
        mail = self.mailer.last()
        self.assertEqual(mail["to"], "alice@example.com")
        self.assertIn("验证码", mail["subject"])
        code = self.mailer.last_code()
        self.assertEqual(len(code), V.EMAIL_CODE_DIGITS)
        self.assertTrue(code.isdigit())
        # 明文只出现在邮件里, 库里存的是哈希
        record = self.store.latest_verify_code("alice")
        self.assertNotEqual(record["code_hash"], code)
        self.assertTrue(record["code_hash"].startswith("pbkdf2_sha256$"))
        self.assertGreater(record["expires_at"], 1000.0)

    def test_correct_code_verifies_the_account(self) -> None:
        self.send()
        code = self.mailer.last_code()
        ok, message = V.check_email_code(self.store, "alice", code, now=1010.0)
        self.assertTrue(ok, message)
        self.assertTrue(self.store.get("alice")["email_verified"])
        self.assertGreater(self.store.get("alice")["verified_at"], 0)
        # 账号现在可以登录了
        self.assertTrue(self.store.authenticate("alice", "password-123").ok)

    def test_wrong_code_counts_attempts(self) -> None:
        self.send()
        code = self.mailer.last_code()
        wrong = "000000" if code != "000000" else "111111"
        ok, reason = V.check_email_code(self.store, "alice", wrong, now=1010.0)
        self.assertFalse(ok)
        self.assertIn(f"还可以再试 {V.EMAIL_CODE_MAX_ATTEMPTS - 1} 次", reason)
        self.assertFalse(self.store.get("alice")["email_verified"])
        # 之后用正确的码仍然能过
        ok, message = V.check_email_code(self.store, "alice", code, now=1020.0)
        self.assertTrue(ok, message)

    def test_attempt_limit_locks_the_code(self) -> None:
        self.send()
        for index in range(V.EMAIL_CODE_MAX_ATTEMPTS):
            ok, reason = V.check_email_code(self.store, "alice", "000001", now=1010.0 + index)
            self.assertFalse(ok)
        self.assertIn("尝试次数过多", reason)
        # 作废后连正确码也不认(得重新发送)
        code = self.mailer.last_code()
        ok, reason = V.check_email_code(self.store, "alice", code, now=1100.0)
        self.assertFalse(ok)
        self.assertIn("没有待验证的验证码", reason)

    def test_expired_code_is_rejected(self) -> None:
        self.send(now=1000.0)
        code = self.mailer.last_code()
        ok, reason = V.check_email_code(
            self.store, "alice", code, now=1000.0 + V.EMAIL_CODE_TTL + 1)
        self.assertFalse(ok)
        self.assertIn("过期", reason)
        self.assertFalse(self.store.get("alice")["email_verified"])

    def test_code_cannot_be_replayed(self) -> None:
        self.send()
        code = self.mailer.last_code()
        self.assertTrue(V.check_email_code(self.store, "alice", code, now=1010.0)[0])
        ok, reason = V.check_email_code(self.store, "alice", code, now=1011.0)
        self.assertFalse(ok)
        self.assertIn("已经验证过", reason)

    def test_resend_interval(self) -> None:
        self.assertTrue(self.send(now=1000.0)[0])
        ok, reason = self.send(now=1030.0)
        self.assertFalse(ok)
        self.assertIn("太频繁", reason)
        self.assertIn("秒后再试", reason)
        other = V.RecordingMailer()
        ok, reason = self.send(now=1000.0 + V.EMAIL_RESEND_INTERVAL + 1, mailer=other)
        self.assertTrue(ok, reason)
        self.assertEqual(len(other.outbox), 1)
        # 新码生效, 老码作废
        old_code = self.mailer.last_code()
        new_code = other.last_code()
        self.assertNotEqual(old_code, new_code)
        self.assertFalse(V.check_email_code(self.store, "alice", old_code,
                                            now=1000.0 + V.EMAIL_RESEND_INTERVAL + 5)[0])
        self.assertTrue(V.check_email_code(self.store, "alice", new_code,
                                           now=1000.0 + V.EMAIL_RESEND_INTERVAL + 10)[0])

    def test_send_without_smtp_degrades_instead_of_raising(self) -> None:
        ok, reason = V.send_email_code(self.store, "alice", environ={}, now=1000.0)
        self.assertFalse(ok)
        self.assertIn("未配置发信", reason)
        # 没有配置时, 整条注册流程不应被卡住: 账号仍然可以建成"已验证"
        ok, message = self.store.create_user("bob", "password-123", email="bob@example.com",
                                             email_verified=True)
        self.assertTrue(ok, message)
        self.assertTrue(self.store.authenticate("bob", "password-123").ok)

    def test_send_to_user_without_email_or_unknown_user(self) -> None:
        ok, message = self.store.create_user("no-mail", "password-123")
        self.assertTrue(ok, message)
        ok, reason = V.send_email_code(self.store, "no-mail", mailer=self.mailer, environ={}, now=1000.0)
        self.assertFalse(ok)
        self.assertIn("没有填写邮箱", reason)
        ok, reason = V.send_email_code(self.store, "ghost", mailer=self.mailer, environ={}, now=1000.0)
        self.assertFalse(ok)
        self.assertIn("找不到这个用户", reason)

    def test_already_verified_account_needs_no_code(self) -> None:
        self.store.mark_email_verified("alice")
        ok, reason = self.send()
        self.assertFalse(ok)
        self.assertIn("已经验证过", reason)
        ok, reason = V.check_email_code(self.store, "alice", "123456", now=1010.0)
        self.assertFalse(ok)
        self.assertIn("已经验证过", reason)

    def test_check_without_pending_code(self) -> None:
        ok, reason = V.check_email_code(self.store, "alice", "123456", now=1010.0)
        self.assertFalse(ok)
        self.assertIn("没有待验证的验证码", reason)
        ok, reason = V.check_email_code(self.store, "ghost", "123456", now=1010.0)
        self.assertFalse(ok)
        self.assertIn("找不到这个用户", reason)

    def test_mailer_failure_is_reported_and_code_is_burned(self) -> None:
        broken = V.RecordingMailer(fail=V.MailerError("smtp down"))
        ok, reason = self.send(mailer=broken, now=1000.0)
        self.assertFalse(ok)
        self.assertIn("邮件发送失败", reason)
        self.assertIsNone(self.store.latest_verify_code("alice"),
                          "发不出去就不能留一个没人知道的'有效码'")
        # 失败后立刻可以重试(不留 60 秒冷却)
        ok, message = self.send(now=1001.0)
        self.assertTrue(ok, message)

    def test_email_verification_state_for_pages(self) -> None:
        state = V.email_verification_state(self.store, "alice", environ={})
        self.assertFalse(state["enabled"])
        self.assertFalse(state["verified"])
        self.assertTrue(state["has_email"])
        self.assertEqual(state["email"], "a***e@example.com")
        self.assertIn("SMTP", state["reason"])
        self.store.mark_email_verified("alice")
        state = V.email_verification_state(self.store, "alice", environ={})
        self.assertTrue(state["verified"])
        # 配置了 SMTP 之后 required 才是 True(此时邮箱已验证)
        env = {"DDDB_SMTP_HOST": "smtp.example.com", "DDDB_SMTP_FROM": "a@b.co"}
        state = V.email_verification_state(self.store, "alice", environ=env)
        self.assertTrue(state["enabled"])
        self.assertTrue(state["required"])


# --------------------------------------------------------------------------
# 4. 端到端: 注册(验证码 + 邮箱验证) → 登录
# --------------------------------------------------------------------------


class TestRegistrationFlow(unittest.TestCase):
    """按"页面接线"的顺序跑一遍, 证明后端契约齐全可用(不涉及 HTTP 层)."""

    def setUp(self) -> None:
        self._ctx = temp_dir("register-")
        self.root = self._ctx.__enter__()
        self.store = UserStore(self.root / "users.sqlite3")
        self.captchas = V.CaptchaStore()

    def tearDown(self) -> None:
        try:
            self.store.close()
        finally:
            self._ctx.__exit__(None, None, None)

    def test_register_with_captcha_and_email_verification(self) -> None:
        mailer = V.RecordingMailer()
        challenge = V.new_captcha(store=self.captchas, now=1000.0)
        # 1) 验证码错了 -> 拒绝建号
        ok, reason = V.check_captcha(challenge["id"], "0", store=self.captchas, now=1001.0)
        self.assertFalse(ok)
        # 2) 换一道, 答对 -> 建号(带邮箱, 待验证)
        challenge = V.new_captcha(store=self.captchas, now=1002.0)
        ok, reason = V.check_captcha(challenge["id"], answer_of(challenge["question"]),
                                     store=self.captchas, now=1003.0)
        self.assertTrue(ok, reason)
        ok, message = self.store.create_user(
            "carol", "password-123", email="carol@example.com", email_verified=False)
        self.assertTrue(ok, message)
        # 3) 邮箱未验证 -> 登录被拒(原因明确)
        # 行为变更: 未验证也能登录, 只是 user 里 email_verified=False
        # (站点层据此只给访客权限并提示验证; 超过 24 小时未验证会被删除)
        result = self.store.authenticate("carol", "password-123")
        self.assertTrue(result.ok, result.message)
        self.assertFalse((result.user or {}).get("email_verified"))
        # 4) 发码 + 校验
        ok, message = V.send_email_code(self.store, "carol", mailer=mailer, environ={}, now=1010.0)
        self.assertTrue(ok, message)
        ok, message = V.check_email_code(self.store, "carol", mailer.last_code(), now=1020.0)
        self.assertTrue(ok, message)
        # 5) 现在能登录, 且默认是普通用户(要去申请编辑者)
        result = self.store.authenticate("carol", "password-123")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.user["role"], ROLE_VIEWER)
        self.assertFalse(result.user["can_write"])

    def test_captcha_is_required_on_the_request(self) -> None:
        """没带/带错 captcha_id 一律不放行(页面据此拦在"建号"之前)."""
        self.assertFalse(V.check_captcha("", "1", store=self.captchas)[0])
        self.assertFalse(V.check_captcha("forged-id", "1", store=self.captchas)[0])

    def test_email_disabled_flow_still_registers(self) -> None:
        """没配 SMTP: 只有验证码也能注册, 而且账号直接可用."""
        challenge = V.new_captcha(store=self.captchas, now=1000.0)
        self.assertTrue(V.check_captcha(challenge["id"], answer_of(challenge["question"]),
                                       store=self.captchas, now=1001.0)[0])
        self.assertFalse(V.email_enabled({}))
        # 页面应当在"邮箱验证未启用"时按已验证建号
        ok, message = self.store.create_user(
            "dave", "password-123", email="dave@example.com", email_verified=not V.email_enabled({}))
        self.assertTrue(ok, message)
        self.assertTrue(self.store.authenticate("dave", "password-123").ok)


# --------------------------------------------------------------------------
# 5. SMTP 发信器的构造(不真的连服务器)
# --------------------------------------------------------------------------


class TestMailerConstruction(unittest.TestCase):
    def test_smtp_mailer_requires_settings(self) -> None:
        with self.assertRaises(V.MailerError):
            V.SmtpMailer({})
        with self.assertRaises(V.MailerError):
            V.SmtpMailer(environ={})

    def test_mailer_from_env_returns_none_when_unconfigured(self) -> None:
        self.assertIsNone(V.mailer_from_env({}))

    def test_mailer_from_env_builds_with_settings(self) -> None:
        mailer = V.mailer_from_env(
            {"DDDB_SMTP_HOST": "smtp.example.com", "DDDB_SMTP_FROM": "a@b.co"})
        self.assertIsInstance(mailer, V.SmtpMailer)
        assert mailer is not None
        self.assertEqual(mailer.settings["host"], "smtp.example.com")

    def test_recording_mailer_helpers(self) -> None:
        mailer = V.RecordingMailer()
        mailer.send("x@y.z", "主题", "验证码 123456 请查收")
        self.assertEqual(mailer.last_code(), "123456")
        self.assertEqual(mailer.last()["to"], "x@y.z")
        mailer.clear()
        self.assertIsNone(mailer.last())
        self.assertEqual(mailer.last_code(), "")
        # 6 位数字的边界: 别把别的数字串误当验证码
        noisy = V.RecordingMailer()
        noisy.send("x@y.z", "主题", "订单号 12345, 验证码 654321")
        self.assertEqual(noisy.last_code(), "654321")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()