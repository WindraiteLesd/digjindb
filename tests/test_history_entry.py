"""测试: 修改记录入口 + 历史页渲染 + 还原链路."""

from __future__ import annotations

import unittest
from pathlib import Path

from tests._tmp import temp_dir
from dddb.public import templates as T


def _album() -> dict:
    return {"key": "k", "title": "T", "href": "/album/k", "circle": "C",
            "artists": [], "tracks": [], "credits": [], "urls": [], "tags": [],
            "genres": [], "missing": [], "cover": "", "has_cover": False,
            "date": "", "event": "", "catalog_no": "", "price": "", "hall": "",
            "booth": "", "updated_text": "", "title_alt": "", "romaji": "",
            "event_kind": "", "year": "", "description": "", "track_count": 0}


class TestHistoryEntry(unittest.TestCase):
    def _editor(self):
        return T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                             user={"username": "ed", "role": "editor"})

    def test_album_page_has_history_link_for_editor(self) -> None:
        html = T.album_detail_page(self._editor(), album=_album(), can_edit=True)
        self.assertIn("/album/k/history", html, "专辑页要有「修改记录」入口")
        self.assertIn("修改记录", html)
        # 与编辑按钮相邻出现
        self.assertIn('href="/album/k/edit"', html)

    def test_viewer_has_no_history_link(self) -> None:
        viewer = T.SiteContext(csrf_token="t", is_editor=False, lang="zh",
                               user={"username": "v", "role": "viewer"})
        html = T.album_detail_page(viewer, album=_album(), can_edit=False)
        self.assertNotIn("/album/k/history", html)


class TestHistoryPage(unittest.TestCase):
    def _ctx(self):
        return T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                             user={"username": "ed", "role": "editor"})

    def test_renders_version_table(self) -> None:
        revisions = [
            {"id": 2, "created_at_text": "2026-01-02 10:00", "username": "bob",
             "summary": "改标题"},
            {"id": 1, "created_at_text": "2026-01-01 09:00", "username": "alice",
             "summary": "新建"},
        ]
        html = T.history_page(self._ctx(), album=_album(), revisions=revisions)
        self.assertIn("history-table", html)
        self.assertIn("改标题", html)
        self.assertIn("bob", html)
        self.assertIn("还原整版", html)
        self.assertIn("/history?rev=2", html)

    def test_empty_history(self) -> None:
        html = T.history_page(self._ctx(), album=_album(), revisions=[])
        self.assertIn("还没有修订记录", html)

    def test_diff_view(self) -> None:
        revisions = [{"id": 3, "created_at_text": "x", "username": "u", "summary": "s"}]
        html = T.history_page(
            self._ctx(), album=_album(), revisions=revisions,
            selected={"id": 3}, 
            diff_rows=[{"field": "title", "before": "旧", "after": "新"}],
        )
        self.assertIn("diff-table", html)
        self.assertIn("还原此字段", html)
        self.assertIn("title", html)


class TestRevisionStore(unittest.TestCase):
    def test_revision_and_revert(self) -> None:
        ctx = temp_dir("hist-")
        root = Path(ctx.__enter__())
        try:
            from dddb.db import Store
            from dddb.models import Album
            store = Store(root / "db.sqlite3")
            store.upsert_album(Album.from_dict({
                "key": "k", "title": "原始标题", "circle": "C",
                "tracks": [], "source_ids": {}}))
            store.add_revision("k", username="u", summary="初始")
            store.update_album_fields("k", {"title": "新标题"})
            store.add_revision("k", username="u", summary="改动")
            revisions = store.list_revisions("k")
            self.assertGreaterEqual(len(revisions), 2)
            earliest = revisions[-1]
            ok, _message = store.revert_to_revision(int(earliest["id"]))
            self.assertTrue(ok)
            self.assertEqual(store.get_album("k").title, "原始标题")
            store.close()
        finally:
            ctx.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()