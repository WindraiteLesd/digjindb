"""个人中心回归测试(契约级, 不做完整 HTTP 夹具以免重复造轮子)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from dnh.public import site as S
from dnh.public import templates as T

ROOT = Path(__file__).resolve().parents[1]


class TestAccountPageContent(unittest.TestCase):
    def setUp(self) -> None:
        self.html = T.account_page(
            T.SiteContext(csrf_token="tok"),
            user={"id": 7, "username": "alice", "role": "viewer", "role_label": "普通用户"},
            profile={"birthday": "1999-01-02", "bio": "喜欢 https://example.com 的社团",
                     "show_bio": 1},
            can_request=True,
        )

    def test_permissions_block_removed(self) -> None:
        self.assertNotIn("我的权限", self.html)
        self.assertNotIn("密码与角色由站方维护", self.html)

    def test_personal_sections_present(self) -> None:
        """左栏选项卡指向所有分区(内容一次只渲染一个)."""
        for key in ("overview", "profile", "favorites", "follows",
                    "comments", "feedback", "privacy"):
            self.assertIn(f'href="/account?tab={key}"', self.html)

    def test_tab_content_is_isolated(self) -> None:
        overview = T.account_page(
            T.SiteContext(csrf_token="tok"),
            user={"id": 7, "username": "alice", "role": "viewer", "role_label": "普通用户"})
        self.assertIn("总览", overview)
        self.assertNotIn('name="birthday"', overview)      # 资料表单不在总览里
        profile_tab = T.account_page(
            T.SiteContext(csrf_token="tok"),
            user={"id": 7, "username": "alice", "role": "viewer", "role_label": "普通用户"},
            tab="profile")
        self.assertIn('name="birthday"', profile_tab)
        self.assertNotIn('<h2 class="section-title">总览', profile_tab)

    def test_private_sections_absent_for_viewer(self) -> None:
        self.assertNotIn("tab=contributions", self.html)

    def test_contributions_visible_for_editor(self) -> None:
        html = T.account_page(
            T.SiteContext(csrf_token="tok"),
            user={"id": 7, "username": "ed", "role": "editor", "role_label": "编辑者"},
            profile={}, tab="contributions",
        )
        self.assertIn('id="contributions"', html)

    def test_profile_fields_editable(self) -> None:
        self.html = T.account_page(
            T.SiteContext(csrf_token="tok"),
            user={"id": 7, "username": "alice", "role": "viewer", "role_label": "普通用户"},
            profile={"bio": "x"}, tab="profile")
        self.assertIn('name="birthday"', self.html)
        self.assertIn('name="bio"', self.html)
        self.assertIn('enctype="multipart/form-data"', self.html)   # 头像上传
        privacy = T.account_page(
            T.SiteContext(csrf_token="tok"),
            user={"id": 7, "username": "alice", "role": "viewer", "role_label": "普通用户"},
            profile={}, tab="privacy")
        self.assertIn('action="/account/privacy"', privacy)

    def test_bio_external_link_is_marked(self) -> None:
        page = T.account_page(
            T.SiteContext(csrf_token="tok"),
            user={"id": 7, "username": "alice", "role": "viewer", "role_label": "普通用户"},
            profile={"bio": "喜欢 https://example.com 的社团"}, tab="profile")
        self.assertIn('data-external="1"', page)
        self.assertIn("https://example.com", page)

    def test_apply_dialog_only_closable_by_button(self) -> None:
        self.html = T.account_page(
            T.SiteContext(csrf_token="tok"),
            user={"id": 7, "username": "alice", "role": "viewer", "role_label": "普通用户"},
            can_request=True, tab="profile")
        self.assertIn('id="apply-dialog"', self.html)
        self.assertIn('id="apply-close"', self.html)
        self.assertIn('data-draft-key="dddb:apply-draft"', self.html)
        self.assertIn('id="apply-open"', self.html)          # 按钮在"角色"那一行
        self.assertIn("申请成为编辑者", self.html)

    def test_no_apply_dialog_when_not_allowed(self) -> None:
        html = T.account_page(
            T.SiteContext(csrf_token="tok"),
            user={"id": 7, "username": "ed", "role": "editor", "role_label": "编辑者"},
            profile={}, can_request=False, tab="profile",
        )
        self.assertNotIn('id="apply-dialog"', html)


class TestRenderBio(unittest.TestCase):
    def test_escapes_html_and_keeps_urls(self) -> None:
        html = T.render_bio('<script>x</script> https://a.example/p?q=1')
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn('data-external="1"', html)
        self.assertIn("https://a.example/p?q=1", html)

    def test_newlines_become_br(self) -> None:
        self.assertIn("<br>", T.render_bio("第一行\n第二行"))


class TestMemberRoutes(unittest.TestCase):
    def test_routes_registered(self) -> None:
        for head, name in (("messages", "messages"), ("notifications", "notifications"),
                           ("feedback", "feedback"), ("avatar", "avatar"),
                           ("favorites", "favorites_toggle"), ("follows", "follows_toggle")):
            self.assertEqual(S._ROUTE_MAP.get(head), name, head)

    def test_subroutes_parse(self) -> None:
        self.assertEqual(S.parse_route("/messages/send")["name"], "messages_send")
        self.assertEqual(S.parse_route("/notifications/read")["name"], "notifications_read")
        self.assertEqual(S.parse_route("/account/profile")["name"], "account_profile")
        self.assertEqual(S.parse_route("/account/privacy")["name"], "account_privacy")
        self.assertEqual(S.parse_route("/account/avatar")["name"], "account_avatar")
        self.assertEqual(S.parse_route("/account/unknown")["name"], "not_found")

    def test_member_write_routes_are_login_level(self) -> None:
        """个人中心写路由不该要求编辑权限."""
        for name in ("favorites_toggle", "follows_toggle", "messages_send",
                     "notifications_read", "account_profile", "account_privacy",
                     "account_avatar", "feedback"):
            self.assertIn(name, S._MEMBER_WRITE_ROUTES, name)


class TestMemberJsAndCss(unittest.TestCase):
    def setUp(self) -> None:
        self.js = (ROOT / "dnh" / "public" / "static" / "public.js").read_text(encoding="utf-8")
        self.css = (ROOT / "dnh" / "public" / "static" / "public.css").read_text(encoding="utf-8")

    def test_dialog_js_hooks(self) -> None:
        self.assertIn("setupApplyDialog", self.js)
        self.assertIn('"cancel"', self.js)          # 禁 Esc
        self.assertIn("localStorage", self.js)      # 草稿
        self.assertIn("apply-draft", self.js)

    def test_external_link_confirm(self) -> None:
        self.assertIn("setupExternalLinks", self.js)
        self.assertIn("data-external", self.js)
        self.assertIn("confirm(", self.js)

    def test_css_and_script_version(self) -> None:
        self.assertIn(".apply-dialog", self.css)
        html = T.layout(T.SiteContext(), title="x", body="")
        self.assertRegex(html, r"/static/public\.js\?v=\d+")


if __name__ == "__main__":
    unittest.main()

class TestCommentsAndFollows(unittest.TestCase):
    def test_comment_route_parses(self) -> None:
        self.assertEqual(S.parse_route("/album/abc/comment")["name"], "album_comment")
        self.assertEqual(S.parse_route("/album/abc/comment")["arg"], "abc")

    def test_comment_write_is_login_level(self) -> None:
        self.assertIn("album_comment", S._MEMBER_WRITE_ROUTES)

    def test_album_page_renders_comment_form_for_logged_in(self) -> None:
        html = T.album_detail_page(
            T.SiteContext(csrf_token="tok", user={"username": "alice", "role": "viewer"}),
            album={"key": "k1", "title": "T", "href": "/album/k1", "circle": "C"},
            comments=[{"username": "bob", "body": "好听", "created_at_text": "2026-01-01"}],
            circle_name="C",
        )
        self.assertIn("评论", html)
        self.assertIn("好听", html)
        self.assertIn("/album/k1/comment", html)
        self.assertIn('name="album_key"', html)

    def test_album_page_hides_form_for_anonymous(self) -> None:
        html = T.album_detail_page(
            T.SiteContext(csrf_token="tok"),
            album={"key": "k1", "title": "T", "href": "/album/k1", "circle": "C"},
            circle_name="C",
        )
        self.assertNotIn("/album/k1/comment", html)
        self.assertIn("还没有评论", html)

    def test_follow_button_toggles_label(self) -> None:
        """未关注显示「关注」, 已关注显示「已关注」."""
        base = T.album_detail_page(
            T.SiteContext(csrf_token="tok", user={"username": "a"}),
            album={"key": "k", "title": "T", "href": "/album/k", "circle": "C"},
            circle_name="C", following_circle=False,
            entities=[{"kind": "circle", "name": "C", "href": "/circle/C",
                       "following": False}],
        )
        followed = T.album_detail_page(
            T.SiteContext(csrf_token="tok", user={"username": "a"}),
            album={"key": "k", "title": "T", "href": "/album/k", "circle": "C"},
            circle_name="C", following_circle=True,
            entities=[{"kind": "circle", "name": "C", "href": "/circle/C",
                       "following": True}],
        )
        # 社团的关注按钮现在在标题后(detail-circle 内)
        self.assertIn("circle-follow", base)
        self.assertIn(">关注<", base)
        self.assertIn("已关注", followed)
        self.assertIn('action="/follows/toggle"', base)

    def test_circle_page_has_follow_button(self) -> None:
        html = T.circle_page(
            T.SiteContext(csrf_token="tok", user={"username": "a"}),
            name="Sample Circle", items=[], pager={"total": 0},
            url_for=lambda *a, **k: "/circles",
        )
        self.assertIn('action="/follows/toggle"', html)
        self.assertIn("关注这个社团", html)

class TestArtistFollowAndCoverUpload(unittest.TestCase):
    def test_cover_route_parses(self) -> None:
        self.assertEqual(S.parse_route("/album/k/cover")["name"], "album_cover")
        self.assertEqual(S.parse_route("/album/k/cover")["arg"], "k")

    def test_cover_upload_moved_to_edit_page(self) -> None:
        """封面上传从专辑页搬到编辑页."""
        album = {"key": "k", "title": "T", "href": "/album/k", "circle": "", "artists": [],
                 "tracks": [], "credits": [], "urls": [], "tags": [], "genres": [],
                 "missing": [], "cover": "", "has_cover": False, "date": "", "event": "",
                 "catalog_no": "", "price": "", "hall": "", "booth": "", "updated_text": "",
                 "title_alt": "", "romaji": "", "event_kind": "", "year": "",
                 "description": "", "track_count": 0}
        detail = T.album_detail_page(
            T.SiteContext(csrf_token="t", user={"username": "ed", "role": "editor"}),
            album=album, cover_upload=True,
        )
        self.assertNotIn('name="cover"', detail, "专辑页不再有上传封面")
        edit = T.edit_album_page(
            T.SiteContext(csrf_token="t", user={"username": "ed", "role": "editor"}),
            album=album, values={},
        )
        self.assertIn('name="cover"', edit)
        self.assertIn("/cover", edit)

    def test_artist_follow_buttons(self) -> None:
        """关注按钮搬进「艺人与社团」悬停卡片(不再有底部关注块)."""
        html = T.album_detail_page(
            T.SiteContext(csrf_token="t", user={"username": "v"}),
            album={"key": "k", "title": "T", "href": "/album/k", "circle": "C",
                   "artists": ["Alpha", "Beta"]},
            circle_name="C", following_artists=["Alpha"],
            entities=[
                {"kind": "circle", "name": "C", "href": "/circle/C", "meta": [("专辑", "2 张")],
                 "following": False},
                {"kind": "artist", "name": "Alpha", "href": "/albums?q=Alpha",
                 "meta": [("专辑", "1 张")], "following": True},
                {"kind": "artist", "name": "Beta", "href": "/albums?q=Beta",
                 "meta": [("专辑", "1 张")], "following": False},
            ],
        )
        # 制作人员 = 按钮; **有实体的艺人用 entity-chip(带悬停卡片 + 关注表单)**
        self.assertIn("people-chips", html)
        self.assertIn("entity-chip", html, "有实体的艺人要能关注")
        self.assertIn('action="/follows/toggle"', html, "关注表单要在")
        self.assertIn('class="detail-circle"', html)
        self.assertIn("Alpha", html)
        self.assertIn("Beta", html)
        self.assertNotIn("关注这个社团", html, "底部关注块已移除")

    def test_artist_follow_form_count(self) -> None:
        """每个有实体的艺人一个关注表单。"""
        html = T.album_detail_page(
            T.SiteContext(csrf_token="t", user={"username": "v"}),
            album={"key": "k", "title": "T", "href": "/album/k", "circle": "C",
                   "artists": ["Alpha", "Beta", "Gamma"]},
            entities=[
                {"kind": "artist", "name": "Alpha", "href": "/albums?q=Alpha",
                 "following": False},
                {"kind": "artist", "name": "Beta", "href": "/albums?q=Beta",
                 "following": True},
            ],
        )
        # 社团 1 + Alpha + Beta(有实体) = 3 个关注表单
        # Gamma 没有实体卡片 -> 只读 chip, 不可关注
        self.assertEqual(html.count('action="/follows/toggle"'), 3)
        self.assertIn("Gamma", html)
        self.assertIn("entity-item", html, "有实体的艺人")
        self.assertIn("credit-chip", html, "无实体的艺人退化为只读")

    def test_no_entity_falls_back_to_readonly_chip(self) -> None:
        """没有实体卡片的艺人退化为只读 credit-chip(不能关注)。"""
        html = T.album_detail_page(
            T.SiteContext(csrf_token="t", user={"username": "v"}),
            album={"key": "k", "title": "T", "href": "/album/k",
                   "artists": ["只有名字"]},
            entities=[],
        )
        self.assertIn('class="credit-chip"', html)
        self.assertNotIn('action="/follows/toggle"', html)

    def test_cover_module_path_and_extension_guard(self) -> None:
        """封面只允许 PNG/JPEG/WebP; 文件名由 key 清洗而来."""
        import re as _re
        stem = _re.sub(r"[^A-Za-z0-9._-]+", "-", "a b/c|d")[:80]
        self.assertEqual(stem, "a-b-c-d")

class TestNotificationBadgeAndPublicProfile(unittest.TestCase):
    def test_nav_shows_unread_badge(self) -> None:
        html = T.layout(
            T.SiteContext(csrf_token="t", user={"username": "u", "role": "viewer"},
                          messages_unread=2, notifications_unread=1),
            title="x", body="")
        self.assertIn("nav-badge", html)
        self.assertIn(">3<", html.split("nav-badge", 1)[1][:40])

    def test_nav_hides_badge_when_zero(self) -> None:
        html = T.layout(
            T.SiteContext(csrf_token="t", user={"username": "u", "role": "viewer"}),
            title="x", body="")
        self.assertNotIn("nav-badge", html)

    def test_public_profile_respects_show_flags(self) -> None:
        user = {"username": "alice", "role": "viewer"}
        public = T.public_profile_page(
            T.SiteContext(), user=user,
            visibility={"vis_bio": 0, "vis_birthday": 3, "vis_favorites": 0,
                        "vis_follows": 0, "vis_contributions": 0}, viewer="anon",
            profile={"bio": "我的简介", "birthday": "1990-01-01"},
            favorites=[{"album_key": "k1", "title": "Album One"}],
            follows=[{"kind": "circle", "key": "Cir"}],
            contributions=[{"kind": "edit", "summary": "改资料", "created_at_text": "2026-01-01"}],
        )
        self.assertIn("我的简介", public)
        self.assertNotIn("1990-01-01", public)
        self.assertIn("Album One", public)
        self.assertIn("Cir", public)
        self.assertIn("改资料", public)

        hidden = T.public_profile_page(
            T.SiteContext(), user=user,
            visibility={"vis_bio": 3, "vis_birthday": 0, "vis_favorites": 3,
                        "vis_follows": 3, "vis_contributions": 3}, viewer="anon",
            profile={"bio": "我的简介", "birthday": "1990-01-01"},
            favorites=[{"album_key": "k1", "title": "Album One"}],
            follows=[{"kind": "circle", "key": "Cir"}],
            contributions=[{"kind": "edit", "summary": "改资料"}],
        )
        self.assertNotIn("我的简介", hidden)
        self.assertIn("1990-01-01", hidden)
        self.assertNotIn("Album One", hidden)

    def test_contributions_are_linked_to_album(self) -> None:
        html = T.account_page(
            T.SiteContext(csrf_token="t"),
            user={"id": 1, "username": "e", "role": "editor", "role_label": "编辑者"},
            profile={},
            contributions=[{"kind": "cover", "target": "my-album",
                            "summary": "封面", "created_at_text": "2026-01-01"}],
            tab="contributions",
        )
        self.assertIn('href="/album/my-album"', html)

    def test_notify_hooks_present_in_source(self) -> None:
        src = (ROOT / "dnh" / "public" / "site.py").read_text(encoding="utf-8")
        self.assertIn('"notify"', src)
        self.assertIn("你的编辑者申请已通过", src)
        self.assertIn("你收到了新的私信" if "你收到了新的私信" in src else "新的私信", src)

class TestThreadsAndCommentActions(unittest.TestCase):
    def test_routes(self) -> None:
        self.assertEqual(S.parse_route("/comment/delete")["name"], "comment_delete")
        self.assertEqual(S.parse_route("/comment/report")["name"], "comment_report")
        self.assertIn("comment_delete", S._MEMBER_WRITE_ROUTES)
        self.assertIn("comment_report", S._MEMBER_WRITE_ROUTES)

    def test_own_comment_can_be_deleted(self) -> None:
        html = T.album_detail_page(
            T.SiteContext(csrf_token="t", user={"username": "alice"}),
            album={"key": "k", "title": "T", "href": "/album/k"},
            comments=[{"id": 5, "username": "alice", "body": "我的评论"}],
        )
        self.assertIn('action="/comment/delete"', html)
        self.assertIn('name="comment_id" value="5"', html)

    def test_other_comment_can_be_reported(self) -> None:
        html = T.album_detail_page(
            T.SiteContext(csrf_token="t", user={"username": "alice"}),
            album={"key": "k", "title": "T", "href": "/album/k"},
            comments=[{"id": 6, "username": "bob", "body": "别人的评论"}],
        )
        self.assertIn('action="/comment/report"', html)
        self.assertNotIn('action="/comment/delete"', html)

    def test_anonymous_sees_no_comment_actions(self) -> None:
        html = T.album_detail_page(
            T.SiteContext(csrf_token="t"),
            album={"key": "k", "title": "T", "href": "/album/k"},
            comments=[{"id": 6, "username": "bob", "body": "x"}],
        )
        self.assertNotIn("/comment/delete", html)
        self.assertNotIn("/comment/report", html)

    def test_conversations_and_thread_pages(self) -> None:
        conv = T.messages_page(
            T.SiteContext(csrf_token="t", user={"username": "a"}),
            conversations=[{"peer_name": "bob", "unread": 2, "last": "你好", "last_at": "10:00"}])
        self.assertIn("会话", conv)
        self.assertIn("/messages?peer=bob", conv)
        self.assertIn("nav-badge", conv)

        thread = T.message_thread_page(
            T.SiteContext(csrf_token="t", user={"username": "a"}),
            peer_name="bob",
            messages=[{"direction": "in", "body": "在吗", "created_at_text": "10:00"},
                      {"direction": "out", "body": "在", "created_at_text": "10:01"}])
        self.assertIn("与 bob 的私信", thread)
        self.assertIn("在吗", thread)
        self.assertIn('action="/messages/send"', thread)
        self.assertIn('name="to" value="bob"', thread)

class TestAlbumPagePolish(unittest.TestCase):
    def test_credits_are_deduped(self) -> None:
        """信用卡/词条去重; 社团类词条不进制作人员区块."""
        html = T.album_detail_page(
            T.SiteContext(csrf_token="t"),
            album={"key": "k", "title": "T", "href": "/album/k", "circle": "C",
                   "credits": [{"label": "社团", "names": ["C", "C", "C"]},
                               {"label": "编曲", "names": ["X、X", "X"]}]},
        )
        # 社团不进制作者区块(只在标题后)
        self.assertNotIn('class="credit-chip-kind">社团', html)
        # 普通制作人员去重后只出现一次(现在用 chip 不是 <dd>)
        self.assertIn("<b>X</b>", html)
        self.assertNotIn("X、X", html)

    def test_tracklist_has_key_and_hint(self) -> None:
        html = T.album_detail_page(
            T.SiteContext(csrf_token="t"),
            album={"key": "k", "title": "T", "href": "/album/k",
                   "tracks": [{"index": 1, "title": "t1", "artists": ["a"],
                               "duration_text": "1:00"}]},
        )
        self.assertIn("data-key=", html)
        self.assertNotIn("拖动表头", html)   # 用户要求删掉这段提示
        self.assertIn("col-title", html)

    def test_css_and_js_are_versioned(self) -> None:
        page = T.layout(T.SiteContext(), title="x", body="")
        self.assertRegex(page, r"/static/public\.css\?v=\d+")
        self.assertRegex(page, r"/static/public\.js\?v=\d+")

