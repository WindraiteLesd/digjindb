"""个人中心存储层测试(`dddb.public.profile`).

覆盖: 建表幂等 / profile get-or-create / 简介审查 / 生日校验 / 隐私开关 /
收藏与关注的 toggle 去重 / 私信收发与已读 / 通知未读 / 反馈 / 贡献 /
头像白名单与覆盖 / 并发打开 / 非法参数不抛异常。

纯函数(``bio_problem`` / ``avatar_problem``)与存储层分开测; 临时目录走
:func:`tests._tmp.temp_dir`(Windows 沙箱下系统 TEMP 不可写)。
"""

from __future__ import annotations

import sqlite3
import unittest

from dddb.public.profile import (
    AVATAR_MAX_BYTES,
    AVATAR_TYPES,
    BIO_MAX,
    CONTRIBUTION_KINDS,
    FEEDBACK_MAX,
    FOLLOW_KINDS,
    MESSAGE_MAX,
    PRIVACY_DEFAULTS,
    PRIVACY_KEYS,
    PROFILE_COLUMNS,
    ProfileStore,
    ProfileStoreError,
    avatar_problem,
    bio_problem,
    default_profiles_path,
)
from tests._tmp import temp_dir

#: 1x1 PNG(合法的最小图片; 内容不做魔数校验, 但用真图更贴近实际)
TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


class StoreCase(unittest.TestCase):
    """每个测试一个全新的个人中心库(仓库内 .tmp)."""

    def setUp(self) -> None:
        self._ctx = temp_dir("profile-")
        self.root = self._ctx.__enter__()
        self.db_path = self.root / "public_profiles.sqlite3"
        self.clock = [1_700_000_000.0]
        self.store = ProfileStore(self.db_path, clock=lambda: self.clock[0])

    def tearDown(self) -> None:
        try:
            self.store.close()
        finally:
            self._ctx.__exit__(None, None, None)

    def tick(self, seconds: float = 1.0) -> None:
        self.clock[0] += seconds


# --------------------------------------------------------------------------
# 1. 纯函数: 简介审查
# --------------------------------------------------------------------------


class TestBioRules(unittest.TestCase):
    def test_empty_and_none_are_allowed(self) -> None:
        self.assertEqual(bio_problem(""), "")
        self.assertEqual(bio_problem(None), "", "None = 不改/清空, 视为合规")

    def test_whitespace_only_is_rejected(self) -> None:
        self.assertIn("空白", bio_problem("   \n\t "))

    def test_length_limit(self) -> None:
        self.assertEqual(bio_problem("a" * BIO_MAX), "")
        self.assertIn(str(BIO_MAX), bio_problem("a" * (BIO_MAX + 1)))

    def test_html_characters_are_rejected(self) -> None:
        for text in ("<script>alert(1)</script>", "a > b", "<b>粗体</b>"):
            self.assertIn("HTML", bio_problem(text), text)

    def test_banned_words_are_rejected(self) -> None:
        # users.USERNAME_BANNED_PATTERNS 里就有的词(广告类)
        problem = bio_problem("加我 telegram 领资源")
        self.assertIn("违规词汇", problem)
        self.assertIn("telegram", problem)

    def test_bad_schemes_are_rejected(self) -> None:
        for text in (
            "点这里 javascript:alert(1)",
            "看这个 data:text/html;base64,PHNjcmlwdD4=",
            "vbscript:msgbox(1)",
            "java\tscript:alert(1)",   # 空白变形也要拦
        ):
            self.assertNotEqual(bio_problem(text), "", text)

    def test_only_http_and_https_links(self) -> None:
        self.assertEqual(bio_problem("我的主页 https://example.com 欢迎"), "")
        self.assertEqual(bio_problem("主页 http://example.com/x"), "")
        for text in ("ftp://example.com/a", "file:///etc/passwd", "gopher://x"):
            self.assertIn("http/https", bio_problem(text), text)

    def test_link_count_limit(self) -> None:
        five = " ".join(f"http://e{i}.example.com" for i in range(5))
        self.assertEqual(bio_problem(five), "")
        six = five + " http://e6.example.com"
        self.assertIn("最多", bio_problem(six))

    def test_normal_chinese_bio_is_fine(self) -> None:
        text = "喜欢同人音乐, 常去 M3 和例大祭。\n主页: https://example.com/me"
        self.assertEqual(bio_problem(text), "")

    def test_non_text_value(self) -> None:
        self.assertIn("文本", bio_problem(12345))
        self.assertIn("文本", bio_problem(b"bytes"))


