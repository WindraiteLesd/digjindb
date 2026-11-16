"""用户系统测试: 口令哈希 / 角色鉴权 / 会话 / CSRF / 403 / 写路径落库 / 页面文案.

分五层:

1. 纯函数(口令哈希与校验、用户名/口令校验、角色规整);
2. :class:`dnh.public.users.UserStore` 对**独立的**用户 sqlite 文件;
3. :mod:`dnh.public.auth` 的会话表 / Cookie / CSRF 签名 / 同源判定;
4. 真起一个 HTTP 服务(端口 0)走完整链路: 注册 -> 登录 -> 编辑者改专辑 ->
   用 ``Store.get_album`` 复核字段真的变了 -> 还原; 以及未登录/普通用户写 -> 403;
5. 三处前端展示修复的结构断言(卡片标签换行 / 展会索引类型列网格 / 曲目表"共 N 首"方向)。

临时目录用 :func:`tests._tmp.temp_dir`(Windows 沙箱下系统 TEMP 不可写)。
"""

from __future__ import annotations

import http.cookiejar
import re
import sqlite3
import threading
import unittest

from dnh.public import verify
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dnh.db import Store
from dnh.models import Album, Credit, Track
from dnh.public import auth
from dnh.public import templates as T
from dnh.public.data import PublicData, ReadOnlyStore
from dnh.public.site import (
    PublicSite,
    form_display_values,
    form_values,
    make_server,
    parse_album_form,
    parse_duration,
    parse_route,
)
from dnh.public.users import (
    ROLE_EDITOR,
    ROLE_VIEWER,
    UserStore,
    can_write,
    clean_role,
    default_users_path,
    hash_password,
    password_problem,
    role_label,
    username_problem,
    verify_password,
)
from tests._tmp import temp_dir

#: 页面 HTML 里绝对不允许出现的东西(站长端 / 数据源)
FORBIDDEN_WORDS = (
    "站长端", "admin-url", "DEFAULT_ADMIN_URL", "8787", "admin-link",
    "dojinmusic", "dojin-music", "bangumi", "bgm.tv", "vgmdb", "doujinstyle",
    "dizzylab", "discogs", "touhoudb", "thwiki", "来源构成", "来源",
)


def make_album(title: str, **over: object) -> Album:
    album = Album(
        title=title,
        title_original=str(over.get("title_original", title)),
        circle=str(over.get("circle", "")),
        artists=list(over.get("artists", []) or []),
        release_date=str(over.get("release_date", "")),
        event=str(over.get("event", "")),
        catalog_no=str(over.get("catalog_no", "")),
        genres=list(over.get("genres", []) or []),
        tags=list(over.get("tags", []) or []),
        description=str(over.get("description", "")),
        tracks=list(over.get("tracks", []) or []),
        credits=list(over.get("credits", []) or []),
        urls=dict(over.get("urls", {}) or {}),
        source_ids=dict(over.get("source_ids", {}) or {}),
    )
    album.key = album.compute_key()
    return album


