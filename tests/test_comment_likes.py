"""补测: 评论点赞 DB(自赞拒绝/计数/取消) + 评论渲染自赞隐藏; README 本轮说明."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests._tmp import temp_dir
from dddb.public import templates as T


class TestCommentLikes(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_dir("likes-")
        self.root = self._ctx.__enter__()
        from dddb.db import Store
        self.store = Store(Path(self.root) / "db.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self._ctx.__exit__(None, None, None)

    def test_like_toggle_and_counts(self) -> None:
        ok, _m, cid = self.store.add_comment(target_kind="album", target_key="k",
                                             user_id=1, username="alice", body="hi")
        self.assertTrue(ok)
        ok, liked, count, _m = self.store.toggle_comment_like(cid, "bob")
        self.assertTrue(ok)
        self.assertTrue(liked)
        self.assertEqual(count, 1)
        self.assertEqual(self.store.comment_like_counts([cid]), {int(cid): 1})
        self.assertEqual(self.store.liked_comment_ids("bob", [cid]), {int(cid)})
        ok, liked, count, _m = self.store.toggle_comment_like(cid, "bob")
        self.assertTrue(ok)
        self.assertFalse(liked)
        self.assertEqual(count, 0)

    def test_cannot_like_own_comment(self) -> None:
        ok, _m, cid = self.store.add_comment(target_kind="album", target_key="k",
                                             user_id=1, username="alice", body="hi")
        self.assertTrue(ok)
        ok, _liked, _count, message = self.store.toggle_comment_like(cid, "alice")
        self.assertFalse(ok)
        self.assertIn("不能给自己", message)
        self.assertEqual(self.store.comment_like_counts([cid]), {})

    def test_missing_comment_is_reported(self) -> None:
        ok, _l, _c, message = self.store.toggle_comment_like(999999, "bob")
        self.assertFalse(ok)
        self.assertIn("不存在", message)


class TestCommentMarkup(unittest.TestCase):
    def test_avatar_time_like_and_self_hidden(self) -> None:
        html = T.comments_section(
            T.SiteContext(csrf_token="t", user={"username": "me"}),
            album={"key": "k"},
            comments=[
                {"id": 1, "username": "other", "user_id": 7,
                 "created_at_text": "2026-01-01", "body": "a"},
                {"id": 2, "username": "me", "user_id": 8,
                 "created_at_text": "2026-01-02", "body": "b"},
            ],
            comment_likes={1: 3, 2: 1},
        )
        self.assertIn("comment-avatar", html)
        self.assertIn("comment-meta", html)
        self.assertIn('action="/like"', html)
        self.assertIn('name="comment_id" value="1"', html)
        # 自己的评论只有静态赞数(其它表单里的 comment_id 不算点赞)
        self.assertEqual(html.count('class="inline-form comment-like"'), 1)
        self.assertIn("comment-like-static", html)
        self.assertIn("赞 1", html)

    def test_cover_upload_not_in_comments(self) -> None:
        html = T.comments_section(
            T.SiteContext(csrf_token="t", user={"username": "me"}),
            album={"key": "k"}, cover_upload=True)
        self.assertNotIn('name="cover"', html)


if __name__ == "__main__":
    unittest.main()
