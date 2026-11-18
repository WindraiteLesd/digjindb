"""角色体系 / 编辑者申请审核 / 账号管理 的测试.

覆盖:

1. 角色矩阵(viewer < editor < superuser)与向后兼容;
2. ``UserStore`` 的角色 CRUD、守卫(最后一个超级用户)、禁用/启用;
3. 编辑者申请: 提交 / 去重 / 列表 / 通过 / 拒绝 / 越权 / 重复处理;
4. 登录鉴权 ``authenticate`` 的原因码(口令错 / 禁用 / 邮箱未验证);
5. ``auth`` 里的会话级判定(:class:`AuthState` 的 is_superuser / can_review /
   在线会话的角色热更新);
6. 三个测试账号能建出来且能力各异;
7. 老库(只有 6 列 user 表 + 老 viewer/editor 角色)打开后自动迁移、照旧可读。

不碰 templates/site/data —— 只测后端。
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from dddb.public import auth
from dddb.public import users as U
from dddb.public.users import (
    LOGIN_BAD_CREDENTIALS,
    LOGIN_INACTIVE,
    LOGIN_OK,
    LOGIN_UNVERIFIED_EMAIL,
    REQUEST_APPROVED,
    REQUEST_PENDING,
    REQUEST_REJECTED,
    ROLE_EDITOR,
    ROLE_SUPERUSER,
    ROLE_VIEWER,
    ROLES,
    UserStore,
    can_review,
    can_write,
    clean_role,
    has_role,
    is_superuser,
    role_label,
    role_rank,
)
from tests._tmp import temp_dir

#: 需求里给的三个测试账号(用户名, 口令, 角色)
ACCOUNTS = (
    ("dddb_admin", "dddb-root-K7m2Qx", ROLE_SUPERUSER),
    ("dddb_editor", "dddb-edit-R4v9Pn", ROLE_EDITOR),
    ("dddb_user", "dddb-user-T6s3Wc", ROLE_VIEWER),
)


class StoreCase(unittest.TestCase):
    """每个测试一个干净的用户库(仓库内 .tmp, Windows 沙箱下系统 TEMP 不可写)."""

    def setUp(self) -> None:
        self._ctx = temp_dir("roles-")
        self.root = self._ctx.__enter__()
        self.db_path = self.root / "public_users.sqlite3"
        self.store = UserStore(self.db_path)

    def tearDown(self) -> None:
        try:
            self.store.close()
        finally:
            self._ctx.__exit__(None, None, None)

    # -- 小工具 -------------------------------------------------------------

    def seed(self) -> None:
        for username, password, role in ACCOUNTS:
            ok, message = self.store.create_user(username, password, role=role)
            self.assertTrue(ok, message)

    def make_user(self, username: str, role: str = ROLE_VIEWER) -> None:
        ok, message = self.store.create_user(username, "password-123", role=role)
        self.assertTrue(ok, message)


# --------------------------------------------------------------------------
# 1. 角色矩阵
# --------------------------------------------------------------------------


class TestRoleMatrix(unittest.TestCase):
    def test_roles_are_ordered_low_to_high(self) -> None:
        self.assertEqual(ROLES, (ROLE_VIEWER, ROLE_EDITOR, ROLE_SUPERUSER))
        self.assertLess(role_rank(ROLE_VIEWER), role_rank(ROLE_EDITOR))
        self.assertLess(role_rank(ROLE_EDITOR), role_rank(ROLE_SUPERUSER))

    def test_write_and_review_permissions(self) -> None:
        self.assertFalse(can_write(ROLE_VIEWER))
        self.assertTrue(can_write(ROLE_EDITOR))
        self.assertTrue(can_write(ROLE_SUPERUSER), "超级用户权限高于编辑者, 当然能写")
        self.assertFalse(can_review(ROLE_VIEWER))
        self.assertFalse(can_review(ROLE_EDITOR), "编辑者不能审核别人")
        self.assertTrue(can_review(ROLE_SUPERUSER))

    def test_has_role_is_at_least(self) -> None:
        self.assertTrue(has_role(ROLE_SUPERUSER, ROLE_EDITOR))
        self.assertTrue(has_role(ROLE_EDITOR, ROLE_EDITOR))
        self.assertFalse(has_role(ROLE_VIEWER, ROLE_EDITOR))
        self.assertTrue(has_role(ROLE_SUPERUSER, ROLE_SUPERUSER))
        self.assertFalse(has_role("", ROLE_VIEWER), "未登录(空角色)不算任何角色")
        self.assertFalse(has_role("root", ROLE_VIEWER), "未知角色不获得任何权限")

    def test_role_labels(self) -> None:
        self.assertEqual(role_label(ROLE_VIEWER), "普通用户")
        self.assertEqual(role_label(ROLE_EDITOR), "编辑者")
        self.assertEqual(role_label(ROLE_SUPERUSER), "超级用户")
        # 未知角色按 viewer 显示, 不炸
        self.assertEqual(role_label("admin"), "普通用户")
        self.assertEqual(role_label(None), "普通用户")

    def test_is_superuser_and_backward_compatible_roles(self) -> None:
        self.assertTrue(is_superuser(ROLE_SUPERUSER))
        self.assertFalse(is_superuser(ROLE_EDITOR))
        # 老库里的 viewer/editor 仍然原样识别(不是"未知角色降级")
        self.assertEqual(clean_role("viewer"), ROLE_VIEWER)
        self.assertEqual(clean_role("editor"), ROLE_EDITOR)
        self.assertEqual(clean_role("EDITOR"), ROLE_EDITOR)
        self.assertEqual(clean_role("superuser"), ROLE_SUPERUSER)
        # 真正未知的才降级
        self.assertEqual(clean_role("admin"), ROLE_VIEWER)
        self.assertEqual(clean_role(None), ROLE_VIEWER)


# --------------------------------------------------------------------------
# 2. 存储:角色 / 状态 / 守卫
# --------------------------------------------------------------------------


class TestStoreRoles(StoreCase):
    def test_three_accounts_have_distinct_capabilities(self) -> None:
        self.seed()
        admin = self.store.get("dddb_admin")
        editor = self.store.get("dddb_editor")
        viewer = self.store.get("dddb_user")
        assert admin and editor and viewer
        self.assertEqual(admin["role"], ROLE_SUPERUSER)
        self.assertEqual(editor["role"], ROLE_EDITOR)
        self.assertEqual(viewer["role"], ROLE_VIEWER)
        self.assertEqual(
            (admin["can_write"], admin["can_review"]), (True, True)
        )
        self.assertEqual((editor["can_write"], editor["can_review"]), (True, False))
        self.assertEqual((viewer["can_write"], viewer["can_review"]), (False, False))
        for user in (admin, editor, viewer):
            self.assertNotIn("password_hash", user, "视图字典不能带口令哈希")
            self.assertTrue(user["is_active"])
            self.assertTrue(user["email_verified"], "CLI 建的账号默认可用")

    def test_created_rows_have_new_columns_appended_after_the_original_six(self) -> None:
        columns = self.store.columns()
        self.assertEqual(
            columns[:6], ["id", "username", "password_hash", "role", "created_at", "last_login"],
            "前 6 列必须保持原样(向后兼容)",
        )
        for extra in ("email", "email_verified", "verified_at", "is_active"):
            self.assertIn(extra, columns)
        self.assertEqual(self.store.schema_version(), len(columns))

    def test_set_role_requires_superuser_when_actor_given(self) -> None:
        self.seed()
        ok, message = self.store.set_role("dddb_user", ROLE_EDITOR, actor="dddb_editor")
        self.assertFalse(ok)
        self.assertIn("超级用户", message)
        self.assertEqual(self.store.get("dddb_user")["role"], ROLE_VIEWER)
        ok, message = self.store.set_role("dddb_user", ROLE_EDITOR, actor="dddb_admin")
        self.assertTrue(ok, message)
        self.assertEqual(self.store.get("dddb_user")["role"], ROLE_EDITOR)

    def test_set_role_without_actor_is_system_operation(self) -> None:
        """CLI / 脚本没有登录态, ``actor=None`` 视为系统操作(由 OS 权限兜底)."""
        self.seed()
        ok, message = self.store.set_role("dddb_user", ROLE_EDITOR)
        self.assertTrue(ok, message)
        self.assertEqual(self.store.get("dddb_user")["role"], ROLE_EDITOR)

    def test_cannot_demote_or_remove_the_last_superuser(self) -> None:
        self.seed()
        ok, message = self.store.set_role("dddb_admin", ROLE_EDITOR)
        self.assertFalse(ok)
        self.assertIn("最后一个超级用户", message)
        ok, message = self.store.remove("dddb_admin", actor="dddb_admin")
        self.assertFalse(ok)
        self.assertIn("最后一个超级用户", message)
        # 有第二个超级用户之后就可以降级了
        self.make_user("second_root", ROLE_SUPERUSER)
        ok, message = self.store.set_role("dddb_admin", ROLE_EDITOR)
        self.assertTrue(ok, message)

    def test_disable_and_enable_account(self) -> None:
        self.seed()
        ok, message = self.store.set_active("dddb_user", False, actor="dddb_admin")
        self.assertTrue(ok, message)
        self.assertFalse(self.store.get("dddb_user")["is_active"])
        result = self.store.authenticate("dddb_user", "dddb-user-T6s3Wc")
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, LOGIN_INACTIVE)
        self.assertIn("禁用", result.message)
        ok, _ = self.store.set_active("dddb_user", True, actor="dddb_admin")
        self.assertTrue(ok)
        self.assertTrue(self.store.authenticate("dddb_user", "dddb-user-T6s3Wc").ok)

    def test_cannot_disable_last_active_superuser(self) -> None:
        self.seed()
        ok, message = self.store.set_active("dddb_admin", False, actor="dddb_admin")
        self.assertFalse(ok)
        self.assertIn("最后一个超级用户", message)

    def test_non_superuser_cannot_manage_users(self) -> None:
        self.seed()
        for action in (
            lambda: self.store.set_active("dddb_editor", False, actor="dddb_editor"),
            lambda: self.store.remove("dddb_user", actor="dddb_user"),
            lambda: self.store.set_role("dddb_user", ROLE_SUPERUSER, actor="dddb_editor"),
        ):
            ok, message = action()
            self.assertFalse(ok)
            self.assertIn("超级用户", message)

    def test_unknown_actor_is_rejected(self) -> None:
        self.seed()
        ok, message = self.store.set_role("dddb_user", ROLE_EDITOR, actor="ghost-user")
        self.assertFalse(ok)
        self.assertIn("找不到操作者", message)


# --------------------------------------------------------------------------
# 3. 编辑者申请 → 审核
# --------------------------------------------------------------------------


class TestEditorRequests(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed()

    def test_viewer_requests_then_superuser_approves(self) -> None:
        ok, message = self.store.request_editor("dddb_user", reason="想补曲目表")
        self.assertTrue(ok, message)
        self.assertEqual(self.store.pending_editor_request_count(), 1)
        request = self.store.get_editor_request("dddb_user")
        assert request is not None
        self.assertEqual(request["status"], REQUEST_PENDING)
        self.assertEqual(request["status_label"], "待审核")
        self.assertEqual(request["reason"], "想补曲目表")
        self.assertEqual(request["username"], "dddb_user")
        self.assertTrue(request["created_at_text"])

        ok, message = self.store.approve_editor(request["id"], actor="dddb_admin", note="看过作品了")
        self.assertTrue(ok, message)
        self.assertEqual(self.store.get("dddb_user")["role"], ROLE_EDITOR)
        handled = self.store.get_editor_request(request["id"])
        assert handled is not None
        self.assertEqual(handled["status"], REQUEST_APPROVED)
        self.assertEqual(handled["status_label"], "已通过")
        self.assertEqual(handled["handled_by"], "dddb_admin")
        self.assertEqual(handled["note"], "看过作品了")
        self.assertGreater(handled["handled_at"], 0)
        self.assertTrue(handled["handled_at_text"])
        counts = self.store.editor_request_counts()
        self.assertEqual((counts["pending"], counts["approved"]), (0, 1))

    def test_reject_keeps_role_and_records_reason(self) -> None:
        ok, _ = self.store.request_editor("dddb_user", reason="凑热闹")
        self.assertTrue(ok)
        ok, message = self.store.reject_editor("dddb_user", actor="dddb_admin", note="请先贡献")
        self.assertTrue(ok, message)
        self.assertEqual(self.store.get("dddb_user")["role"], ROLE_VIEWER)
        request = self.store.get_editor_request("dddb_user")
        assert request is not None
        self.assertEqual(request["status"], REQUEST_REJECTED)
        self.assertEqual(request["status_label"], "已拒绝")
        self.assertEqual(request["note"], "请先贡献")

    def test_duplicate_pending_request_is_refused(self) -> None:
        self.assertTrue(self.store.request_editor("dddb_user")[0])
        ok, message = self.store.request_editor("dddb_user")
        self.assertFalse(ok)
        self.assertIn("已经提交过", message)
        self.assertEqual(self.store.editor_request_counts()["pending"], 1)

    def test_editor_and_superuser_cannot_request(self) -> None:
        for name in ("dddb_editor", "dddb_admin"):
            ok, message = self.store.request_editor(name)
            self.assertFalse(ok, name)
            self.assertIn("已经有编辑权限", message)

    def test_unknown_user_cannot_request(self) -> None:
        ok, message = self.store.request_editor("nobody-here")
        self.assertFalse(ok)
        self.assertIn("找不到", message)

    def test_reason_is_limited_and_optional(self) -> None:
        ok, message = self.store.request_editor("dddb_user", reason="x" * 5000)
        self.assertFalse(ok)
        self.assertIn("最长", message)
        ok, message = self.store.request_editor("dddb_user", reason="")
        self.assertTrue(ok, message)
        request = self.store.get_editor_request("dddb_user")
        self.assertEqual(request["reason"], "")

    def test_only_superuser_can_approve(self) -> None:
        self.store.request_editor("dddb_user")
        for actor in ("dddb_editor", "dddb_user"):
            ok, message = self.store.approve_editor("dddb_user", actor=actor)
            self.assertFalse(ok, f"{actor} 不该能审核")
            self.assertIn("超级用户", message)
        self.assertEqual(self.store.get("dddb_user")["role"], ROLE_VIEWER)
        self.assertEqual(self.store.pending_editor_request_count(), 1)

    def test_request_cannot_be_handled_twice(self) -> None:
        self.store.request_editor("dddb_user")
        self.assertTrue(self.store.approve_editor("dddb_user", actor="dddb_admin")[0])
        ok, message = self.store.reject_editor("dddb_user", actor="dddb_admin")
        self.assertFalse(ok)
        self.assertIn("已经处理过", message)

    def test_unknown_request_is_reported(self) -> None:
        ok, message = self.store.approve_editor(99999, actor="dddb_admin")
        self.assertFalse(ok)
        self.assertIn("找不到这条申请", message)

    def test_list_requests_filters_and_orders(self) -> None:
        self.make_user("alice")
        self.make_user("bob")
        self.store.request_editor("alice", now=1000.0)
        self.store.request_editor("bob", now=2000.0)
        pending = self.store.list_editor_requests()
        self.assertEqual([row["username"] for row in pending], ["alice", "bob"],
                         "待审核按提交时间升序")
        self.store.approve_editor("bob", actor="dddb_admin", now=3000.0)
        self.assertEqual([r["username"] for r in self.store.list_editor_requests()], ["alice"])
        approved = self.store.list_editor_requests(status=REQUEST_APPROVED)
        self.assertEqual([r["username"] for r in approved], ["bob"])
        everything = self.store.list_editor_requests(status="all")
        self.assertEqual({r["username"] for r in everything}, {"alice", "bob"})
        self.assertEqual(self.store.list_editor_requests(status="bogus"), self.store.list_editor_requests())

    def test_request_after_rejection_can_be_resubmitted(self) -> None:
        self.store.request_editor("dddb_user", reason="第一次")
        self.store.reject_editor("dddb_user", actor="dddb_admin")
        ok, message = self.store.request_editor("dddb_user", reason="第二次")
        self.assertTrue(ok, message)
        self.assertEqual(self.store.editor_request_counts()["pending"], 1)

    def test_pending_request_survives_role_grant_by_hand(self) -> None:
        """即使管理员直接把人升成 editor, 那条 pending 申请仍可被正常处理."""
        self.store.request_editor("dddb_user")
        self.assertTrue(self.store.set_role("dddb_user", ROLE_EDITOR, actor="dddb_admin")[0])
        ok, message = self.store.approve_editor("dddb_user", actor="dddb_admin")
        self.assertTrue(ok, message)
        self.assertEqual(self.store.get("dddb_user")["role"], ROLE_EDITOR)


# --------------------------------------------------------------------------
# 4. 登录鉴权(带原因)
# --------------------------------------------------------------------------


class TestAuthenticate(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed()

    def test_ok_reason_and_last_login(self) -> None:
        result = self.store.authenticate("dddb_admin", "dddb-root-K7m2Qx", now=1_700_000_000)
        self.assertTrue(result.ok)
        self.assertFalse(result.denied)
        self.assertEqual(result.reason, LOGIN_OK)
        self.assertEqual(result.user["role"], ROLE_SUPERUSER)
        self.assertEqual(result.user["last_login"], 1_700_000_000)
        self.assertFalse(result.need_verify)

    def test_bad_credentials_for_wrong_password_and_unknown_user(self) -> None:
        for name, password in (("dddb_admin", "wrong-pass"), ("ghost-user", "whatever1")):
            result = self.store.authenticate(name, password)
            self.assertFalse(result.ok, name)
            self.assertEqual(result.reason, LOGIN_BAD_CREDENTIALS)
            self.assertIsNone(result.user)
            self.assertIn("密码", result.message)

    def test_unverified_email_can_login_but_is_flagged(self) -> None:
        """行为变更: 未验证不再阻断登录; LoginResult.reason 保持 ok,
        页面据用户资料的 email_verified 判断是否提示验证。"""
        ok, message = self.store.create_user("uv3", "password-123",
                                             email="uv3@example.com",
                                             email_verified=False)
        self.assertTrue(ok, message)
        result = self.store.authenticate("uv3", "password-123")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.reason, "ok")
        self.assertFalse((result.user or {}).get("email_verified"))

    def test_email_without_verification_flag_defaults_to_usable(self) -> None:
        """``email_verified=None`` + 没给邮箱 -> 直接可用(老行为不变)."""
        ok, message = self.store.create_user("plain", "password-123")
        self.assertTrue(ok, message)
        self.assertTrue(self.store.get("plain")["email_verified"])
        self.assertEqual(self.store.create_user(
            "withmail", "password-123", email="a@b.co")[1], "", "给了邮箱默认要验证")
        self.assertFalse(self.store.get("withmail")["email_verified"])

    def test_explicit_verified_flag_is_honoured(self) -> None:
        """CLI/管理员建号传 ``email_verified=True`` 时, 带邮箱也直接可用."""
        ok, message = self.store.create_user(
            "admin_made", "password-123", email="admin@example.com", email_verified=True)
        self.assertTrue(ok, message)
        self.assertTrue(self.store.get("admin_made")["email_verified"])
        self.assertTrue(self.store.authenticate("admin_made", "password-123").ok)

    def test_policy_flag_for_email_gate(self) -> None:
        self.assertTrue(U.email_verification_blocks_login(),
                        "未验证邮箱的账号不允许登录(页面据此引导去验证页)")


# --------------------------------------------------------------------------
# 5. 会话层(auth)的角色判定
# --------------------------------------------------------------------------


class TestSessionRoles(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.seed()
        self.manager = auth.SessionManager()

    def state_of(self, username: str) -> auth.AuthState:
        session = self.manager.create(self.store.get(username))
        return self.manager.state({auth.SESSION_COOKIE: session.sid})

    def test_session_flags_for_each_role(self) -> None:
        admin = self.state_of("dddb_admin")
        editor = self.state_of("dddb_editor")
        viewer = self.state_of("dddb_user")
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.is_editor)
        self.assertTrue(admin.can_review())
        self.assertTrue(auth.can_review(admin))
        self.assertTrue(auth.can_manage_users(admin))
        self.assertFalse(editor.is_superuser)
        self.assertTrue(editor.is_editor, "编辑者仍然能写")
        self.assertFalse(auth.can_review(editor))
        self.assertFalse(viewer.is_editor)
        self.assertFalse(auth.is_superuser(viewer))
        self.assertEqual(admin.user["role_label"], "超级用户")
        self.assertTrue(admin.user["can_review"])

    def test_write_request_allows_superuser(self) -> None:
        for username in ("dddb_admin", "dddb_editor"):
            state = self.state_of(username)
            self.assertTrue(auth.can_write_request("POST", state), username)
        viewer = self.state_of("dddb_user")
        self.assertFalse(auth.can_write_request("POST", viewer))
        self.assertTrue(auth.can_write_request("GET", viewer))

    def test_online_session_picks_up_role_changes(self) -> None:
        session = self.manager.create(self.store.get("dddb_user"))
        self.assertFalse(self.manager.get(session.sid).is_editor)
        user_id = self.store.get("dddb_user")["id"]
        self.store.set_role("dddb_user", ROLE_EDITOR, actor="dddb_admin")
        self.assertEqual(self.manager.update_user_role(user_id, ROLE_EDITOR), 1)
        refreshed = self.manager.get(session.sid)
        self.assertTrue(refreshed.is_editor, "升职后不用重新登录")
        self.assertEqual(self.manager.update_user_role(user_id, ROLE_EDITOR), 0,
                         "角色没变化时不重复写")
        # 降职同理: 立刻失去写权限
        self.store.set_role("dddb_user", ROLE_VIEWER, actor="dddb_admin")
        self.manager.update_user_role(user_id, ROLE_VIEWER)
        self.assertFalse(self.manager.get(session.sid).is_editor)

    def test_drop_user_kicks_all_sessions(self) -> None:
        first = self.manager.create(self.store.get("dddb_admin"))
        second = self.manager.create(self.store.get("dddb_admin"))
        user_id = self.store.get("dddb_admin")["id"]
        self.store.set_active("dddb_admin", True, actor="dddb_admin")
        self.assertEqual(self.manager.drop_user(user_id), 2)
        self.assertIsNone(self.manager.get(first.sid))
        self.assertIsNone(self.manager.get(second.sid))

    def test_role_choices_include_superuser(self) -> None:
        labels = dict(auth.ROLE_CHOICES)
        self.assertEqual(labels[ROLE_SUPERUSER], "超级用户")
        self.assertEqual(set(labels), set(ROLES))


# --------------------------------------------------------------------------
# 6. 老库迁移 / 向后兼容
# --------------------------------------------------------------------------


class TestLegacyDatabaseCompatibility(unittest.TestCase):
    """手工造一个"老版本"的用户库(只有 6 列 + viewer/editor), 看能不能直接用."""

    LEGACY_SQL = """
    CREATE TABLE users (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      username TEXT NOT NULL UNIQUE COLLATE NOCASE,
      password_hash TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'viewer',
      created_at REAL NOT NULL,
      last_login REAL NOT NULL DEFAULT 0
    );
    CREATE TABLE invite_codes (
      code TEXT PRIMARY KEY, note TEXT NOT NULL DEFAULT '',
      created_at REAL NOT NULL, used_by TEXT NOT NULL DEFAULT '', used_at REAL NOT NULL DEFAULT 0
    );
    """

    def setUp(self) -> None:
        self._ctx = temp_dir("legacy-")
        self.root = self._ctx.__enter__()
        self.path = self.root / "old_users.sqlite3"

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def build_legacy(self) -> None:
        conn = sqlite3.connect(str(self.path))
        try:
            conn.executescript(self.LEGACY_SQL)
            stamp = 1_600_000_000.0
            for name, role in (("old_viewer", ROLE_VIEWER), ("old_editor", ROLE_EDITOR)):
                conn.execute(
                    "INSERT INTO users(username, password_hash, role, created_at, last_login)"
                    " VALUES (?,?,?,?,0)",
                    (name, U.hash_password("password-123"), role, stamp),
                )
            conn.commit()
        finally:
            conn.close()

    def test_legacy_database_is_migrated_in_place(self) -> None:
        self.build_legacy()
        store = UserStore(self.path)
        try:
            columns = store.columns()
            self.assertEqual(columns[:6], ["id", "username", "password_hash", "role",
                                           "created_at", "last_login"])
            for extra in ("email", "email_verified", "verified_at", "is_active"):
                self.assertIn(extra, columns, f"迁移应补上 {extra}")
            self.assertIn("editor_requests", store.table_names())
            self.assertIn("verify_codes", store.table_names())
            # 老账号照旧能用(不会被"邮箱未验证"锁死)
            viewer = store.get("old_viewer")
            editor = store.get("old_editor")
            assert viewer and editor
            self.assertEqual(viewer["role"], ROLE_VIEWER)
            self.assertEqual(editor["role"], ROLE_EDITOR)
            self.assertTrue(viewer["email_verified"], "老数据默认视为已验证")
            self.assertTrue(viewer["is_active"])
            self.assertTrue(store.check_login("old_editor", "password-123"))
            self.assertTrue(store.check_login("old_viewer", "password-123"))
        finally:
            store.close()

    def test_migration_is_idempotent(self) -> None:
        self.build_legacy()
        first = UserStore(self.path)
        first_columns = first.columns()
        first.close()
        second = UserStore(self.path)
        try:
            self.assertEqual(second.columns(), first_columns)
            self.assertEqual(second.schema_version(), len(first_columns))
        finally:
            second.close()

    def test_legacy_editor_can_still_request_nothing_but_viewer_can(self) -> None:
        self.build_legacy()
        store = UserStore(self.path)
        try:
            ok, message = store.request_editor("old_editor")
            self.assertFalse(ok)
            self.assertIn("已经有编辑权限", message)
            self.assertTrue(store.request_editor("old_viewer")[0])
        finally:
            store.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()