class Fixture:
    """临时目录里的专辑库 + 用户库 + 站点对象."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.covers = root / "data" / "covers"
        self.covers.mkdir(parents=True, exist_ok=True)
        self.db_path = root / "data" / "dnh.sqlite3"
        self.users_path = root / "data" / "public_users.sqlite3"
        self.store = Store(self.db_path)
        self.album = make_album(
            "幻想郷の記憶",
            circle="ShibayanRecords",
            artists=["坂上なち"],
            release_date="2026-10-25",
            event="M3-2026秋",
            catalog_no="SBCD-042",
            tags=["東方Project", "Vocal", "Hardcore", "Ambient", "Piano"],
            genres=["Vocal"],
            description="一张用来测试的专辑。",
            tracks=[Track(title="Opening", index=1, duration=83),
                    Track(title="幻想郷の記憶", index=2, duration=245)],
            credits=[Credit(name="ShibayanRecords", role="circle"),
                     Credit(name="坂上なち", role="vocal")],
            urls={"vgmdb": "https://vgmdb.net/album/101",
                  "special": "https://crimson.example.com/"},
            source_ids={"vgmdb": "101"},
        )
        self.store.upsert_album(self.album)
        self.reference = make_album(
            "Neon Drive",
            circle="ROUNDEX RECORD",
            release_date="2026-04-26",
            event="M3-2026春",
            tags=["Hardcore"],
            source_ids={"dojinmusic": "202"},
        )
        self.store.upsert_album(self.reference)
        self.second = make_album(
            "Crimson Lotus",
            circle="蝶々結び",
            event="C108",
            source_ids={"bangumi": "303"},
        )
        self.store.upsert_album(self.second)
        self.data = PublicData(
            ReadOnlyStore(self.db_path), root=root, covers_dir=self.covers,
        )
        self.users = UserStore(self.users_path)
        self.sessions = auth.SessionManager()

    def site(self, **kwargs: object) -> PublicSite:
        return PublicSite(
            self.data, users=self.users, sessions=self.sessions, db_path=self.db_path, **kwargs
        )

    def close(self) -> None:
        self.store.close()
        self.users.close()


# --------------------------------------------------------------------------
# 1. 口令哈希与校验(纯函数)
# --------------------------------------------------------------------------


class TestPasswordHashing(unittest.TestCase):
    def test_hash_is_not_plaintext_and_is_self_describing(self) -> None:
        hashed = hash_password("correct horse battery")
        self.assertNotIn("correct horse battery", hashed)
        algo, rounds, salt, digest = hashed.split("$")
        self.assertEqual(algo, "pbkdf2_sha256")
        self.assertGreaterEqual(int(rounds), 100_000, "迭代次数不能太低")
        self.assertTrue(salt and digest)

    def test_same_password_gets_different_salt(self) -> None:
        first = hash_password("same-password-here")
        second = hash_password("same-password-here")
        self.assertNotEqual(first, second)

    def test_verify_accepts_correct_and_rejects_wrong(self) -> None:
        hashed = hash_password("s3cret-password")
        self.assertTrue(verify_password("s3cret-password", hashed))
        self.assertFalse(verify_password("s3cret-passwore", hashed))
        self.assertFalse(verify_password("", hashed))
        self.assertFalse(verify_password("s3cret-password", None))

    def test_verify_rejects_malformed_stored_values(self) -> None:
        for broken in ("", "plaintext", "pbkdf2_sha256$only-two", "bcrypt$1$x$y",
                       "pbkdf2_sha256$0$c2FsdA$ZGlnZXN0", "pbkdf2_sha256$abc$c2FsdA$aGFzaA"):
            self.assertFalse(verify_password("whatever", broken), broken)

    def test_custom_iterations_and_salt_are_honoured(self) -> None:
        hashed = hash_password("pw-for-testing", salt=b"0123456789abcdef", iterations=1000)
        self.assertIn("$1000$", hashed)
        self.assertTrue(verify_password("pw-for-testing", hashed))
        # 换了迭代次数的存档仍然可校验(格式自带参数)
        self.assertFalse(verify_password("pw-for-testing", hashed.replace("$1000$", "$1001$")))

    def test_stored_hash_never_round_trips_a_plaintext(self) -> None:
        hashed = hash_password("plain-text-secret")
        self.assertNotEqual(hashed, "plain-text-secret")
        self.assertNotIn("plain-text-secret", hashed)


class TestCredentialValidation(unittest.TestCase):
    def test_password_problem(self) -> None:
        """**新策略**: 至少 8 位 + 含数字 + 含英文字母(用户要求)。"""
        from dnh.public.users import password_problem
        self.assertEqual(password_problem("abc12345"), "")
        self.assertEqual(password_problem("PASSWORD9"), "")
        self.assertIn("位", password_problem("abc1234"))          # 太短
        self.assertIn("数字", password_problem("abcdefgh"))        # 无数字
        self.assertIn("英文字母", password_problem("12345678"))    # 无字母
        self.assertIn("英文字母", password_problem("密码12345678"))  # 中文不算字母
        self.assertIn("空白", password_problem(" abc12345"))       # 首尾空白
        self.assertIn("密码", password_problem(""))                # 空

    def test_username_problem(self) -> None:
        self.assertEqual(username_problem("alice"), "")
        self.assertIn("请输入", username_problem("   "))
        self.assertIn("至少", username_problem("ab"))
        self.assertIn("最长", username_problem("x" * 40))
        self.assertIn("不能包含", username_problem("a b c"))
        self.assertIn("不能包含", username_problem("a/b"))

    def test_role_helpers(self) -> None:
        self.assertEqual(clean_role("editor"), ROLE_EDITOR)
        self.assertEqual(clean_role("EDITOR"), ROLE_EDITOR)
        self.assertEqual(clean_role("admin"), ROLE_VIEWER)       # 未知角色降级
        self.assertEqual(clean_role(None), ROLE_VIEWER)
        self.assertTrue(can_write("editor"))
        self.assertFalse(can_write("viewer"))
        self.assertFalse(can_write(""))
        self.assertEqual(role_label("editor"), "编辑者")
        self.assertEqual(role_label("viewer"), "普通用户")

    def test_default_users_path_is_separate_from_album_db(self) -> None:
        path = default_users_path(Path("E:/tmp/project"))
        self.assertEqual(path.name, "public_users.sqlite3")
        self.assertNotIn("dnh.sqlite3", path.name)


# --------------------------------------------------------------------------
# 2. 用户存储(独立文件)
# --------------------------------------------------------------------------


class TestUserStore(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_dir("users-")
        self.root = self._ctx.__enter__()
        self.store = UserStore(self.root / "public_users.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self._ctx.__exit__(None, None, None)

    def test_schema_is_exactly_the_documented_columns(self) -> None:
        # 角色/邮箱验证扩展后 users 表追加了 4 列; **原有 6 列仍在原位、顺序不变**
        # (老库靠 ALTER TABLE 逐列追加, 这是向后兼容的硬要求)。
        original_six = ["id", "username", "password_hash", "role", "created_at", "last_login"]
        columns = self.store.columns()
        self.assertEqual(columns[: len(original_six)], original_six)
        self.assertEqual(
            columns[len(original_six):],
            ["email", "email_verified", "verified_at", "is_active", "last_env_hash"],
        )

    def test_create_and_get_user(self) -> None:
        ok, message = self.store.create_user("alice", "password-1")
        self.assertTrue(ok, message)
        user = self.store.get("alice")
        assert user is not None
        self.assertEqual(user["username"], "alice")
        self.assertEqual(user["role"], ROLE_VIEWER)
        self.assertEqual(user["last_login"], 0.0)
        # 存储层**绝不**把口令哈希交出去
        self.assertNotIn("password_hash", user)
        self.assertNotIn("password_hash", self.store.list_users()[0])

    def test_username_uniqueness_is_case_insensitive(self) -> None:
        self.store.create_user("alice", "password-1")
        ok, message = self.store.create_user("ALICE", "password-2")
        self.assertFalse(ok)
        self.assertIn("占用", message)
        self.assertEqual(self.store.count(), 1)
        self.assertIsNotNone(self.store.get("Alice"))     # 大小写都能查到同一个人

    def test_create_rejects_bad_credentials(self) -> None:
        ok, message = self.store.create_user("ab", "password-1")
        self.assertFalse(ok)
        self.assertIn("至少", message)
        ok, message = self.store.create_user("bob", "short")
        self.assertFalse(ok)
        self.assertIn("至少", message)
        self.assertEqual(self.store.count(), 0)

    def test_check_login_updates_last_login_and_rejects_wrong_password(self) -> None:
        self.store.create_user("carol", "password-1")
        self.assertIsNone(self.store.check_login("carol", "wrong-password"))
        self.assertIsNone(self.store.check_login("nobody", "password-1"))
        user = self.store.check_login("carol", "password-1", now=1_700_000_000)
        assert user is not None
        self.assertEqual(user["last_login"], 1_700_000_000)
        self.assertEqual(self.store.get("carol")["last_login"], 1_700_000_000)

    def test_set_password_set_role_and_remove(self) -> None:
        self.store.create_user("dave", "password-1")
        self.assertTrue(self.store.set_password("dave", "password-2")[0])
        self.assertIsNone(self.store.check_login("dave", "password-1"))
        self.assertIsNotNone(self.store.check_login("dave", "password-2"))
        self.assertTrue(self.store.set_role("dave", ROLE_EDITOR)[0])
        self.assertEqual(self.store.get("dave")["role"], ROLE_EDITOR)
        self.assertTrue(self.store.remove("dave")[0])
        self.assertIsNone(self.store.get("dave"))
        self.assertFalse(self.store.remove("dave")[0])

    def test_editor_role_must_be_granted_not_chosen(self) -> None:
        """默认注册一律 viewer; editor 只能由邀请码或管理命令给出."""
        self.store.create_user("erin", "password-1")
        self.assertEqual(self.store.get("erin")["role"], ROLE_VIEWER)
        self.store.create_user("frank", "password-1", role="root")   # 非法角色
        self.assertEqual(self.store.get("frank")["role"], ROLE_VIEWER)

    def test_invite_codes_are_single_use(self) -> None:
        code = self.store.create_invite(note="给编辑者")
        self.assertFalse(self.store.invite_info(code)["used"])
        self.assertTrue(self.store.redeem_invite(code, "someone"))
        self.assertTrue(self.store.invite_info(code)["used"])
        self.assertFalse(self.store.redeem_invite(code, "other"), "一次性码不能复用")
        self.assertFalse(self.store.redeem_invite("not-a-real-code", "someone"))
        self.assertEqual(len(self.store.list_invites()), 1)

    def test_users_live_in_a_separate_database_file(self) -> None:
        """核心约束: 用户表**不在**专辑主库 schema 里."""
        album_db = self.root / "dnh.sqlite3"
        album_store = Store(album_db)
        album_store.upsert_album(make_album("分离测试"))
        albums_path = Path(album_store.path)
        album_store.close()

        conn = sqlite3.connect(str(albums_path))
        try:
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertIn("albums", names)
        self.assertNotIn("users", names, "主库里不该有 users 表")
        self.assertNotEqual(self.store.path, albums_path)
        self.assertTrue(self.store.path.name.startswith("public_users"))

    def test_missing_parent_directory_is_created(self) -> None:
        nested = self.root / "deep" / "deeper" / "users.sqlite3"
        store = UserStore(nested)
        try:
            self.assertTrue(store.create_user("grace", "password-1")[0])
            self.assertTrue(nested.is_file())
        finally:
            store.close()


# --------------------------------------------------------------------------
# 3. 会话 / Cookie / CSRF
# --------------------------------------------------------------------------


class TestAuthPrimitives(unittest.TestCase):
    def setUp(self) -> None:
        self.users = {"id": 7, "username": "editor", "role": ROLE_EDITOR}
        self.manager = auth.SessionManager(secret=b"k" * 32)

    def test_session_lifecycle(self) -> None:
        session = self.manager.create(self.users, now=1000.0)
        self.assertTrue(session.logged_in)
        self.assertTrue(session.is_editor)
        self.assertNotEqual(session.sid, "")
        self.assertIsNotNone(self.manager.get(session.sid, now=1001.0))
        self.assertTrue(self.manager.destroy(session.sid))
        self.assertIsNone(self.manager.get(session.sid, now=1001.0))
        self.assertFalse(self.manager.destroy(session.sid))

    def test_session_expires_and_prunes(self) -> None:
        session = self.manager.create(self.users, now=1000.0)
        self.assertIsNone(self.manager.get(session.sid, now=1000.0 + auth.SESSION_TTL + 1))
        other = self.manager.create(self.users, now=2000.0)
        self.assertEqual(self.manager.prune(now=2000.0 + auth.SESSION_TTL + 1), 1)
        self.assertEqual(self.manager.count(), 0)
        self.assertIsNone(self.manager.get(other.sid, now=2000.0 + auth.SESSION_TTL + 1))

    def test_drop_user_kills_every_session_of_that_user(self) -> None:
        first = self.manager.create(self.users)
        second = self.manager.create(self.users)
        viewer = self.manager.create({"id": 9, "username": "v", "role": ROLE_VIEWER})
        self.assertEqual(self.manager.drop_user(7), 2)
        self.assertIsNone(self.manager.get(first.sid))
        self.assertIsNone(self.manager.get(second.sid))
        self.assertIsNotNone(self.manager.get(viewer.sid))

    def test_anonymous_state_gets_a_csrf_cookie_and_token(self) -> None:
        state = self.manager.state({})
        self.assertFalse(state.logged_in)
        self.assertFalse(state.is_editor)
        self.assertTrue(state.set_anonymous_cookie)
        self.assertTrue(state.csrf_token)
        # 第二次带着 Cookie 来 -> 复用同一个绑定, 不再重发 Cookie
        again = self.manager.state({auth.CSRF_COOKIE: state.anonymous_cookie})
        self.assertFalse(again.set_anonymous_cookie)
        self.assertEqual(again.csrf_token, state.csrf_token)

    def test_garbage_csrf_cookie_is_replaced(self) -> None:
        state = self.manager.state({auth.CSRF_COOKIE: "<script>"})
        self.assertTrue(state.set_anonymous_cookie)
        self.assertNotIn("<", state.anonymous_cookie)

    def test_logged_in_state_binds_token_to_session(self) -> None:
        session = self.manager.create(self.users)
        state = self.manager.state({auth.SESSION_COOKIE: session.sid})
        self.assertTrue(state.logged_in)
        self.assertTrue(state.is_editor)
        self.assertEqual(state.csrf_binding, session.sid)
        self.assertEqual(state.user["username"], "editor")
        self.assertNotEqual(state.csrf_token, self.manager.state({}).csrf_token)

    def test_csrf_signer_checks_binding_and_secret(self) -> None:
        signer = auth.CsrfSigner(secret=b"s" * 32)
        token = signer.token("binding-a")
        self.assertTrue(signer.check("binding-a", token))
        self.assertFalse(signer.check("binding-b", token))
        self.assertFalse(signer.check("binding-a", token + "x"))
        self.assertFalse(signer.check("", token))
        self.assertFalse(signer.check("binding-a", None))
        self.assertFalse(auth.CsrfSigner(secret=b"x" * 32).check("binding-a", token))

    def test_parse_cookie_header(self) -> None:
        self.assertEqual(auth.parse_cookie_header("a=1; b=2"), {"a": "1", "b": "2"})
        self.assertEqual(auth.parse_cookie_header("dnh_session=xyz"), {"dnh_session": "xyz"})
        self.assertEqual(auth.parse_cookie_header(""), {})
        self.assertEqual(auth.parse_cookie_header(None), {})
        self.assertEqual(auth.parse_cookie_header("novalue; a=1"), {"a": "1"})

    def test_session_cookie_is_httponly_and_same_site(self) -> None:
        cookie = auth.set_cookie(auth.SESSION_COOKIE, "sid-value", max_age=3600)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn("Path=/", cookie)
        self.assertIn("Max-Age=3600", cookie)
        cleared = auth.delete_cookie(auth.SESSION_COOKIE)
        self.assertIn("Max-Age=0", cleared)

    def test_same_origin_rules(self) -> None:
        host = "127.0.0.1:8940"
        self.assertTrue(auth.same_origin("", "", host)[0])                        # 都没有 -> 放行
        self.assertTrue(auth.same_origin(f"http://{host}", "", host)[0])
        self.assertTrue(auth.same_origin("", f"http://{host}/login", host)[0])
        self.assertFalse(auth.same_origin("http://evil.test", "", host)[0])
        # `Origin: null` = 没有可用的来源信息(用户实测 403 的成因):
        # 与"没带 Origin"等价 -> 放行; CSRF token 仍是硬门槛。
        self.assertTrue(auth.same_origin("null", "", host)[0])
        self.assertFalse(auth.same_origin("null", "http://evil.test/x", host)[0])
        self.assertFalse(auth.same_origin("", "http://evil.test/x", host)[0])
        self.assertFalse(auth.same_origin(f"http://{host}", "", "")[0])

    def test_can_write_request_requires_editor_for_non_read_methods(self) -> None:
        anonymous = self.manager.state({})
        viewer_session = self.manager.create({"id": 1, "username": "v", "role": ROLE_VIEWER})
        viewer = self.manager.state({auth.SESSION_COOKIE: viewer_session.sid})
        editor_session = self.manager.create(self.users)
        editor = self.manager.state({auth.SESSION_COOKIE: editor_session.sid})
        for state in (anonymous, viewer, editor):
            self.assertTrue(auth.can_write_request("GET", state))
            self.assertTrue(auth.can_write_request("HEAD", state))
        self.assertFalse(auth.can_write_request("POST", anonymous))
        self.assertFalse(auth.can_write_request("PUT", anonymous))
        self.assertFalse(auth.can_write_request("DELETE", viewer))
        self.assertFalse(auth.can_write_request("PATCH", viewer))
        self.assertTrue(auth.can_write_request("POST", editor))
        self.assertFalse(auth.can_edit(anonymous))
        self.assertTrue(auth.can_edit(editor))


# --------------------------------------------------------------------------
# 4. 表单解析(纯函数)
# --------------------------------------------------------------------------


class TestAlbumFormParsing(unittest.TestCase):
    BODY = (
        "title_original=新标题&title=New Title&title_romaji=Shin&circle=Circle&artists=A、B"
        "&release_date=2026-01-02&event=M3-2026春&catalog_no=X-1&price=1000 JPY&hall=H&booth=B1"
        "&tags=a、b,c&genres=Vocal&description=line1%0Aline2"
        "&credit_role=arrange&credit_name=某人、另人&credit_role=vocal&credit_name=歌手"
        "&url_key=home&url_value=https://example.com/&url_key=&url_value=https://e.test/x"
        "&track_index=1&track_title=第一首&track_artists=A&track_duration=3:42"
        "&track_index=&track_title=第二首&track_artists=&track_duration=222"
    )

    def values(self, body: str = None):
        raw = (body or self.BODY).encode("utf-8")
        return form_values(raw)

    def test_parse_duration(self) -> None:
        self.assertEqual(parse_duration("3:42"), (222, True))
        self.assertEqual(parse_duration("1:02:07"), (3727, True))
        self.assertEqual(parse_duration("222"), (222, True))
        self.assertEqual(parse_duration(""), (0, True))
        self.assertEqual(parse_duration("bogus"), (None, False))
        self.assertEqual(parse_duration("99:99"), (6039, True))

    def test_full_form_becomes_a_patch(self) -> None:
        patch, errors = parse_album_form(self.values())
        self.assertEqual(errors, {})
        self.assertEqual(patch["title"], "New Title")
        self.assertEqual(patch["title_original"], "新标题")
        self.assertEqual(patch["artists"], ["A", "B"])
        self.assertEqual(patch["tags"], ["a", "b", "c"])
        self.assertEqual(patch["release_date"], "2026-01-02")
        self.assertEqual(patch["credits"], [
            {"role": "arrange", "name": "某人"}, {"role": "arrange", "name": "另人"},
            {"role": "vocal", "name": "歌手"},
        ])
        self.assertEqual(patch["urls"], {"home": "https://example.com/", "link2": "https://e.test/x"})
        self.assertEqual(len(patch["track_list"]), 2)
        self.assertEqual(patch["track_list"][0]["duration"], 222)
        self.assertEqual(patch["track_list"][1]["index"], 2, "空序号自动补齐")

    def test_empty_title_is_reported_not_crashed(self) -> None:
        """标题为空要给出明确提示(而不是 500 或"点了没反应")."""
        values = {k: [""] for k in ("artists", "release_date", "event", "catalog_no",
                                    "price", "tags", "genres", "hall", "booth",
                                    "title_original", "title_romaji")}
        values["csrf_token"] = ["t"]
        _patch, errors = parse_album_form(values)
        self.assertIn("title", errors)
        self.assertIn("标题", errors["title"])

    def test_bad_date_and_bad_url_are_reported(self) -> None:
        _, errors = parse_album_form(self.values("title_original=X&release_date=2026/01/02"))
        self.assertIn("release_date", errors)
        _, errors = parse_album_form(self.values(
            "title_original=X&url_value=javascript:alert(1)&url_key=x"))
        self.assertIn("urls", errors)

    def test_bad_track_duration_is_reported(self) -> None:
        _, errors = parse_album_form(self.values(
            "title_original=X&track_title=A&track_duration=nope"))
        self.assertIn("tracks", errors)

    def test_blank_rows_are_ignored(self) -> None:
        patch, errors = parse_album_form(self.values(
            "title_original=X&credit_role=&credit_name=&url_key=&url_value=&track_title="))
        self.assertEqual(errors, {})
        self.assertEqual(patch["credits"], [])
        self.assertEqual(patch["urls"], {})
        self.assertEqual(patch["track_list"], [])

    def test_form_display_values_round_trip(self) -> None:
        values = self.values()
        shown = form_display_values(values)
        self.assertEqual(shown["title"], "New Title")
        self.assertEqual(shown["tags"], "a、b,c")
        self.assertEqual(shown["credits"][0], {"role": "arrange", "name": "某人、另人"})
        self.assertEqual(shown["urls"][0], {"key": "home", "url": "https://example.com/"})
        self.assertEqual(shown["tracks"][1]["title"], "第二首")
        # 回显结构必须能被编辑模板直接渲染(不炸、不丢字段)
        page = T.edit_album_page(
            T.SiteContext(csrf_token="tok", is_editor=True),
            album={"key": "k|1", "title": "X"},
            values=shown,
        )
        self.assertIn('value="New Title"', page)


# --------------------------------------------------------------------------
# 5. 模板: 三处展示修复 + 无站长端/来源字样
# --------------------------------------------------------------------------


class TestTemplateFixes(unittest.TestCase):
    CSS = (Path(__file__).resolve().parent.parent / "dnh" / "public" / "static" / "public.css").read_text(
        encoding="utf-8")

    @staticmethod
    def css_block(css: str, selector: str) -> str:
        """取某个选择器的第一条规则体(粗略但够用的断言工具)."""
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
        return match.group(1) if match else ""

    def test_card_tags_wrap_and_are_not_clipped(self) -> None:
        """修复 1: 卡片标签超出被裁掉."""
        card = T.album_card({
            "key": "k", "href": "/album/k", "title": "T",
            "tags": ["一", "二", "三", "四", "五"], "circle": "", "event": "", "date": "",
            "track_count": 5,
        })
        # 标签行是可换行的容器(旧版是 `tagrow` = nowrap + overflow:hidden)
        self.assertIn('class="tagrow tagrow-wrap card-tags"', card)
        for tag in ("一", "二", "三", "四", "五"):
            self.assertIn(f">{tag}</a>", card)
        tagrow = self.css_block(self.CSS, ".tagrow")
        self.assertIn("flex-wrap: wrap", tagrow)
        self.assertNotIn("overflow: hidden", tagrow)
        # 卡片本身不再 overflow:hidden(那会把标签行整条裁掉)
        card_rule = self.css_block(self.CSS, ".album-card")
        self.assertNotIn("overflow: hidden", card_rule)
        self.assertIn("border-radius", card_rule)
        cover_rule = self.css_block(self.CSS, ".card-cover")
        self.assertIn("overflow: hidden", cover_rule)      # 圆角裁剪只在封面上

    def test_event_index_kind_column_is_a_grid(self) -> None:
        """修复 2: 展会索引右侧类型列纵向没对齐."""
        page = T.events_index_page(
            T.SiteContext(),
            events=[{"value": "M3-2026秋", "kind": "m3", "count": 12},
                    {"value": "C108", "kind": "comiket", "count": 3}],
            kinds=[{"value": "m3", "label": "M3", "count": 15}],
        )
        self.assertIn('class="index-row index-row-event"', page)
        self.assertEqual(page.count("index-row index-row-event"), 2)
        self.assertIn('class="index-name"', page)
        self.assertIn('<span class="index-kind">M3</span>', page)
        self.assertIn('<span class="index-count">12 张</span>', page)
        row = self.css_block(self.CSS, ".index-row")
        self.assertIn("grid", row)
        self.assertIn("align-items: center", row)
        event_row = self.css_block(self.CSS, ".index-row-event")
        self.assertIn("grid-template-columns: minmax(0, 1fr) 120px 88px", event_row)
        self.assertIn("text-align: right", self.css_block(self.CSS, ".index-count"))

    def test_circles_index_keeps_two_columns(self) -> None:
        page = T.circles_index_page(
            T.SiteContext(), circles=[{"value": "C", "count": 4}],
            pager={"total": 1, "page": 1, "per": 60, "pages": 1, "window": [], "has_prev": False,
                   "has_next": False},
            url_for=T.make_url_for("/circles", {}),
        )
        self.assertIn('class="index-row"', page)
        self.assertIn('class="index-name"', page)
        self.assertNotIn("index-row-event", page)

    def test_tracklist_caption_direction(self) -> None:
        """曲目表标题只出现一次, 「共 N 首」在表格外的横排标题行."""
        page = T.album_detail_page(
            T.SiteContext(),
            album={
                "key": "k", "href": "/album/k", "title": "T", "tags": [], "genres": [],
                "credits": [], "urls": [], "missing": [], "has_cover": False,
                "track_count": 2, "date": "", "event": "", "circle": "", "artists": [],
                "description": "", "cover": "", "catalog_no": "", "price": "", "booth": "",
                "hall": "", "updated_text": "", "title_alt": "", "romaji": "", "event_kind": "",
                "year": "",
                "tracks": [
                    {"index": 1, "disc": 1, "title": "A", "artists": [], "duration": 83,
                     "duration_text": "1:23"},
                    {"index": 2, "disc": 1, "title": "B", "artists": [], "duration": 245,
                     "duration_text": "4:05"},
                ],
            },
        )
        self.assertEqual(page.count("曲目表"), 1, "「曲目表」标题不应重复")
        self.assertIn('class="tracklist-head"', page)
        self.assertIn('<span class="caption-meta">共 2 首', page)
        self.assertNotIn("<caption>", page)
        head = self.css_block(self.CSS, ".tracklist-head")
        self.assertIn("display: flex", head)
        self.assertIn("white-space: nowrap", head)

    def test_layout_has_no_admin_link_or_source_copy(self) -> None:
        page = T.layout(T.SiteContext(), title="x", body="<p>b</p>")
        for word in FORBIDDEN_WORDS:
            self.assertNotIn(word, page, word)
        self.assertIn("登录", page)
        self.assertIn("注册", page)

    def test_about_page_has_no_source_information(self) -> None:
        page = T.about_page(T.SiteContext(), stats={"albums": 5, "tracks": 9, "circles": 2,
                                                   "events": 1, "tags": 3, "has_profile": 4,
                                                   "edited": 1})
        for word in ("dojin-music", "dojinmusic", "bgm.tv", "Bangumi", "VGMdb", "DoujinStyle",
                     "来源", "覆盖率", "抓取", "采集", "站长端", "歌单", "后台任务"):
            self.assertNotIn(word, page, word)
        self.assertIn("账号与权限", page)
        self.assertIn("编辑者", page)
        self.assertIn("邀请码", page)

    def test_stats_page_drops_the_readonly_blurb(self) -> None:
        page = T.stats_page(T.SiteContext(), stats={"albums": 1, "coverage": {}, "completeness": {}})
        self.assertNotIn("只看公开数据", page)
        self.assertNotIn("歌单", page)
        self.assertNotIn("来源构成", page)

    def test_work_queue_page_keeps_only_its_own_copy(self) -> None:
        page = T.inbox_page(T.SiteContext(), kinds=[{"value": "no_desc", "label": "缺简介", "count": 2}],
                            items=[], pager={"total": 0, "page": 1, "per": 24, "pages": 1,
                                             "window": [], "has_prev": False, "has_next": False},
                            url_for=T.make_url_for("/edit", {}))
        self.assertIn("工作队列", page)
        for word in ("站长端", "公开端", "只读", "补数据", "来源"):
            self.assertNotIn(word, page, word)

    def test_detail_page_drops_data_site_links(self) -> None:
        """**政策**: 指向其它数据网站条目页的链接不得出现在公开端.

        用户明确要求"禁止使用其它数据网站的外链, 仅限本地数据库缓存或外接对象储存"。
        页面既不能给 href, 也不能留下站名字样。
        """
        page = T.album_detail_page(T.SiteContext(), album={
            "key": "k", "href": "/album/k", "title": "T", "tags": [], "genres": [], "credits": [],
            "missing": [], "has_cover": False, "cover": "", "track_count": 0, "tracks": [],
            "date": "", "event": "", "circle": "", "artists": [], "description": "",
            "catalog_no": "", "price": "", "booth": "", "hall": "", "updated_text": "",
            "title_alt": "", "romaji": "", "event_kind": "", "year": "",
            "urls": [{"url": "https://vgmdb.net/album/1", "href": "/link/k/1"},
                     {"url": "https://bgm.tv/subject/1", "href": "/link/k/2"}],
        })
        self.assertNotIn("/link/k/1", page, "数据站链接不该出现在页面上")
        for word in ("vgmdb", "bgm.tv", "bangumi"):
            self.assertNotIn(word, page.lower())

    def test_detail_page_shows_external_links_via_own_route(self) -> None:
        page = T.album_detail_page(T.SiteContext(), album={
            "key": "k", "href": "/album/k", "title": "T", "tags": [], "genres": [], "credits": [],
            "missing": [], "has_cover": True, "cover": "", "track_count": 0, "tracks": [],
            "date": "", "event": "", "circle": "", "artists": [], "description": "",
            "catalog_no": "", "price": "", "booth": "", "hall": "", "updated_text": "",
            "title_alt": "", "romaji": "", "event_kind": "", "year": "",
            # 用**合法**链接(试听/通贩类) —— 数据站链接现在会被政策丢弃(见下一个断言)
            "urls": [{"url": "https://www.youtube.com/watch?v=1", "href": "/link/k/1"}],
        })
        self.assertIn('href="/link/k/1"', page)
        self.assertIn("外部链接", page)
        # 外链旁边不再出现来源站名, 也不直接把外部 URL 写进页面
        self.assertNotIn("youtube.com", page.lower())
        self.assertNotIn("https://www.youtube.com", page)
        for word in ("来源", "可信度"):
            self.assertNotIn(word, page, word)

    def test_auth_pages_carry_their_csrf_token(self) -> None:
        ctx = T.SiteContext(csrf_token="token-abc")
        for page in (T.login_page(ctx), T.register_page(ctx)):
            self.assertIn('name="csrf_token" value="token-abc"', page)
            self.assertIn('method="post"', page)
        register = T.register_page(ctx)
        self.assertIn('name="invite"', register)
        # 表单里没有可伪造的 role 字段
        self.assertNotIn('name="role"', register)

    def test_edit_form_covers_every_required_field(self) -> None:
        page = T.edit_album_page(
            T.SiteContext(csrf_token="t", is_editor=True),
            album={"key": "k", "title": "T", "href": "/album/k"},
            values=T.edit_values_from_album({}),
            errors={"title_original": "标题不能为空"},
            notice="没有保存",
        )
        for field in ("title", "title_original", "title_romaji", "circle", "artists",
                      "release_date", "event", "catalog_no", "price", "hall", "booth",
                      "tags", "genres", "description", "credit_role", "credit_name",
                      "url_key", "url_value", "track_index", "track_title", "track_duration"):
            self.assertIn(f'name="{field}"', page, field)
        self.assertIn("标题不能为空", page)
        self.assertIn("没有保存", page)
        self.assertIn('action="/album/k/edit"', page)


# --------------------------------------------------------------------------
# 6. HTTP 集成: 注册 / 登录 / 鉴权 / 落库
# --------------------------------------------------------------------------


#: 注册页上的算术验证码题面(纯表单, 没有 JS)
_CAPTCHA_QUESTION = re.compile(r'class="captcha-question".*?<b>([^<]*)</b>', re.S)
_CAPTCHA_ID = re.compile(r'name="captcha_id" value="([^"]*)"')
_CAPTCHA_EXPR = re.compile(r"^(\d+) ([-+×]) (\d+) = \?$")


def _answer_of(captcha_id: str) -> str:
    """图形验证码: 页面只有 <img>, 答案在服务端 —— 测试从进程内验证码表取."""
    item = verify._DEFAULT_CAPTCHA_STORE.peek(captcha_id)
    if not item:
        raise AssertionError(f"验证码不存在或已过期: {captcha_id!r}")
    return str(item["answer"])

def captcha_fields(html: str) -> dict:
    """从注册页取 captcha_id, 答案从服务端验证码表取(图形码不在 HTML 里)."""
    id_match = _CAPTCHA_ID.search(html)
    if id_match is None:
        raise AssertionError("注册页没有验证码字段")
    captcha_id = id_match.group(1)
    return {"captcha_id": captcha_id, "captcha_answer": _answer_of(captcha_id)}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class HttpCase(unittest.TestCase):
    """真起一个服务(端口 0), 用带 Cookie 罐的 opener 打接口."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._ctx = temp_dir("users-http-")
        cls.fixture = Fixture(cls._ctx.__enter__())
        cls.site = cls.fixture.site()
        cls.httpd = make_server(cls.site, host="127.0.0.1", port=0)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.httpd.server_address[:2]
        cls.base = f"http://{host}:{port}"
        cls.jar = http.cookiejar.CookieJar()
        #: 跟随跳转(走完整的注册/登录后落地页)
        cls.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cls.jar))
        #: 不跟随跳转 —— 用来断言 302/303 的状态码与 Location
        cls.raw = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cls.jar), NoRedirect
        )
        #: 编辑者账号(直接建号, 等于站方用命令行提升)
        ok, message = cls.fixture.users.create_user("editor1", "editor-password1", role=ROLE_EDITOR)
        assert ok, message

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.site.close()
        cls.fixture.close()
        cls._ctx.__exit__(None, None, None)

    # -- 工具 ---------------------------------------------------------------

    def _request(self, opener, method: str, path: str, *, data: bytes | None = None,
                 origin: str | None = None, headers: dict | None = None):
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        if origin:
            req.add_header("Origin", origin)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        return opener.open(req, timeout=10)

    def request(self, method: str, path: str, *, data: bytes | None = None,
                origin: str | None = None, headers: dict | None = None):
        return self._request(self.opener, method, path, data=data, origin=origin, headers=headers)

    def request_raw(self, method: str, path: str, *, data: bytes | None = None,
                    origin: str | None = None, headers: dict | None = None):
        """不跟随跳转: 3xx 会以 HTTPError 抛出, 从 ``ctx.exception`` 取状态与 Location."""
        return self._request(self.raw, method, path, data=data, origin=origin, headers=headers)

    def get(self, path: str):
        return self.request("GET", path)

    def get_raw(self, path: str):
        return self.request_raw("GET", path)

    def post(self, path: str, fields: dict, *, origin: str | None = "self", headers=None):
        body = urllib.parse.urlencode(fields, doseq=True).encode("utf-8")
        if origin == "self":
            origin = self.base
        return self.request("POST", path, data=body, origin=origin, headers=headers)

    def post_raw(self, path: str, fields: dict, *, origin: str | None = "self", headers=None):
        body = urllib.parse.urlencode(fields, doseq=True).encode("utf-8")
        if origin == "self":
            origin = self.base
        return self.request_raw("POST", path, data=body, origin=origin, headers=headers)

    @staticmethod
    def token_of(html: str) -> str:
        match = re.search(r'name="csrf_token" value="([^"]*)"', html)
        return match.group(1) if match else ""

    def page(self, path: str) -> str:
        with self.get(path) as resp:
            return resp.read().decode("utf-8")

    def form_token(self, path: str) -> str:
        return self.token_of(self.page(path))

    def register_fields(self, **overrides: object) -> dict:
        """注册表单字段: CSRF token + 页面上的验证码(算术题当场算答案).

        注册现在**必须**带验证码(见 :mod:`dnh.public.verify`), 所以凡是走
        ``POST /register`` 的测试都用这个 helper 生成字段, 别再手写 csrf。
        """
        html = self.page("/register")
        fields: dict = {"csrf_token": self.token_of(html)}
        fields.update(captcha_fields(html))
        fields.update({key: value for key, value in overrides.items()})
        return fields

    def cookie(self, name: str):
        for item in self.jar:
            if item.name == name:
                return item
        return None