# --------------------------------------------------------------------------
# 2. 纯函数: 头像校验
# --------------------------------------------------------------------------


class TestAvatarRules(unittest.TestCase):
    def test_allowed_types(self) -> None:
        for content_type in AVATAR_TYPES:
            self.assertEqual(avatar_problem(content_type, 1024), "", content_type)
        self.assertEqual(avatar_problem("image/png; charset=binary", 1024), "")
        self.assertEqual(avatar_problem("IMAGE/PNG", 1024), "")

    def test_rejected_types(self) -> None:
        for content_type in ("image/svg+xml", "image/gif", "text/html", "", None):
            self.assertIn("只支持", avatar_problem(content_type, 1024), str(content_type))

    def test_size_limits(self) -> None:
        self.assertEqual(avatar_problem("image/png", AVATAR_MAX_BYTES), "")
        self.assertIn("KB", avatar_problem("image/png", AVATAR_MAX_BYTES + 1))
        self.assertIn("为空", avatar_problem("image/png", 0))
        self.assertIn("无效", avatar_problem("image/png", "not-a-size"))
        self.assertIn("无效", avatar_problem("image/png", None))


# --------------------------------------------------------------------------
# 3. 建表 / profile get-or-create
# --------------------------------------------------------------------------


class TestProfileBasics(StoreCase):
    def test_tables_are_created(self) -> None:
        self.assertEqual(
            self.store.table_names(),
            sorted([
                "contributions", "favorites", "feedback", "follows",
                "messages", "notifications", "profiles", "user_prefs",
            ]),
        )
        # 列顺序由 SQLite 决定, 语义上只关心"列齐全"
        self.assertEqual(set(self.store.profile_columns()), set(PROFILE_COLUMNS))

    def test_reopening_is_idempotent(self) -> None:
        me = self.store.profile(11, "alice")
        tables = self.store.table_names()
        self.store.close()
        again = ProfileStore(self.db_path, clock=lambda: self.clock[0])
        try:
            self.assertEqual(again.profile(11, "alice")["user_id"], me["user_id"])
            self.assertEqual(again.table_names(), tables)
        finally:
            again.close()

    def test_profile_get_or_create(self) -> None:
        me = self.store.profile(7, "alice")
        self.assertEqual(me["user_id"], 7)
        self.assertEqual(me["username"], "alice")
        self.assertEqual(me["birthday"], "")
        self.assertEqual(me["bio"], "")
        self.assertEqual(me["avatar"], "")
        self.assertEqual(me["updated_at"], self.clock[0])
        for key in PRIVACY_KEYS:
            self.assertEqual(me[key], PRIVACY_DEFAULTS[key], key)
        # 第二次读不该再插一条
        again = self.store.profile(7, "alice")
        self.assertEqual(again["user_id"], 7)
        rows = self.store._query("SELECT COUNT(*) AS n FROM profiles")
        self.assertEqual(int(rows[0]["n"]), 1)

    def test_username_is_refreshed(self) -> None:
        self.store.profile(7, "old-name")
        renamed = self.store.profile(7, "new-name")
        self.assertEqual(renamed["username"], "new-name")
        rows = self.store._query("SELECT username FROM profiles WHERE user_id = 7")
        self.assertEqual(str(rows[0]["username"]), "new-name")

    def test_invalid_user_id_returns_empty_profile_without_raising(self) -> None:
        for bad in (0, -3, None, "abc", 1.5, True):
            me = self.store.profile(bad, "nobody")
            self.assertEqual(me["user_id"], 0, repr(bad))
            self.assertEqual(me["username"], "nobody")
            self.assertEqual(me["bio"], "")
            # 空档案也要带上全部隐私键(旧 show_* 与档位 vis_*)
            self.assertIn("vis_bio", me)
            self.assertIn("show_bio", me)

    def test_privacy_flags_are_saved(self) -> None:
        """旧的布尔开关仍然可写可读(档位 vis_* 是新增的并行模型)."""
        self.store.profile(3, "dave")
        ok, msg = self.store.set_privacy(3, show_birthday=True, show_bio=False,
                                         show_favorites=True, show_follows=False,
                                         show_comments=True, show_contributions=False)
        self.assertTrue(ok, msg)
        me = self.store.profile(3, "dave")
        self.assertEqual(me["show_birthday"], 1)
        self.assertEqual(me["show_bio"], 0)
        self.assertEqual(me["show_favorites"], 1)
        self.assertEqual(me["show_follows"], 0)
        self.assertEqual(me["show_comments"], 1)
        self.assertEqual(me["show_contributions"], 0)

    def test_visibility_levels_roundtrip(self) -> None:
        self.store.profile(4, "erin")
        ok, msg = self.store.set_visibility(4, {"vis_bio": 2, "vis_favorites": 1})
        self.assertTrue(ok, msg)
        levels = self.store.visibility(4)
        self.assertEqual(levels["vis_bio"], 2)
        self.assertEqual(levels["vis_favorites"], 1)

    def test_unknown_flag_is_rejected(self) -> None:
        ok, msg = self.store.set_privacy(3, show_everything=True)
        self.assertFalse(ok)
        self.assertIn("不支持的隐私项", msg)
        ok, msg = self.store.set_privacy(3, is_admin=True)
        self.assertFalse(ok)

    def test_empty_flags_is_a_noop(self) -> None:
        self.assertEqual(self.store.set_privacy(3), (True, ""))

    def test_invalid_user(self) -> None:
        ok, _ = self.store.set_privacy("abc", show_bio=0)
        self.assertFalse(ok)


