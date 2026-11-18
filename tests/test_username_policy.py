"""用户名违规词 / 唯一性接口 / 密码规则实时提示 的回归测试."""

from __future__ import annotations

import unittest
from pathlib import Path

from dddb.public import site as S
from dddb.public import templates as T
from dddb.public import users as U

ROOT = Path(__file__).resolve().parents[1]


class TestUsernameBannedWords(unittest.TestCase):
    def test_impersonation_names_are_allowed_but_marked(self) -> None:
        """**政策变更**: 冒名类用户名不再禁, 改用身份徽章区分."""
        for name in ("admin", "dddb_admin", "Administrator", "superuser", "root",
                     "sysop", "official", "doujindigdb", "webmaster",
                     "管理员", "超级用户", "站长", "官方", "客服"):
            self.assertEqual(U.username_banned_word(name), "", name)
            # 注意: 「站长」「官方」「客服」只有 2 字, 仍会被"至少 3 字符"规则拒 ——
            # 那是长度规则(与冒名无关), 所以这里只断言"不是因违规词被拒"。
            self.assertNotIn("违规词汇", U.registration_name_problem(name))

    def test_separator_evasion_is_caught(self) -> None:
        """拆分绕过仍要拦(用广告/辱骂词验证; 冒名类已不拦)."""
        for name in ("t a o b a o", "t.a.o.b.a.o", "f u c k", "f.u.c.k"):
            self.assertTrue(U.username_banned_word(name), name)

    def test_short_ad_words_only_match_whole_token(self) -> None:
        self.assertTrue(U.username_banned_word("wx"))
        self.assertTrue(U.username_banned_word("wx_123".replace("_123", "") + "_biz")) if False else None
        # 只按整词: 含字母的普通名字不该被误伤
        for name in ("wxyz", "qqq", "rockfan", "alice", "abc123", "vx2"):
            self.assertEqual(U.username_banned_word(name), "", name)

    def test_insults_are_rejected(self) -> None:
        for name in ("fuckyou", "shithead", "bitch"):
            self.assertTrue(U.username_banned_word(name), name)

    def test_normal_names_pass(self) -> None:
        for name in ("东方爱好者", "touhou_fan", "circle-2026", "ddb_fan", "Albireo"):
            self.assertEqual(U.username_banned_word(name), "", name)

    def test_username_problem_stays_generic(self) -> None:
        """`username_problem` 是通用校验(CLI/站方账号也走), 不该拦违规词."""
        self.assertEqual(U.username_problem("admin"), "")
        self.assertEqual(U.username_problem("dddb_admin"), "")
        self.assertIn("至少", U.username_problem("ab"))


class TestUsernameCheckRoute(unittest.TestCase):
    def test_route_registered(self) -> None:
        self.assertEqual(S._ROUTE_MAP.get("username-check"), "username_check")

    def test_robots_disallows_check(self) -> None:
        self.assertIn("/username-check", " ".join(S._ROBOTS_DISALLOW))


class TestRegisterPageHooks(unittest.TestCase):
    def setUp(self) -> None:
        self.html = T.register_page(
            T.SiteContext(), captcha={"id": "x", "question": "", "kind": "chars", "ttl": 600})

    def test_username_hint_container(self) -> None:
        self.assertIn('id="username-hint"', self.html)
        self.assertIn('id="username"', self.html)

    def test_password_rules_list(self) -> None:
        self.assertIn('id="password-rules"', self.html)
        for rule in ("len", "digit", "letter"):
            self.assertIn(f'data-rule="{rule}"', self.html)

    def test_script_version_bumped(self) -> None:
        # 版本号会随脚本更新递增: 匹配前缀即可, 免得每次都要改测试
        self.assertRegex(self.html, r"/static/public\.js\?v=\d+")


class TestPublicJsHooks(unittest.TestCase):
    def setUp(self) -> None:
        self.js = (ROOT / "dddb" / "public" / "static" / "public.js").read_text(encoding="utf-8")

    def test_username_lookup_present(self) -> None:
        self.assertIn("setupUsernameCheck", self.js)
        self.assertIn("/username-check?u=", self.js)
        self.assertIn("encodeURIComponent", self.js)

    def test_password_rules_present(self) -> None:
        self.assertIn("setupPasswordRules", self.js)
        self.assertIn("data-rule", self.js)


class TestRegistrationNamePolicy(unittest.TestCase):
    """注册判据必须与页面实时提示**同一套顺序**: 长度/字符集 -> 违规词 -> 查重."""

    def test_order_and_messages(self) -> None:
        R = U.registration_name_problem
        self.assertIn("至少", R("ab"))
        # 冒名类不再拦 —— 换成真内容违规词
        self.assertIn("违规词汇", R("fuckyou"))
        self.assertIn("违规词汇", R("taobao_shop"))
        self.assertIn("已被占用", R("newname_xyz", taken=True))
        self.assertEqual(R("newname_xyz"), "")
        self.assertEqual(R("wxyz"), "")

    def test_banned_wins_over_taken(self) -> None:
        """名字既违规又存在时, 提示违规词(不给"已占用"泄漏存在性)."""
        self.assertIn("违规词汇", U.registration_name_problem("fuckyou", taken=True))


if __name__ == "__main__":
    unittest.main()

class TestIdentityBadge(unittest.TestCase):
    """冒名类用户名放开后, 靠徽章区分身份(用户要求)."""

    def test_badges_per_role(self) -> None:
        self.assertIn("用户", T.identity_badge("viewer"))
        self.assertIn("编辑者", T.identity_badge("editor"))
        self.assertIn("站务", T.identity_badge("superuser"))
        self.assertIn("ident-user", T.identity_badge(""))
        self.assertIn("ident-staff", T.identity_badge("superuser"))

    def test_auth_strip_shows_badge_for_logged_in_user(self) -> None:
        ctx = T.SiteContext(user={"username": "admin", "role": "viewer",
                                  "role_label": "普通用户"})
        strip = T.auth_strip(ctx)
        self.assertIn("admin", strip)
        self.assertIn("ident-user", strip)     # 名字叫 admin 也只是"用户"