class TestHttpAnonymous(HttpCase):
    def test_pages_are_reachable_without_login(self) -> None:
        for path in ("/", "/albums", "/about", "/stats", "/login", "/register",
                     "/album/" + T.quote_segment(self.fixture.album.key)):
            with self.subTest(path=path):
                with self.get(path) as resp:
                    self.assertEqual(resp.status, 200)

    def test_work_queue_requires_editor(self) -> None:
        """工作队列是编辑者的工作台 —— 未登录看到的是 403, 不是内容页."""
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/edit")
        self.assertEqual(ctx.exception.code, 403)

    def test_pages_never_mention_admin_or_sources(self) -> None:
        paths = ["/", "/albums", "/about", "/stats", "/login", "/register",
                 "/circles", "/events", "/tags",
                 "/album/" + T.quote_segment(self.fixture.album.key),
                 "/circle/" + T.quote_segment("ShibayanRecords"),
                 "/event/" + T.quote_segment("M3-2026秋"),
                 "/tag/" + T.quote_segment("東方Project")]
        for path in paths:
            with self.subTest(path=path):
                lowered = self.page(path).lower()
                hits = [word for word in FORBIDDEN_WORDS if word.lower() in lowered]
                self.assertEqual(hits, [], f"{path} 含禁止字样: {hits}")

    def test_anonymous_post_is_403_not_405(self) -> None:
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                req = urllib.request.Request(self.base + "/albums", data=b"x=1", method=method)
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self.opener.open(req, timeout=10)
                self.assertEqual(ctx.exception.code, 403)
                self.assertIn("编辑者", ctx.exception.read().decode("utf-8"))

    def test_anonymous_cannot_open_the_edit_form(self) -> None:
        path = "/album/" + T.quote_segment(self.fixture.album.key) + "/edit"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get(path)
        self.assertEqual(ctx.exception.code, 403)

    def test_account_page_redirects_to_login(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get_raw("/account")
        self.assertEqual(ctx.exception.code, 302)
        self.assertTrue(ctx.exception.headers["Location"].startswith("/login"))
        # 跟随跳转后落在登录页
        self.assertIn("登录", self.page("/account"))


class TestHttpAccounts(HttpCase):
    def setUp(self) -> None:
        self.jar.clear()
        self.site.sessions = auth.SessionManager()
        self.username = f"user{id(self) % 100000}"
        self.viewer_name = self.username

    def test_register_creates_a_viewer_and_logs_in(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post_raw("/register", self.register_fields(
                username=self.username, password="password-1", password2="password-1",
                invite="",
            ))
        self.assertEqual(ctx.exception.code, 303)
        self.assertEqual(ctx.exception.headers["Location"], "/account")
        user = self.fixture.users.get(self.username)
        assert user is not None
        self.assertEqual(user["role"], ROLE_VIEWER)
        html = self.page("/account?tab=profile")
        self.assertIn(self.username, html)
        self.assertIn("ident-user", html)     # 角色改为徽章展示
        self.assertIn("申请成为编辑者", html)

    def test_register_rejects_mismatched_password(self) -> None:
        with self.post("/register", self.register_fields(
                username=self.username + "x", password="password-1", password2="password-2",
        )) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("不一致", resp.read().decode("utf-8"))
        self.assertIsNone(self.fixture.users.get(self.username + "x"))

    def test_register_reports_short_password_without_500(self) -> None:
        with self.post("/register", self.register_fields(
                username=self.username + "y", password="short", password2="short",
        )) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("至少", resp.read().decode("utf-8"))

    def test_register_requires_a_captcha(self) -> None:
        """没有验证码 / 验证码答错都不该建号(新规则: 注册必须过验证码)."""
        token = self.form_token("/register")
        with self.post("/register", {"csrf_token": token, "username": self.username + "c",
                                     "password": "password-1", "password2": "password-1"}) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("验证码", resp.read().decode("utf-8"))
        self.assertIsNone(self.fixture.users.get(self.username + "c"))

    def test_register_without_csrf_token_rejected_without_creating_account(self) -> None:
        """令牌失效**不建号**; 注册页改为重新渲染表单并提示"页面已过期"(比裸 403 友好).

        安全属性是"无副作用", 而不是某个状态码 —— 所以这里断言更严格:
        200/403 都接受, 但账号绝不能建出来。
        """
        try:
            resp = self.post("/register", {"username": self.username + "z",
                                           "password": "password-1",
                                           "password2": "password-1"})
            status, body = 200, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status, body = exc.code, exc.read().decode("utf-8", "replace")
        self.assertIn(status, (200, 403))
        if status == 200:
            self.assertIn("页面已过期", body)
        self.assertIsNone(self.fixture.users.get(self.username + "z"))

    def test_login_with_stale_token_logs_out_instead_of_403(self) -> None:
        """**线上故障回归**: 已登录(A)时提交更早渲染的登录表单(令牌属于旧会话)。

        以前 -> 403;现在 -> 销毁旧会话 + 303 回 /login?expired=1。
        """
        self.fixture.users.create_user(self.username, "password-1")
        stale_token = self.form_token("/login")          # 匿名态的令牌
        self.post("/login", {"csrf_token": stale_token, "username": self.username,
                             "password": "password-1"})  # 登录成功, 会话换绑
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post_raw("/login", {"csrf_token": stale_token,   # 旧令牌 -> 不匹配
                                     "username": self.username,
                                     "password": "password-1"})
        self.assertEqual(ctx.exception.code, 303, "应为 303 而不是 403")
        self.assertIn("/login?expired=1", ctx.exception.headers.get("Location") or "")

    def test_register_from_another_origin_is_403(self) -> None:
        token = self.form_token("/register")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/register", {"csrf_token": token, "username": self.username + "o",
                                    "password": "password-1", "password2": "password-1"},
                      origin="http://evil.test")
        self.assertEqual(ctx.exception.code, 403)
        self.assertIsNone(self.fixture.users.get(self.username + "o"))

    def test_login_with_wrong_password_returns_the_form(self) -> None:
        self.fixture.users.create_user(self.username, "password-1")
        token = self.form_token("/login")
        with self.post("/login", {"csrf_token": token, "username": self.username,
                                  "password": "wrong-password"}) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("不正确", resp.read().decode("utf-8"))
        self.assertIsNone(self.fixture.users.get(self.username)["last_login"] or None)

    def test_login_logout_round_trip(self) -> None:
        self.fixture.users.create_user(self.username, "password-1")
        login_token = self.form_token("/login")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post_raw("/login", {"csrf_token": login_token, "username": self.username,
                                     "password": "password-1"})
        self.assertEqual(ctx.exception.code, 303)
        self.assertIsNotNone(self.cookie(auth.SESSION_COOKIE))
        self.assertIn(self.username, self.page("/account"))
        logout_token = self.form_token("/account")
        with self.assertRaises(urllib.error.HTTPError) as ctx2:
            self.post_raw("/logout", {"csrf_token": logout_token})
        self.assertEqual(ctx2.exception.code, 303)
        self.assertEqual(self.page("/").count("登出"), 0)
        session_cookie = self.cookie(auth.SESSION_COOKIE)
        self.assertTrue(session_cookie is None or not session_cookie.value,
                        "登出后会话 Cookie 必须被清掉")

    def test_logout_via_get_is_refused(self) -> None:
        self.fixture.users.create_user(self.username, "password-1")
        token = self.form_token("/login")
        with self.assertRaises(urllib.error.HTTPError):
            self.post_raw("/login", {"csrf_token": token, "username": self.username,
                                     "password": "password-1"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/logout")
        self.assertEqual(ctx.exception.code, 405)
        self.assertIn("POST", ctx.exception.read().decode("utf-8"))

    def test_login_next_parameter_only_allows_local_paths(self) -> None:
        self.fixture.users.create_user(self.username, "password-1")
        token = self.form_token("/login")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post_raw("/login", {"csrf_token": token, "username": self.username,
                                     "password": "password-1", "next": "//evil.test/"})
        self.assertEqual(ctx.exception.headers["Location"], "/account",
                         "协议相对地址必须被挡掉, 只能落在 /account")
        # 换一个干净的匿名会话再试站内路径: 登录后绑定会变, 必须重新取 token
        self.jar.clear()
        self.site.sessions = auth.SessionManager()
        token2 = self.form_token("/login")
        with self.assertRaises(urllib.error.HTTPError) as local:
            self.post_raw("/login", {"csrf_token": token2, "username": self.username,
                                     "password": "password-1", "next": "/albums"})
        self.assertEqual(local.exception.headers["Location"], "/albums")

    def test_session_cookie_is_httponly(self) -> None:
        self.fixture.users.create_user(self.username, "password-1")
        token = self.form_token("/login")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post_raw("/login", {"csrf_token": token, "username": self.username,
                                     "password": "password-1"})
        raw = ctx.exception.headers.get_all("Set-Cookie") or []
        session_cookie = [line for line in raw if line.startswith(auth.SESSION_COOKIE)]
        self.assertTrue(session_cookie)
        self.assertIn("HttpOnly", session_cookie[0])
        self.assertIn("SameSite=Lax", session_cookie[0])


class TestHttpEditing(HttpCase):
    def setUp(self) -> None:
        self.jar.clear()
        self.site.sessions = auth.SessionManager()

    # -- 工具 ---------------------------------------------------------------

    def login(self, username: str, password: str) -> None:
        token = self.form_token("/login")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post_raw("/login", {"csrf_token": token, "username": username,
                                     "password": password})
        self.assertEqual(ctx.exception.code, 303, ctx.exception.headers.get("Location"))

    def register_viewer(self, username: str) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post_raw("/register", self.register_fields(
                username=username, password="password-1", password2="password-1"))
        self.assertEqual(ctx.exception.code, 303)

    @property
    def edit_path(self) -> str:
        return "/album/" + T.quote_segment(self.fixture.album.key) + "/edit"

    def test_viewer_post_is_403(self) -> None:
        self.register_viewer("viewer1")
        token = self.form_token("/account")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post(self.edit_path, {"csrf_token": token, "title_original": "改不动"})
        self.assertEqual(ctx.exception.code, 403)
        self.assertEqual(self.fixture.store.get_album(self.fixture.album.key).title, "幻想郷の記憶")

    def test_viewer_cannot_open_edit_form(self) -> None:
        self.register_viewer("viewer2")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get(self.edit_path)
        self.assertEqual(ctx.exception.code, 403)

    def test_editor_sees_the_edit_form_prefilled(self) -> None:
        self.login("editor1", "editor-password1")
        html = self.page(self.edit_path)
        self.assertIn('value="幻想郷の記憶"', html)
        self.assertIn('value="ShibayanRecords"', html)
        self.assertIn('value="SBCD-042"', html)
        self.assertIn("Opening", html)
        self.assertIn("ShibayanRecords", html)

    def test_editor_post_without_token_is_403(self) -> None:
        self.login("editor1", "editor-password1")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post(self.edit_path, {"title_original": "无 token"})
        self.assertEqual(ctx.exception.code, 403)

    def test_editor_post_with_wrong_token_is_403(self) -> None:
        self.login("editor1", "editor-password1")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post(self.edit_path, {"csrf_token": "forged-token", "title_original": "伪造"})
        self.assertEqual(ctx.exception.code, 403)

    def test_editor_post_from_other_origin_is_403(self) -> None:
        self.login("editor1", "editor-password1")
        token = self.form_token(self.edit_path)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post(self.edit_path, {"csrf_token": token, "title_original": "跨站"},
                      origin="http://evil.test")
        self.assertEqual(ctx.exception.code, 403)

    def test_editor_edit_lands_in_the_database_then_is_reverted(self) -> None:
        """核心链路: 编辑者 POST -> update_album_fields -> Store.get_album 复核 -> 还原."""
        self.login("editor1", "editor-password1")
        token = self.form_token(self.edit_path)
        fields = {
            "csrf_token": token,
            "title_original": "幻想郷の記憶(改订)",
            "title": "Gensoukyou no Kioku",
            "title_romaji": "Gensoukyou no Kioku",
            "circle": "ShibayanRecords(改)",
            "artists": "坂上なち、Guest Artist",
            "release_date": "2026-10-26",
            "event": "M3-2026秋",
            "catalog_no": "SBCD-042R",
            "price": "1500 JPY",
            "hall": "第一展示場",
            "booth": "A-12",
            "tags": "東方Project、Vocal、改订",
            "genres": "Vocal、Rock",
            "description": "改订后的简介。\n第二行。",
            "credit_role": ["arrange", "vocal"],
            "credit_name": ["编曲者", "坂上なち"],
            "url_key": ["home"],
            "url_value": ["https://example.com/album"],
            "track_index": ["1", "2", "3"],
            "track_title": ["Opening", "幻想郷の記憶", "Bonus Track"],
            "track_artists": ["", "坂上なち", ""],
            "track_duration": ["1:23", "4:05", "2:00"],
        }
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post_raw(self.edit_path, fields)
        self.assertEqual(ctx.exception.code, 303)
        self.assertEqual(ctx.exception.headers["Location"],
                         "/album/" + T.quote_segment(self.fixture.album.key))

        album = self.fixture.store.get_album(self.fixture.album.key)
        assert album is not None
        # 表单里 title_original = 原题, title = 译名/显示名
        self.assertEqual(album.title_original, "幻想郷の記憶(改订)")
        self.assertEqual(album.title, "Gensoukyou no Kioku")
        self.assertEqual(album.circle, "ShibayanRecords(改)")
        self.assertEqual(album.release_date, "2026-10-26")
        self.assertEqual(album.catalog_no, "SBCD-042R")
        self.assertEqual(album.artists, ["坂上なち", "Guest Artist"])
        self.assertEqual(album.tags, ["東方Project", "Vocal", "改订"])
        self.assertEqual(album.genres, ["Vocal", "Rock"])
        self.assertEqual([t.title for t in album.tracks], ["Opening", "幻想郷の記憶", "Bonus Track"])
        self.assertEqual(album.tracks[1].duration, 245)
        self.assertEqual({(c.role, c.name) for c in album.credits},
                         {("arrange", "编曲者"), ("vocal", "坂上なち")})
        self.assertIn("https://example.com/album", album.urls.values())
        self.assertTrue(album.edited, "人工编辑必须打上 edited 标记")
        self.assertIn("第二行", album.description)

        # 详情页立刻反映新值(只读连接 + 缓存失效)
        detail = self.page("/album/" + T.quote_segment(self.fixture.album.key))
        self.assertIn("幻想郷の記憶(改订)", detail)
        self.assertIn("SBCD-042R", detail)

        # 还原, 免得影响同类的其它测试
        restored = {k: v for k, v in fields.items()}
        restored.update({
            "title_original": "幻想郷の記憶", "title": "",
            "circle": "ShibayanRecords", "artists": "坂上なち",
            "release_date": "2026-10-25", "catalog_no": "SBCD-042",
            "tags": "東方Project、Vocal、Hardcore、Ambient、Piano",
            "genres": "Vocal", "description": "一张用来测试的专辑。",
            "credit_role": ["circle", "vocal"], "credit_name": ["ShibayanRecords", "坂上なち"],
            "url_key": ["special"], "url_value": ["https://crimson.example.com/"],
            "track_title": ["Opening", "幻想郷の記憶"], "track_artists": ["", ""],
            "track_duration": ["1:23", "4:05"], "track_index": ["1", "2"],
            "price": "", "hall": "", "booth": "",
        })
        restored["csrf_token"] = self.form_token(self.edit_path)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post_raw(self.edit_path, restored)
        self.assertEqual(ctx.exception.code, 303)
        again = self.fixture.store.get_album(self.fixture.album.key)
        assert again is not None
        self.assertEqual(again.title, "幻想郷の記憶")
        self.assertEqual(again.title_original, "幻想郷の記憶")
        self.assertEqual(again.circle, "ShibayanRecords")
        self.assertEqual(len(again.tracks), 2)
        self.assertEqual(again.tags, ["東方Project", "Vocal", "Hardcore", "Ambient", "Piano"])

    def test_editor_validation_error_is_echoed_not_500(self) -> None:
        self.login("editor1", "editor-password1")
        token = self.form_token(self.edit_path)
        with self.post(self.edit_path, {
            "csrf_token": token, "title_original": "", "release_date": "2026/13/40",
            "url_key": "x", "url_value": "javascript:alert(1)",
            "track_title": "A", "track_duration": "nope",
        }) as resp:
            self.assertEqual(resp.status, 200)
            body = resp.read().decode("utf-8")
        for error in ("标题不能为空", "发售日格式", "外链地址", "曲目时长格式"):
            self.assertIn(error, body)
        album = self.fixture.store.get_album(self.fixture.album.key)
        self.assertEqual(album.title, "幻想郷の記憶", "校验失败时不能改到库里")

    def test_invite_code_registration_grants_editor(self) -> None:
        code = self.fixture.users.create_invite(note="测试")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post_raw("/register", self.register_fields(
                username="invited1", password="password-1", password2="password-1",
                invite=code,
            ))
        self.assertEqual(ctx.exception.code, 303)
        user = self.fixture.users.get("invited1")
        self.assertEqual(user["role"], ROLE_EDITOR)
        self.assertTrue(self.fixture.users.invite_info(code)["used"])
        # 同一个人马上就能进编辑界面
        self.assertEqual(self.get(self.edit_path).status, 200)

    def test_used_invite_code_is_refused(self) -> None:
        code = self.fixture.users.create_invite(note="一次性")
        self.fixture.users.redeem_invite(code, "editor1")
        with self.post("/register", self.register_fields(
                username="invited2", password="password-1", password2="password-1",
                invite=code,
        )) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("邀请码", resp.read().decode("utf-8"))
        self.assertIsNone(self.fixture.users.get("invited2"))

    def test_editor_with_unknown_method_gets_405(self) -> None:
        self.login("editor1", "editor-password1")
        token = self.form_token(self.edit_path)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("PUT", "/albums", data=urllib.parse.urlencode(
                {"csrf_token": token}).encode("utf-8"), origin=self.base)
        self.assertEqual(ctx.exception.code, 405)

    def test_outbound_link_route_redirects_to_the_real_url(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get_raw("/link/" + T.quote_segment(self.fixture.album.key) + "/1")
        self.assertEqual(ctx.exception.code, 302)
        self.assertIn("http", ctx.exception.headers["Location"])
        with self.assertRaises(urllib.error.HTTPError) as bad:
            self.get_raw("/link/" + T.quote_segment(self.fixture.album.key) + "/99")
        self.assertEqual(bad.exception.code, 404)
        with self.assertRaises(urllib.error.HTTPError) as messy:
            self.get_raw("/link/nope")
        self.assertEqual(messy.exception.code, 404)


class TestRouting(unittest.TestCase):
    def test_edit_sub_route_is_recognised(self) -> None:
        route = parse_route("/album/a%7Cb/edit")
        self.assertEqual(route["name"], "album_edit")
        self.assertEqual(route["arg"], "a|b")
        self.assertEqual(parse_route("/album/a%7Cb")["name"], "album")
        self.assertEqual(parse_route("/login")["name"], "login")
        self.assertEqual(parse_route("/account")["name"], "account")
        self.assertEqual(parse_route("/logout")["name"], "logout")
        self.assertEqual(parse_route("/link/k/1")["name"], "link")
        self.assertEqual(parse_route("/nope")["name"], "not_found")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