# --------------------------------------------------------------------------
# 6. 收藏 / 关注
# --------------------------------------------------------------------------


class TestFavorites(StoreCase):
    def test_toggle_round_trip(self) -> None:
        self.assertFalse(self.store.is_favorite(9, "a|b"))
        self.assertTrue(self.store.toggle_favorite(9, "a|b"), "第一次 toggle = 收藏")
        self.assertTrue(self.store.is_favorite(9, "a|b"))
        self.assertFalse(self.store.toggle_favorite(9, "a|b"), "第二次 toggle = 取消")
        self.assertFalse(self.store.is_favorite(9, "a|b"))

    def test_no_duplicates(self) -> None:
        self.store.toggle_favorite(9, "k")
        self.store.toggle_favorite(9, "k")   # 取消
        self.store.toggle_favorite(9, "k")   # 再收藏
        rows = self.store._query("SELECT COUNT(*) AS n FROM favorites WHERE user_id = 9")
        self.assertEqual(int(rows[0]["n"]), 1)

    def test_list_order_and_limit(self) -> None:
        for index in range(5):
            self.tick(1)
            self.store.toggle_favorite(9, f"k{index}")
        listed = self.store.list_favorites(9)
        self.assertEqual([item["album_key"] for item in listed],
                         ["k4", "k3", "k2", "k1", "k0"], "按收藏时间倒序")
        self.assertEqual(len(self.store.list_favorites(9, limit=2)), 2)

    def test_invalid_arguments(self) -> None:
        self.assertFalse(self.store.toggle_favorite(0, "k"))
        self.assertFalse(self.store.toggle_favorite(9, ""))
        self.assertFalse(self.store.is_favorite(9, ""))
        self.assertEqual(self.store.list_favorites("abc"), [])

    def test_key_is_stored_as_literal_not_sql(self) -> None:
        evil = "'; DROP TABLE favorites; --"
        self.assertTrue(self.store.toggle_favorite(9, evil))
        self.assertTrue(self.store.is_favorite(9, evil))
        self.assertEqual(self.store.list_favorites(9)[0]["album_key"], evil)


class TestFollows(StoreCase):
    def test_toggle_all_kinds(self) -> None:
        for kind in FOLLOW_KINDS:
            self.assertFalse(self.store.is_following(4, kind, "x"))
            self.assertTrue(self.store.toggle_follow(4, kind, "x"))
            self.assertTrue(self.store.is_following(4, kind, "x"))
            self.assertFalse(self.store.toggle_follow(4, kind, "x"))
            self.assertFalse(self.store.is_following(4, kind, "x"))

    def test_kind_is_case_insensitive(self) -> None:
        self.assertTrue(self.store.toggle_follow(4, "Circle", "ROUNDEX"))
        self.assertTrue(self.store.is_following(4, "circle", "ROUNDEX"))

    def test_invalid_kind_returns_false(self) -> None:
        for kind in ("genre", "", None, "album"):
            self.assertFalse(self.store.toggle_follow(4, kind, "x"), repr(kind))
            self.assertFalse(self.store.is_following(4, kind, "x"), repr(kind))

    def test_list_follows_and_filter(self) -> None:
        self.store.toggle_follow(4, "circle", "C1")
        self.tick()
        self.store.toggle_follow(4, "artist", "A1")
        self.tick()
        self.store.toggle_follow(4, "event", "M3-2026春")
        everything = self.store.list_follows(4)
        self.assertEqual([item["kind"] for item in everything], ["event", "artist", "circle"])
        circles = self.store.list_follows(4, kind="circle")
        self.assertEqual([item["key"] for item in circles], ["C1"])
        self.assertEqual(self.store.list_follows(4, kind="album"), [])
        self.assertEqual(self.store.list_follows(0), [])


