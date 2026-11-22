"""编辑历史版本系统测试(存储 + 模板 + 路由)."""

from __future__ import annotations

import unittest
from pathlib import Path

from dddb.db import Store
from dddb.public import site as S
from dddb.public import templates as T
from tests._tmp import temp_dir


class TestRevisionStore(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_dir("rev-")
        self.root = self._ctx.__enter__()
        self.store = Store(self.root / "dddb.sqlite3")
        self.store.upsert_album(_album("k1", title="原始标题", description="原始简介"))

    def tearDown(self) -> None:
        self.store.close()
        self._ctx.__exit__(None, None, None)

    def test_add_list_get(self) -> None:
        rid = self.store.add_revision("k1", user_id=3, username="alice", summary="首次")
        self.assertGreater(rid, 0)
        rows = self.store.list_revisions("k1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["username"], "alice")
        rev = self.store.get_revision(rid)
        self.assertIn("原始标题", rev["payload"])

    def test_revert_whole(self) -> None:
        self.store.add_revision("k1", user_id=3, username="alice", summary="v1")
        ok, _ = self.store.update_album_fields("k1", {"title": "改过的标题"})
        self.assertTrue(ok)
        rid = self.store.list_revisions("k1")[0]["id"]
        ok, msg = self.store.revert_to_revision(rid, user_id=3, username="bob")
        self.assertTrue(ok, msg)
        album = self.store.get_album("k1")
        self.assertEqual(album.title, "原始标题")
        self.assertGreaterEqual(len(self.store.list_revisions("k1")), 2)   # 还原本身也留痕

    def test_restore_single_field(self) -> None:
        self.store.add_revision("k1", user_id=3, username="alice", summary="v1")
        self.store.update_album_fields("k1", {"title": "新标题", "description": "新简介"})
        rid = self.store.list_revisions("k1")[0]["id"]
        ok, msg = self.store.restore_field_from_revision(rid, "title", user_id=3, username="bob")
        self.assertTrue(ok, msg)
        album = self.store.get_album("k1")
        self.assertEqual(album.title, "原始标题")      # 只还原 title
        self.assertEqual(album.description, "新简介")   # description 保持新版

    def test_restore_rejects_unknown_field(self) -> None:
        rid = self.store.add_revision("k1", summary="v1")
        ok, msg = self.store.restore_field_from_revision(rid, "key")
        self.assertFalse(ok)
        self.assertIn("不支持", msg)

    def test_unknown_revision(self) -> None:
        ok, _ = self.store.revert_to_revision(9999)
        self.assertFalse(ok)


def _album(key: str, **kwargs):
    from dddb.models import Album
    data = {"key": key, "title": kwargs.pop("title", "T"), "circle": "C",
            "release_date": "2026-01-01", "tracks": [{"index": 1, "title": "t1"}],
            "source_ids": {"dojinmusic": key}, **kwargs}
    return Album.from_dict(data) if hasattr(Album, "from_dict") else Album(**data)


class TestHistoryTemplate(unittest.TestCase):
    def test_renders_versions_and_diff(self) -> None:
        html = T.history_page(
            T.SiteContext(csrf_token="tok"),
            album={"key": "k1", "title": "T"},
            revisions=[{"id": 2, "created_at_text": "2026-01-02 10:00",
                        "username": "alice", "summary": "改标题"}],
            selected={"id": 2},
            diff_rows=[{"field": "title", "before": "旧", "after": "新"}],
        )
        self.assertIn("编辑历史", html)
        self.assertIn("#2", html)
        self.assertIn("alice", html)
        self.assertIn("还原整版", html)
        self.assertIn("还原此字段", html)
        self.assertIn("title", html)
        self.assertIn('action="/album/revert"', html)

    def test_route_parses(self) -> None:
        self.assertEqual(S.parse_route("/album/k1/history")["name"], "album_history")
        self.assertEqual(S.parse_route("/album/k1/history")["arg"], "k1")
        self.assertEqual(S.parse_route("/album/revert")["name"], "album_revert")
        self.assertIn("album_revert", S._MEMBER_WRITE_ROUTES)


if __name__ == "__main__":
    unittest.main()