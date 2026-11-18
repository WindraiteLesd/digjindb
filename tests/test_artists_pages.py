"""测试: 国旗(台湾→中国)/艺术家页/社团页/私信开关."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests._tmp import temp_dir
from dddb.public import templates as T


class TestFlags(unittest.TestCase):
    def test_taiwan_is_never_used_and_shows_china(self) -> None:
        self.assertEqual(T.country_flag("TW"), "🇨🇳")
        self.assertEqual(T.country_flag("tw"), "🇨🇳")
        self.assertNotEqual(T.country_flag("TW"), "🇹🇼")

    def test_known_and_unknown(self) -> None:
        self.assertEqual(T.country_flag("JP"), "🇯🇵")
        self.assertEqual(T.country_flag("cn"), "🇨🇳")
        self.assertEqual(T.country_flag(""), "")
        self.assertEqual(T.country_flag("XYZ"), "")


class TestArtistPages(unittest.TestCase):
    def _artist(self, **extra):
        data = {
            "name": "テスト", "album_count": 3, "albums": [
                {"key": "k1", "title": "A1"}, {"key": "k2", "title": "A2"}],
            "circles": ["Circle X"], "occupations": ["作曲", "编曲"],
            "relations": ["Other"], "bio": "简介文本", "nationality": "TW",
            "real_name": "本名", "aliases": ["别名甲"], "links": [],
        }
        data.update(extra)
        return data

    def test_artist_detail_has_all_columns(self) -> None:
        html = T.artist_detail_page(
            T.SiteContext(csrf_token="t", user={"username": "me"}),
            artist=self._artist(), followers=7, following=True,
        )
        self.assertIn("profile-card", html)
        self.assertIn("参与专辑", html)
        self.assertIn("人际关系", html)
        self.assertIn("简介", html)
        self.assertIn("已关注", html)
        self.assertIn('action="/artist/%E3%83%86%E3%82%B9%E3%83%88/comment"', html)
        # 台湾 -> 中国国旗, 不出现青天白日旗
        self.assertIn("🇨🇳", html)
        self.assertNotIn("🇹🇼", html)
        self.assertIn("别名甲", html)
        self.assertIn("本名", html)

    def test_artist_without_optional_fields(self) -> None:
        html = T.artist_detail_page(
            T.SiteContext(csrf_token="t"),
            artist={"name": "无名", "album_count": 0, "albums": [],
                   "circles": [], "occupations": [], "relations": []},
        )
        self.assertIn("profile-card", html)
        self.assertIn("还没有参与专辑", html)


class TestArtistIndex(unittest.TestCase):
    def test_index_cards_and_pager(self) -> None:
        html = T.artists_index_page(
            T.SiteContext(csrf_token="t"),
            artists=[{"name": "Alpha", "album_count": 2, "followers": 5},
                     {"name": "Beta", "album_count": 1, "followers": 0}],
            pager={"page": 1, "per": 90, "total": 200, "pages": 3},
            query="",
        )
        self.assertEqual(html.count("artist-card"), 2)
        self.assertIn("/artist/Alpha", html)
        self.assertIn("下一页", html)


class TestCirclePage(unittest.TestCase):
    def test_circle_detail_people_and_albums(self) -> None:
        html = T.circle_detail_page(
            T.SiteContext(csrf_token="t", user={"username": "me"}),
            circle={
                "name": "社团X", "albums": [{"key": "k1", "title": "A"}],
                "members": [{"name": "成员甲", "roles": ["作曲"], "albums": 3}],
                "former_members": [{"name": "旧成员", "roles": [], "albums": 2}],
                "guests": [{"name": "客串乙", "roles": ["作词"], "albums": 1}],
                "first_event": "C90", "last_event": "C102",
                "latest_year": 2026, "links": [], "description": "社团简介",
                "nationality": "JP",
            },
            followers=11, following=False,
        )
        self.assertIn("profile-card", html)
        self.assertIn("相关人士", html)
        self.assertIn("过往成员", html)
        self.assertIn("客串", html)
        self.assertIn("成员甲", html)
        self.assertIn("首次活动", html)
        self.assertIn('action="/circle/%E7%A4%BE%E5%9B%A2X/comment"', html)
        self.assertIn("🇯🇵", html)


class TestAcceptMessages(unittest.TestCase):
    def test_switch_default_and_persist(self) -> None:
        ctx = temp_dir("dm-")
        root = Path(ctx.__enter__())
        try:
            from dddb.public.profile import ProfileStore
            store = ProfileStore(root / "p.sqlite3")
            try:
                self.assertTrue(store.accepts_messages(5), "默认接受私信")
                ok, _m = store.set_accept_messages(5, "0")
                self.assertTrue(ok)
                self.assertFalse(store.accepts_messages(5))
                store.set_accept_messages(5, "1")
                self.assertTrue(store.accepts_messages(5))
            finally:
                store.close()
        finally:
            ctx.__exit__(None, None, None)

    def test_privacy_page_has_switch(self) -> None:
        html = T.account_page(
            T.SiteContext(csrf_token="t"),
            user={"id": 1, "username": "u", "role": "viewer", "role_label": "用户"},
            profile={"accept_messages": 0}, visibility={}, tab="privacy",
        )
        self.assertIn('name="accept_messages"', html)
        self.assertIn("接受他人私信", html)
        # 旧的「私信状态」可见性档位不再出现
        self.assertNotIn("私信状态", html)


class TestRoutes(unittest.TestCase):
    def test_artist_and_circle_routes(self) -> None:
        from dddb.public import site as S
        self.assertEqual(S.parse_route("/artists")["name"], "artists")
        self.assertEqual(S.parse_route("/artist/abc")["name"], "artist")
        self.assertEqual(S.parse_route("/artist/abc/comment")["name"], "artist_comment")
        self.assertEqual(S.parse_route("/circle/abc/comment")["name"], "circle_comment")
        for name in ("artist_comment", "circle_comment"):
            self.assertIn(name, S._MEMBER_WRITE_ROUTES)


if __name__ == "__main__":
    unittest.main()