# --------------------------------------------------------------------------
# 7. 私信
# --------------------------------------------------------------------------


class TestMessages(StoreCase):
    def setUp(self) -> None:
        super().setUp()
        self.store.profile(1, "sender")
        self.store.profile(2, "receiver")

    def test_send_and_receive(self) -> None:
        ok, msg = self.store.send_message(1, 2, "你好", "这是一条私信")
        self.assertTrue(ok, msg)
        inbox = self.store.list_messages(2)
        self.assertEqual(len(inbox), 1)
        item = inbox[0]
        self.assertEqual(item["direction"], "in")
        self.assertEqual(item["peer_id"], 1)
        self.assertEqual(item["peer_name"], "sender", "peer_name 来自 profiles.username")
        self.assertEqual(item["subject"], "你好")
        self.assertEqual(item["body"], "这是一条私信")
        self.assertFalse(item["read"])
        outbox = self.store.list_messages(1, box="outbox")
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["direction"], "out")
        self.assertEqual(outbox[0]["peer_id"], 2)
        self.assertEqual(outbox[0]["peer_name"], "receiver")

    def test_invalid_messages_are_rejected(self) -> None:
        cases = (
            ((0, 2, "", "x"), "无效的发送者"),
            ((1, 0, "", "x"), "无效的收件人"),
            ((1, 1, "", "x"), "不能给自己"),
            ((1, 2, "", "   "), "不能为空"),
            ((1, 2, "", "x" * (MESSAGE_MAX + 1)), "最长"),
            ((1, 2, "s" * 101, "正文"), "标题"),
        )
        for args, keyword in cases:
            ok, msg = self.store.send_message(*args)
            self.assertFalse(ok, args)
            self.assertIn(keyword, msg, args)
        self.assertEqual(self.store.list_messages(2), [])

    def test_mark_read_by_ids_and_all(self) -> None:
        self.store.send_message(1, 2, "第一", "a")
        self.tick()
        self.store.send_message(1, 2, "第二", "b")
        listed = self.store.list_messages(2)
        self.assertEqual(self.store.unread_message_count(2), 2)
        first_id = listed[-1]["id"]   # 时间最早的"第一"
        self.assertEqual(self.store.mark_messages_read(2, [first_id]), 1)
        self.assertEqual(self.store.unread_message_count(2), 1)
        self.assertEqual(self.store.mark_messages_read(2), 1)
        self.assertEqual(self.store.unread_message_count(2), 0)
        self.assertTrue(all(item["read"] for item in self.store.list_messages(2)))
        # 已读的再标一次不再计数
        self.assertEqual(self.store.mark_messages_read(2), 0)

    def test_mark_read_does_not_touch_other_peoples_messages(self) -> None:
        self.store.profile(3, "third")
        self.store.send_message(1, 3, "给第三人", "x")
        self.store.send_message(1, 2, "给 receiver", "y")
        other_id = self.store.list_messages(3)[0]["id"]
        self.assertEqual(self.store.mark_messages_read(2, [other_id]), 0)
        self.assertEqual(self.store.unread_message_count(3), 1)

    def test_box_and_limit(self) -> None:
        for index in range(3):
            self.tick()
            self.store.send_message(1, 2, f"s{index}", "b")
        self.assertEqual(len(self.store.list_messages(2, limit=2)), 2)
        self.assertEqual(len(self.store.list_messages(2, box="bogus")), 3, "未知 box 按收件箱")
        self.assertEqual(self.store.list_messages(0), [])

    def test_sql_injection_in_fields(self) -> None:
        ok, _ = self.store.send_message(1, 2, "'; DROP TABLE messages; --",
                                        "正文 ' OR 1=1 --")
        self.assertTrue(ok)
        self.assertEqual(len(self.store.list_messages(2)), 1)
        self.assertIn("messages", self.store.table_names(), "表还在, 注入只是字面量")


# --------------------------------------------------------------------------
# 8. 通知 / 反馈 / 贡献
# --------------------------------------------------------------------------


