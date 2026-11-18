"""本轮新增能力的回归测试: 图形验证码 / 邀请码(编辑者可生成) / 注册页文案守卫."""

from __future__ import annotations

import unittest

from dddb.public import site as S
from dddb.public import templates as T
from dddb.public import users as U
from dddb.public import verify as V
from tests._tmp import temp_dir


class TestGraphicCaptcha(unittest.TestCase):
    def setUp(self) -> None:
        self.store = V.CaptchaStore(ttl=600, max_attempts=3)

    def _new(self, **kwargs):
        return V.new_captcha("chars", store=self.store, **kwargs)

    def test_graphic_hides_question(self) -> None:
        """图形题**不能**把答案放进 question(否则页面就有明文了)."""
        cap = V.new_captcha("chars", store=self.store, graphic=True)
        self.assertEqual(cap["question"], "")
        self.assertIn("id", cap)

    def test_non_graphic_keeps_question(self) -> None:
        cap = self._new()
        self.assertTrue(cap["question"], "非图形题应保留题面")

    def test_svg_contains_no_plaintext(self) -> None:
        cap = V.new_captcha("chars", store=self.store, graphic=True)
        answer = self.store.peek(cap["id"])["answer"]
        svg = V.captcha_svg(cap["id"], store=self.store)
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("</svg>", svg)
        # 答案被拆成单字符 <text>: 整串不应原样出现
        self.assertNotIn(answer, svg)
        # 干扰元素
        self.assertIn("<circle", svg)
        self.assertIn("<path", svg)

    def test_svg_404_material_for_unknown_id(self) -> None:
        self.assertEqual(V.captcha_svg("nope", store=self.store), "")

    def test_svg_for_expired_captcha_is_empty(self) -> None:
        cap = V.new_captcha("chars", store=self.store, graphic=True, now=1000.0)
        self.assertEqual(V.captcha_svg(cap["id"], store=self.store, now=1000.0 + 601), "")

    def test_peek_does_not_consume(self) -> None:
        """渲染 SVG 用 peek, 不能把验证码吃掉(否则用户提交就永远错)."""
        cap = V.new_captcha("chars", store=self.store, graphic=True)
        answer = self.store.peek(cap["id"])["answer"]
        self.assertEqual(V.captcha_svg(cap["id"], store=self.store).count("<svg"), 1)
        ok, reason = V.check_captcha(cap["id"], answer, store=self.store)
        self.assertTrue(ok, reason)

    def test_answer_is_case_insensitive(self) -> None:
        cap = V.new_captcha("chars", store=self.store, graphic=True)
        answer = self.store.peek(cap["id"])["answer"]
        ok, _ = V.check_captcha(cap["id"], answer.lower(), store=self.store)
        self.assertTrue(ok)


class TestInviteCodes(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_dir("invites-")
        self.root = self._ctx.__enter__()
        self.store = U.UserStore(self.root / "users.sqlite3", iterations=1000)

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def test_editor_can_create_invite_with_owner(self) -> None:
        self.assertTrue(U.can_create_invite("editor"))
        self.assertTrue(U.can_create_invite("superuser"))
        self.assertFalse(U.can_create_invite("viewer"))
        code = self.store.create_invite(note="给社团A", created_by="alice")
        info = self.store.invite_info(code)
        self.assertEqual(info["created_by"], "alice")
        self.assertEqual(info["note"], "给社团A")
        self.assertFalse(info["used"])

    def test_list_invites_filters_by_creator(self) -> None:
        mine = self.store.create_invite(created_by="alice")
        self.store.create_invite(created_by="bob")
        own = self.store.list_invites(created_by="alice")
        self.assertEqual([item["code"] for item in own], [mine])
        self.assertEqual(len(self.store.list_invites()), 2)

    def test_invite_stats(self) -> None:
        first = self.store.create_invite(created_by="alice")
        self.store.create_invite(created_by="alice")
        self.store.create_user("carol", "password-1")
        self.store.redeem_invite(first, "carol")
        stats = self.store.invite_stats(created_by="alice")
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["used"], 1)
        self.assertEqual(stats["unused"], 1)

    def test_invite_is_single_use(self) -> None:
        code = self.store.create_invite(created_by="alice")
        self.store.create_user("dave", "password-1")
        self.assertTrue(self.store.redeem_invite(code, "dave"))
        self.store.create_user("erin", "password-1")
        self.assertFalse(self.store.redeem_invite(code, "erin"))


class TestRegisterCopy(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = T.SiteContext()
        self.register = T.register_page(
            self.ctx, captcha={"id": "abc", "question": "", "kind": "chars", "ttl": 600})
        self.login = T.login_page(self.ctx)

    def test_subtitle_matches_request(self) -> None:
        self.assertIn("注册后默认为普通用户", self.register)
        self.assertIn("填写邀请码", self.register)
        self.assertIn("8 位数字加英文字母", self.register)

    def test_old_hints_removed(self) -> None:
        self.assertNotIn("不要用别的站点用过的密码", self.register)
        self.assertNotIn("再输一次", self.register)
        self.assertNotIn("管理员发放的一次性码", self.register)

    def test_confirm_password_and_realtime_hint(self) -> None:
        self.assertIn("确认密码", self.register)
        self.assertIn('id="password2"', self.register)
        self.assertIn('id="password2-error"', self.register)
        self.assertIn('id="password"', self.register)

    def test_invite_hint_wording(self) -> None:
        self.assertIn("填写邀请码无需审核即可成为编辑者", self.register)

    def test_graphic_captcha_markup(self) -> None:
        self.assertIn("/captcha.svg?id=abc", self.register)
        self.assertNotIn("captcha-question", self.register)

    def test_email_extra_description_gone(self) -> None:
        with_email = T.register_page(
            self.ctx, captcha={"id": "abc", "question": "", "kind": "chars", "ttl": 600},
            email_enabled=True)
        self.assertNotIn("用于接收 6 位验证码", with_email)

    def test_login_subtitle_removed(self) -> None:
        self.assertNotIn("登录后可以查看", self.login)


class TestRoutes(unittest.TestCase):
    def test_captcha_and_invites_routes_registered(self) -> None:
        self.assertEqual(S._ROUTE_MAP.get("captcha.svg"), "captcha_svg")
        self.assertEqual(S._ROUTE_MAP.get("invites"), "invites")

    def test_robots_disallows_new_paths(self) -> None:
        text = " ".join(S._ROBOTS_DISALLOW)
        self.assertIn("/invites", text)
        self.assertIn("/captcha.svg", text)


class TestPublicJsIsServed(unittest.TestCase):
    """回归: `public.js` 一度**不在 STATIC_FILES**, 于是线上 /static/public.js 404,
    确认密码的实时校验根本没生效(页面有 <script> 但拿不到文件)。
    """

    def test_public_js_is_listed_as_static(self) -> None:
        self.assertIn("public.js", S.STATIC_FILES)
        # 模板引用带版本号: 静态资源被 CF/浏览器缓存 1 天
        page = T.layout(T.SiteContext(), title="x", body="")
        self.assertIn("/static/public.js?v=", page)

    def test_public_js_exists_and_checks_confirm_password(self) -> None:
        from pathlib import Path as _Path
        js = (_Path(__file__).resolve().parents[1]
              / "dddb" / "public" / "static" / "public.js").read_text(encoding="utf-8")
        self.assertIn("password2-error", js)
        self.assertIn("两次输入的密码不一致", js)


if __name__ == "__main__":
    unittest.main()