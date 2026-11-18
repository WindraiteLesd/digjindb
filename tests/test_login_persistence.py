"""测试: 换 IP 不再被当成新环境; 换设备才提示; 会话滑动续期不失效."""

from __future__ import annotations

import time
import unittest
from pathlib import Path

from tests._tmp import temp_dir
from dddb.public import auth as A
from dddb.public import site as S


class TestEnvFingerprint(unittest.TestCase):
    """环境指纹只认设备, 换网络(IP 变)不算新环境."""

    def _hash(self, ua: str, lang: str = "zh-CN", ip: str = "1.1.1.1") -> str:
        return S.PublicSite._env_hash({
            "user-agent": ua, "accept-language": lang,
            "x-forwarded-for": ip, "x-real-ip": ip,
        })

    def test_same_device_different_ip_is_same_env(self) -> None:
        ua = "Mozilla/5.0 (Windows NT 10.0) Chrome/120"
        self.assertEqual(self._hash(ua, ip="1.1.1.1"), self._hash(ua, ip="8.8.8.8"),
                         "换 IP 不该被判定成新环境")

    def test_different_device_is_new_env(self) -> None:
        first = self._hash("Mozilla/5.0 (Windows NT 10.0) Chrome/120")
        second = self._hash("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Safari/604")
        self.assertNotEqual(first, second, "换设备应当被识别")

    def test_missing_ua_is_lenient(self) -> None:
        self.assertEqual(self._hash(""), "", "没有 UA 时不触发新环境判定")

    def test_fingerprint_stable_across_calls(self) -> None:
        ua = "Mozilla/5.0 (Macintosh) Safari/605"
        self.assertEqual(self._hash(ua), self._hash(ua))


class TestSessionPersistence(unittest.TestCase):
    def test_ttl_is_thirty_days(self) -> None:
        self.assertGreaterEqual(A.SESSION_TTL, 30 * 86400)
        self.assertGreaterEqual(A.SESSION_MAX_LIFE, 180 * 86400)

    def test_滑动续期延长过期时间(self) -> None:
        manager = A.SessionManager()
        session = manager.create({"id": 1, "username": "a", "role": "viewer"},
                                 now=1000.0)
        first_expiry = session.expires_at
        manager.get(session.sid, now=1000.0 + 3600)
        self.assertGreater(session.expires_at, first_expiry, "活跃使用应延长会话")

    def test_长期不用仍会过期(self) -> None:
        manager = A.SessionManager(ttl=100.0, max_life=1000.0)
        session = manager.create({"id": 2, "username": "b", "role": "viewer"}, now=0.0)
        self.assertIsNone(manager.get(session.sid, now=500.0), "超过 TTL 应失效")

    def test_total_lifetime_cap(self) -> None:
        """持续活跃也要受总寿命(max_life)约束, 不能无限续期."""
        manager = A.SessionManager(ttl=100.0, max_life=300.0)
        session = manager.create({"id": 3, "username": "c", "role": "viewer"}, now=0.0)
        # 每 80 秒访问一次(都在 TTL 内), 会话应一直有效
        self.assertIsNotNone(manager.get(session.sid, now=80.0))
        self.assertIsNotNone(manager.get(session.sid, now=160.0))
        self.assertIsNotNone(manager.get(session.sid, now=240.0))
        # 超过 max_life(300s) 必须失效, 不能再靠活跃续命
        self.assertIsNone(manager.get(session.sid, now=310.0),
                          "超过总寿命应失效")


class TestLoginDoesNotRenewPrompt(unittest.TestCase):
    """同一设备重复登录, 不应产生"新环境"提示."""

    def test_repeated_login_same_device(self) -> None:
        ctx = temp_dir("env-")
        root = Path(ctx.__enter__())
        try:
            from dddb.public.users import UserStore
            store = UserStore(root / "u.sqlite3", iterations=1000)
            store.create_user("env_user", "password-123", email="e@example.com",
                              email_verified=True)
            ua = "Mozilla/5.0 (Windows NT 10.0) Chrome/120"
            first = S.PublicSite._env_hash({"user-agent": ua, "x-forwarded-for": "1.1.1.1"})
            store.set_last_env("env_user", first)
            # 同样设备, 不同 IP —— 指纹应一致, 不会触发新环境
            again = S.PublicSite._env_hash({"user-agent": ua, "x-forwarded-for": "9.9.9.9"})
            self.assertEqual(store.last_env("env_user"), again)
            store.close()
        finally:
            ctx.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()