class TestNotifications(StoreCase):
    def test_notify_and_list(self) -> None:
        first = self.store.notify(8, "comment", "有人回复了你的评论", link="/album/x")
        self.assertGreater(first, 0)
        self.tick()
        second = self.store.notify(8, "system", "系统公告")
        self.assertGreater(second, first)
        listed = self.store.list_notifications(8)
        self.assertEqual([item["text"] for item in listed],
                         ["系统公告", "有人回复了你的评论"])
        self.assertEqual(listed[0]["kind"], "system")
        self.assertEqual(listed[0]["link"], "")
        self.assertEqual(listed[1]["link"], "/album/x")
        self.assertFalse(listed[0]["read"])

    def test_unread_and_mark_read(self) -> None:
        self.store.notify(8, "system", "a")
        self.store.notify(8, "system", "b")
        self.assertEqual(self.store.unread_notification_count(8), 2)
        self.tick()
        self.assertEqual(self.store.mark_notifications_read(8), 2)
        self.assertEqual(self.store.unread_notification_count(8), 0)
        self.assertEqual(self.store.mark_notifications_read(8), 0)
        self.assertTrue(all(item["read"] for item in self.store.list_notifications(8)))

    def test_invalid_notify(self) -> None:
        self.assertEqual(self.store.notify(0, "system", "x"), 0)
        self.assertEqual(self.store.notify(8, "system", "  "), 0)
        self.assertEqual(self.store.list_notifications("abc"), [])


class TestFeedback(StoreCase):
    def test_add_and_list(self) -> None:
        ok, msg = self.store.add_feedback(6, "希望支持按年份筛选", contact="a@b.co")
        self.assertTrue(ok, msg)
        self.tick()
        self.store.add_feedback(6, "第二条反馈")
        listed = self.store.list_feedback(6)
        self.assertEqual([item["body"] for item in listed], ["第二条反馈", "希望支持按年份筛选"])
        self.assertEqual(listed[1]["contact"], "a@b.co")
        self.assertEqual(listed[0]["status"], "new")

    def test_rejections(self) -> None:
        self.assertFalse(self.store.add_feedback(0, "x")[0])
        self.assertFalse(self.store.add_feedback(6, "  ")[0])
        self.assertFalse(self.store.add_feedback(6, "x" * (FEEDBACK_MAX + 1))[0])
        ok, msg = self.store.add_feedback(6, "正文", contact="c" * 201)
        self.assertFalse(ok)
        self.assertIn("联系方式", msg)
        self.assertEqual(self.store.list_feedback(0), [])


class TestContributions(StoreCase):
    def test_add_and_list(self) -> None:
        for kind in CONTRIBUTION_KINDS:
            self.tick()
            self.assertGreater(self.store.add_contribution(12, kind, f"album-{kind}",
                                                           "补了曲目"), 0, kind)
        listed = self.store.list_contributions(12)
        self.assertEqual(len(listed), len(CONTRIBUTION_KINDS))
        self.assertEqual(listed[0]["kind"], CONTRIBUTION_KINDS[-1], "最近的在最前")
        self.assertEqual(listed[0]["summary"], "补了曲目")

    def test_rejections_and_truncation(self) -> None:
        self.assertEqual(self.store.add_contribution(0, "edit", "x"), 0)
        self.assertEqual(self.store.add_contribution(12, "hack", "x"), 0)
        self.assertEqual(self.store.add_contribution(12, "edit", "  "), 0)
        new_id = self.store.add_contribution(12, "edit", "x", "s" * 2000)
        self.assertGreater(new_id, 0)
        self.assertEqual(len(self.store.list_contributions(12)[0]["summary"]), 500,
                         "过长的摘要截断而不是报错")
        self.assertEqual(self.store.list_contributions("abc"), [])


# --------------------------------------------------------------------------
# 9. 头像
# --------------------------------------------------------------------------


