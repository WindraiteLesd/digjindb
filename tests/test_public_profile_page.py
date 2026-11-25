"""测试: 公开资料页对其他用户的展示."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _page(**overrides):
    from dddb.public import templates as T

    kwargs = {
        "user": {"id": 35, "username": "hhdaf", "role": "editor"},
        "profile": {"bio": "我爱好音乐", "birthday": "2000-01-01"},
        "visibility": {
            "vis_bio": 0, "vis_birthday": 0, "vis_favorites": 0,
            "vis_follows": 0, "vis_contributions": 0,
        },
        "viewer": "member",
        "favorites": [{"album_key": "k", "title": "某专辑",
                       "cover_src": "/cover/k"}],
        "follows": [{"kind": "circle", "key": "某社团"},
                    {"kind": "artist", "key": "ARForest"}],
        "contributions": [{"kind": "edit", "summary": "某专辑",
                           "created_at_text": "2026-11-16 10:00"}],
        "counts": {"favorites": 3, "follows": 5, "contributions": 11},
        "joined_text": "2026-11-13 12:16",
        "active_text": "2026-11-16 18:00",
    }
    kwargs.update(overrides)
    ctx = T.SiteContext(csrf_token="t", is_editor=False, lang="zh")
    return T.public_profile_page(ctx, **kwargs)


class TestProfileHead(unittest.TestCase):
    """资料页头部: 头像 + 名字 + 身份 + 元信息."""

    def setUp(self) -> None:
        self.html = _page()

    def test_avatar_shown(self) -> None:
        self.assertIn("pub-profile-avatar", self.html)
        self.assertIn("/avatar/35", self.html,
                      "要显示用户头像")
        self.assertIn("pub-profile-avatar-img", self.html)

    def test_username_and_badge(self) -> None:
        self.assertIn("hhdaf", self.html)
        # 身份徽章(layout 渲染 h1, 卡片里显示名字 + 徽章)
        self.assertIn("ident-badge", self.html)
        # 不能重复输出 h1
        self.assertEqual(len(re.findall(r"<h1", self.html)), 1)

    def test_joined_time(self) -> None:
        self.assertIn("加入于", self.html)
        self.assertIn("2026-11-13", self.html)

    def test_last_active(self) -> None:
        self.assertIn("最后活跃", self.html)

    def test_stats_shown(self) -> None:
        self.assertIn("收藏 3", self.html)
        self.assertIn("关注 5", self.html)
        self.assertIn("贡献 11", self.html)

    def test_no_follower_count(self) -> None:
        """本站不支持关注用户, 不显示"粉丝"(永远是 0)."""
        self.assertNotIn("粉丝", self.html)


class TestProfileSections(unittest.TestCase):
    """各区块按隐私档位展示."""

    def test_bio(self) -> None:
        self.assertIn("我爱好音乐", _page())

    def test_birthday(self) -> None:
        self.assertIn("2000-01-01", _page())

    def test_favorites_with_links(self) -> None:
        html = _page()
        self.assertIn("收藏", html)
        self.assertIn("/album/k", html)
        self.assertIn("某专辑", html)

    def test_follows_with_links(self) -> None:
        html = _page()
        self.assertIn("关注", html)
        self.assertIn("某社团", html)
        self.assertIn("ARForest", html)
        # 社团/艺人要给站内链接(卡片化之后是 /circle/ 与 /artist/)
        self.assertIn("/circle/", html)
        self.assertIn("/artist/", html)
        self.assertIn('class="follow-card"', html)

    def test_contributions(self) -> None:
        html = _page()
        self.assertIn("贡献", html)
        self.assertIn("2026-11-16 10:00", html)

    def test_privacy_hides_sections(self) -> None:
        """档位 3 = 不可见."""
        html = _page(visibility={
            "vis_bio": 3, "vis_birthday": 3, "vis_favorites": 3,
            "vis_follows": 3, "vis_contributions": 3,
        })
        self.assertNotIn("我爱好音乐", html)
        self.assertNotIn("2000-01-01", html)
        self.assertNotIn("某专辑", html)
        self.assertNotIn("某社团", html)


class TestProfileEmptyStates(unittest.TestCase):
    """数据少时也要像样, 不堆砌 0."""

    def test_no_zero_pile(self) -> None:
        html = _page(counts={"favorites": 0, "follows": 0,
                             "contributions": 0})
        self.assertNotIn("收藏 0", html)
        self.assertNotIn("关注 0", html)
        self.assertNotIn("贡献 0", html)

    def test_only_contributions(self) -> None:
        """这正是用户反馈的情况: 只有编辑历史."""
        html = _page(profile={}, favorites=[], follows=[],
                     counts={"favorites": 0, "follows": 0,
                             "contributions": 2})
        # 头部信息仍然完整
        self.assertIn("hhdaf", html)
        self.assertIn("/avatar/35", html)
        self.assertIn("加入于", html)
        self.assertIn("贡献 2", html)

    def test_hint_when_nothing_public(self) -> None:
        html = _page(profile={}, favorites=[], follows=[],
                     contributions=[])
        self.assertIn("还没有公开", html)

    def test_no_avatar_when_no_id(self) -> None:
        html = _page(user={"id": 0, "username": "x", "role": "viewer"})
        self.assertNotIn("/avatar/0", html)


class TestProfileCss(unittest.TestCase):
    def test_styles_exist(self) -> None:
        css = (ROOT / "dddb/public/static/public.css").read_text(
            encoding="utf-8")
        for token in (".pub-profile-head", ".pub-profile-avatar",
                      ".pub-profile-meta", ".pub-profile-head-card"):
            self.assertIn(token, css, f"缺样式 {token}")

    def test_no_conflict_with_account_page(self) -> None:
        """账号页用 .profile-*, 资料页用 .pub-profile-*, 不能互相影响."""
        css = (ROOT / "dddb/public/static/public.css").read_text(
            encoding="utf-8")
        # 账号页的 .profile-avatar 仍存在(96px)
        self.assertIn(".profile-avatar {", css)
        # 资料页用独立前缀
        self.assertIn(".pub-profile-avatar {", css)


if __name__ == "__main__":
    unittest.main()
