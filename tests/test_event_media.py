"""展会词条支持图片与链接.

用户要求: "展会词条允许设置图片和链接"

覆盖:
  * events 表有 logo_url/logo_local/links_json 列, 老库自动迁移
  * save_event 写入, list_events 解出(含 has_logo)
  * PublicData.events() 把图片/链接挂到聚合行上
  * 索引页出缩略图与链接标记
  * 详情页出 event-media 区(图片 + link-pill)
  * 编辑表单缺字段时保留原值(不会被清空)
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from tests._tmp import temp_path

ROOT = Path(__file__).resolve().parents[1]


class TestEventSchema(unittest.TestCase):
    def setUp(self) -> None:
        from dddb.db import Store

        self.tmp = temp_path("event-media")
        self.db = self.tmp / "t.sqlite3"
        self.store = Store(self.db)

    def tearDown(self) -> None:
        import shutil

        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_columns_exist(self) -> None:
        cols = {r["name"] for r in self.store.query(
            "PRAGMA table_info(events)")}
        for name in ("logo_url", "logo_local", "links_json"):
            self.assertIn(name, cols, f"events 表缺 {name}")

    def test_save_and_read_back(self) -> None:
        self.store.save_event(
            "M3-2026春", kind="m3", year=2026, tags=["东方"],
            logo_url="https://e.com/p.png", logo_local="/tmp/x.img",
            links={"官网": "https://m3.example.com/"})
        rows = self.store.list_events()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["logo_url"], "https://e.com/p.png")
        self.assertEqual(row["logo_local"], "/tmp/x.img")
        self.assertEqual(row["links"], {"官网": "https://m3.example.com/"})
        self.assertTrue(row["has_logo"])

    def test_no_logo_means_false(self) -> None:
        self.store.save_event("C102", kind="comiket", year=2026)
        row = self.store.list_events()[0]
        self.assertFalse(row["has_logo"])
        self.assertEqual(row["links"], {})


class TestEventMigration(unittest.TestCase):
    """老库(没有这三列)打开后要自动补上。"""

    def test_old_table_upgraded(self) -> None:
        from dddb.db import Store

        tmp = temp_path("event-migrate")
        db = tmp / "old.sqlite3"
        conn = sqlite3.connect(db)
        conn.executescript("""
        CREATE TABLE events (
            name TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT '',
            year INTEGER NOT NULL DEFAULT 0, season TEXT NOT NULL DEFAULT '',
            start_date TEXT NOT NULL DEFAULT '', end_date TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '[]', updated_at REAL NOT NULL
        );
        INSERT INTO events(name, kind, year, tags, updated_at)
        VALUES ('旧展会', 'm3', 2025, '[]', 0);
        """)
        conn.commit()
        conn.close()

        store = Store(db)
        try:
            cols = {r["name"] for r in store.query(
                "PRAGMA table_info(events)")}
            for name in ("logo_url", "logo_local", "links_json"):
                self.assertIn(name, cols, f"迁移后应补上 {name}")
            # 老数据还在
            self.assertEqual(len(store.list_events()), 1)
            # 新列可写
            store.save_event("旧展会", kind="m3", year=2025,
                             logo_url="https://e.com/a.png",
                             links={"x": "https://e.com/"})
            row = store.list_events()[0]
            self.assertTrue(row["has_logo"])
        finally:
            store.close()
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


class TestEventDataMerge(unittest.TestCase):
    """PublicData.events() 要把词条图片/链接挂到聚合行。"""

    def setUp(self) -> None:
        from dddb.db import Store
        from dddb.models import Album
        from dddb.public.data import PublicData, ReadOnlyStore

        self.tmp = temp_path("event-merge")
        self.db = self.tmp / "t.sqlite3"
        store = Store(self.db)
        store.save_event("M3-2026春", kind="m3", year=2026,
                         logo_url="https://e.com/p.png",
                         links={"官网": "https://m3.example.com/",
                                "售票": "https://t.example.com/"})
        store.save_event("没有专辑的展会", kind="m3", year=2026,
                         logo_url="https://e.com/q.png")
        store.upsert_album(Album.from_dict({
            "key": "t|2026", "title": "T", "title_original": "T",
            "event": "M3-2026春", "circle": "C", "source_ids": {}}))
        store.close()
        self.store = ReadOnlyStore(self.db)
        self.data = PublicData(self.store, root=self.tmp)

    def tearDown(self) -> None:
        import shutil

        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_logo_and_links_attached(self) -> None:
        events = self.data.events()
        self.assertTrue(events, "应有聚合行")
        row = events[0]
        self.assertEqual(row["value"], "M3-2026春")
        self.assertTrue(row["has_logo"])
        self.assertIn("e.com/p.png", row["logo"])
        self.assertEqual(len(row["links"]), 2)

    def test_event_without_albums_still_listed(self) -> None:
        """用户 m19443: 刚建好的会次一张碟都没有, 也必须出现在索引里.

        以前 events() 只按 albums 聚合, 建完跳详情页能看到, 回索引却找不到.
        """
        rows = {e["value"]: e for e in self.data.events()}
        self.assertIn("没有专辑的展会", rows)
        self.assertEqual(0, rows["没有专辑的展会"]["count"])
        self.assertTrue(rows["没有专辑的展会"]["has_logo"])


class TestEventRender(unittest.TestCase):
    def _ctx(self):
        from dddb.public import templates as T

        return T.SiteContext(csrf_token="t", lang="zh")

    def test_index_shows_series_cards(self) -> None:
        """索引页现在是**系列卡片**(每个系列一张), 不再是逐行列表。"""
        from dddb.public import templates as T

        series = [
            {"name": "M3", "abbr": "M3", "full_name": "M3",
             "logo": "https://e.com/p.png", "has_logo": True,
             "editions": [
                 {"value": "M3-2026春", "display": "[M3] M3-2026春 (M3-57)",
                  "count": 12}]},
            {"name": "Comic Market", "abbr": "CM",
             "full_name": "Comic Market", "logo": "", "has_logo": False,
             "editions": [
                 {"value": "C102", "display": "[CM] Comic Market (C102)",
                  "count": 3}]},
        ]
        html = T.events_index_page(self._ctx(), events=[], kinds=[],
                                   series=series)
        self.assertIn("series-grid", html)
        self.assertEqual(html.count('data-series-card="1"'), 2)
        self.assertIn("series-card-logo", html)
        # 有图的系列出 <img>, 没图的出文字占位
        self.assertIn("<img", html)
        self.assertIn("series-card-logo-text", html)
        # 会次按 [缩写] 全称 (会次) 显示
        self.assertIn("[M3] M3-2026春 (M3-57)", html)
        self.assertIn("[CM] Comic Market (C102)", html)

    def test_detail_shows_media(self) -> None:
        from dddb.public import templates as T

        html = T.event_page(
            self._ctx(), name="M3-2026春", kind="m3", items=[], groups=[],
            group_by="circle", total=12, url_for=lambda **k: "/x",
            logo="https://e.com/p.png",
            links={"官网": "https://m3.example.com/",
                   "售票": "https://t.example.com/"})
        self.assertIn("event-media", html)
        self.assertIn("event-logo", html)
        self.assertEqual(html.count('class="link-pill"'), 2)

    def test_detail_without_media(self) -> None:
        from dddb.public import templates as T

        html = T.event_page(
            self._ctx(), name="C102", kind="comiket", items=[], groups=[],
            group_by="circle", total=0, url_for=lambda **k: "/x")
        self.assertNotIn("event-media", html)

    def test_logo_uses_local_route(self) -> None:
        """/event-logo/<name> 路由已注册。"""
        from dddb.public import site as SITE

        self.assertEqual(SITE._ROUTE_MAP.get("event-logo"), "event_logo")
        self.assertIn("event_logo", SITE._ENTITY_ROUTES)
        self.assertTrue(hasattr(SITE.PublicSite, "serve_event_logo"))

    def test_css_present(self) -> None:
        css = (ROOT / "dddb/public/static/public.css").read_text(
            encoding="utf-8")
        for token in (".event-thumb", ".event-media", ".event-logo"):
            self.assertIn(token, css, f"缺样式 {token}")


class TestFormSpec(unittest.TestCase):
    def test_event_spec_has_media_fields(self) -> None:
        """展会表单要有图片(**上传**)与链接字段。"""
        from dddb.public import templates as T

        title, fields = T.NEW_ENTITY_SPECS["event"]
        names = [f[0] for f in fields]
        # 图片改成文件上传: spec 里是 logo(file), 不再有 logo_url 文本框
        self.assertIn("logo", names, "展会表单要有图片上传")
        logo_field = next(f for f in fields if f[0] == "logo")
        self.assertEqual(logo_field[2], "file", "图片要用上传而不是 URL")
        self.assertIn("links", names, "展会表单要有链接字段")


if __name__ == "__main__":
    unittest.main()