class TestAvatars(StoreCase):
    def test_save_png(self) -> None:
        ok, msg, rel = self.store.save_avatar(13, TINY_PNG, "image/png")
        self.assertTrue(ok, msg)
        self.assertEqual(rel, "avatars/13.png")
        written = self.root / "avatars" / "13.png"
        self.assertTrue(written.is_file())
        self.assertEqual(written.read_bytes(), TINY_PNG)
        self.assertEqual(self.store.profile(13, "eve")["avatar"], rel)
        self.assertEqual(self.store.avatar_path(13), str(written.resolve()))

    def test_overwrite_removes_old_extension(self) -> None:
        self.store.save_avatar(13, TINY_PNG, "image/png")
        old = self.root / "avatars" / "13.png"
        self.assertTrue(old.is_file())
        ok, msg, rel = self.store.save_avatar(13, b"\xff\xd8\xff\xe0fake-jpeg", "image/jpeg")
        self.assertTrue(ok, msg)
        self.assertEqual(rel, "avatars/13.jpg")
        self.assertFalse(old.is_file(), "旧扩展名的头像应被清掉")
        self.assertTrue((self.root / "avatars" / "13.jpg").is_file())
        self.assertEqual(self.store.profile(13, "eve")["avatar"], rel)

    def test_rejections(self) -> None:
        ok, msg, rel = self.store.save_avatar(13, TINY_PNG, "image/svg+xml")
        self.assertFalse(ok)
        self.assertEqual(rel, "")
        self.assertIn("只支持", msg)
        ok, msg, _ = self.store.save_avatar(13, b"x" * (AVATAR_MAX_BYTES + 1), "image/png")
        self.assertFalse(ok)
        self.assertIn("KB", msg)
        ok, msg, _ = self.store.save_avatar(0, TINY_PNG, "image/png")
        self.assertFalse(ok)
        ok, msg, _ = self.store.save_avatar(13, "not-bytes", "image/png")
        self.assertFalse(ok)
        # 失败不该留下文件
        self.assertFalse((self.root / "avatars" / "13.png").exists())
        self.assertEqual(self.store.avatar_path(13), "")

    def test_avatar_path_without_avatar(self) -> None:
        self.store.profile(14, "no-avatar")
        self.assertEqual(self.store.avatar_path(14), "")
        self.assertEqual(self.store.avatar_path(0), "")


# --------------------------------------------------------------------------
# 10. 健壮性: 非法参数 / 汇总
# --------------------------------------------------------------------------


class TestRobustness(StoreCase):
    def test_every_read_survives_invalid_user_ids(self) -> None:
        bad_ids = (0, -1, None, "abc", 2.5, True)
        for user_id in bad_ids:
            self.assertEqual(self.store.list_favorites(user_id), [])
            self.assertEqual(self.store.list_follows(user_id), [])
            self.assertEqual(self.store.list_messages(user_id), [])
            self.assertEqual(self.store.list_notifications(user_id), [])
            self.assertEqual(self.store.list_feedback(user_id), [])
            self.assertEqual(self.store.list_contributions(user_id), [])
            self.assertEqual(self.store.mark_messages_read(user_id), 0)
            self.assertEqual(self.store.mark_notifications_read(user_id), 0)
            self.assertEqual(self.store.unread_message_count(user_id), 0)
            self.assertEqual(self.store.unread_notification_count(user_id), 0)
            self.assertEqual(self.store.avatar_path(user_id), "")

    def test_summary(self) -> None:
        self.store.toggle_favorite(15, "k")
        self.store.toggle_follow(15, "circle", "C")
        self.store.notify(15, "system", "hi")
        self.store.add_contribution(15, "edit", "album-x")
        self.store.send_message(2, 15, "你好", "正文")
        summary = self.store.summary(15)
        self.assertEqual(summary["favorites"], 1)
        self.assertEqual(summary["follows"], 1)
        self.assertEqual(summary["unread_notifications"], 1)
        self.assertEqual(summary["contributions"], 1)
        self.assertEqual(summary["unread_messages"], 1)
        self.assertEqual(self.store.summary(0),
                         {"favorites": 0, "follows": 0, "unread_messages": 0,
                          "unread_notifications": 0, "contributions": 0})

    def test_limits_are_clamped(self) -> None:
        for index in range(3):
            self.store.notify(16, "system", f"n{index}")
        self.assertEqual(len(self.store.list_notifications(16, limit=0)), 1)
        self.assertEqual(len(self.store.list_notifications(16, limit=-5)), 1)
        self.assertEqual(len(self.store.list_notifications(16, limit="abc")), 3)

    def test_default_path_helper(self) -> None:
        path = default_profiles_path(self.root)
        self.assertEqual(path.name, "public_profiles.sqlite3")
        self.assertEqual(path.parent.name, "data")

    def test_bad_database_path_raises_profile_error(self) -> None:
        """把 db 指向一个**目录** -> sqlite 打不开, 必须包成 ProfileStoreError."""
        target = self.root / "i-am-a-directory"
        target.mkdir()
        with self.assertRaises(ProfileStoreError):
            ProfileStore(target)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()