"""events.series 外键: ON UPDATE CASCADE / ON DELETE SET NULL.

为什么必须做:
    早先靠手工 ``UPDATE events SET series=?`` 维护关联。系列改名
    ``M3`` -> ``Music Media-Mix Market`` 时漏了一条, 那个会次
    (``M3-2026秋``, 1096 张专辑)的 series 指向了不存在的旧 key,
    ``event_series()`` 查不到它 —— 整届专辑从索引页消失。

有了外键:
    * 改系列名 -> 会次自动跟着改(CASCADE), 不可能漏
    * 删系列   -> 会次的 series 置 NULL(会次本身保留)
"""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path

from tests._tmp import temp_path

ROOT = Path(__file__).resolve().parents[1]


class TestForeignKeySchema(unittest.TestCase):
    """新建的库就带外键。"""

    def setUp(self) -> None:
        from dddb.db import Store

        self.tmp = temp_path("fk-schema")
        self.db = self.tmp / "t.sqlite3"
        self.store = Store(self.db)

    def tearDown(self) -> None:
        import shutil

        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ddl(self) -> str:
        row = self.store.query_one(
            "SELECT sql FROM sqlite_master WHERE name='events'")
        return str(row["sql"] or "")

    def test_has_references(self) -> None:
        self.assertIn("REFERENCES event_series", self._ddl())

    def test_has_on_update_cascade(self) -> None:
        self.assertIn("ON UPDATE CASCADE", self._ddl(),
                      "没有 CASCADE 改名时又要手工维护")

    def test_has_on_delete_set_null(self) -> None:
        self.assertIn("ON DELETE SET NULL", self._ddl())

    def test_foreign_keys_pragma_on(self) -> None:
        rows = [r[0] for r in self.store.query("PRAGMA foreign_keys")]
        self.assertEqual(rows, [1], "SQLite 默认关闭外键, 必须显式打开")


class TestCascadeRename(unittest.TestCase):
    """改系列名, 会次必须自动跟随。"""

    def setUp(self) -> None:
        from dddb.db import Store

        self.tmp = temp_path("fk-cascade")
        self.db = self.tmp / "t.sqlite3"
        store = Store(self.db)
        store.save_series("M3", abbr="M3", full_name="M3")
        store.save_event("M3-2026秋", series="M3", vol=58,
                         vol_label="M3-58")
        store.save_event("M3-2026春", series="M3", vol=57,
                         vol_label="M3-57")
        store.close()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rename_cascades(self) -> None:
        conn = sqlite3.connect(str(self.db))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")   # 裸连接默认关闭
        try:
            conn.execute("UPDATE event_series SET name = ? WHERE name = ?",
                         ("Music Media-Mix Market", "M3"))
            conn.commit()
            rows = {r["name"]: r["series"] for r in conn.execute(
                "SELECT name, series FROM events")}
        finally:
            conn.close()
        self.assertEqual(rows["M3-2026秋"], "Music Media-Mix Market",
                         "会次没跟着改名 —— 这正是上次事故的成因")
        self.assertEqual(rows["M3-2026春"], "Music Media-Mix Market")

    def test_delete_sets_null_and_keeps_event(self) -> None:
        conn = sqlite3.connect(str(self.db))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")   # 裸连接默认关闭
        try:
            conn.execute("DELETE FROM event_series WHERE name = ?", ("M3",))
            conn.commit()
            rows = {r["name"]: r["series"] for r in conn.execute(
                "SELECT name, series FROM events")}
        finally:
            conn.close()
        self.assertEqual(len(rows), 2, "删系列不该连坐删会次")
        self.assertTrue(all(v is None for v in rows.values()),
                        "series 应置 NULL")


class TestNullMeansUnassigned(unittest.TestCase):
    """未归类的会次存 NULL, 不存空串(空串是非法引用)。"""

    def test_empty_series_stored_as_null(self) -> None:
        from dddb.db import Store

        tmp = temp_path("fk-null")
        db = tmp / "t.sqlite3"
        store = Store(db)
        try:
            store.save_event("孤儿会次", series="", vol=1)
            row = store.query_one(
                "SELECT series FROM events WHERE name = ?", ("孤儿会次",))
            self.assertIsNone(row["series"], "空 series 应存 NULL")
            # 对外(数据层)仍按空串呈现
            got = store.list_events()[0]
            self.assertEqual(got["series"], "")
        finally:
            store.close()
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


class TestMigrationOldDb(unittest.TestCase):
    """老库(没有外键)打开后自动重建表补上。"""

    def test_old_table_upgraded(self) -> None:
        from dddb.db import Store

        tmp = temp_path("fk-migrate")
        db = tmp / "t.sqlite3"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
        CREATE TABLE event_series (
            name TEXT PRIMARY KEY, abbr TEXT NOT NULL DEFAULT '',
            full_name TEXT NOT NULL DEFAULT '',
            logo_url TEXT NOT NULL DEFAULT '',
            logo_local TEXT NOT NULL DEFAULT '',
            poster_url TEXT NOT NULL DEFAULT '',
            poster_local TEXT NOT NULL DEFAULT '',
            wiki TEXT NOT NULL DEFAULT '',
            links_json TEXT NOT NULL DEFAULT '{}',
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, updated_at REAL NOT NULL
        );
        CREATE TABLE events (
            name TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT '',
            year INTEGER NOT NULL DEFAULT 0,
            season TEXT NOT NULL DEFAULT '',
            start_date TEXT NOT NULL DEFAULT '',
            end_date TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '[]',
            logo_url TEXT NOT NULL DEFAULT '',
            logo_local TEXT NOT NULL DEFAULT '',
            links_json TEXT NOT NULL DEFAULT '{}',
            series TEXT NOT NULL DEFAULT '',
            vol INTEGER NOT NULL DEFAULT 0,
            vol_label TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL
        );
        INSERT INTO event_series(name, created_at, updated_at)
        VALUES ('M3', 0, 0);
        INSERT INTO events(name, series, vol, updated_at)
        VALUES ('M3-2026秋', 'M3', 58, 0);
        INSERT INTO events(name, series, vol, updated_at)
        VALUES ('未归类', '', 0, 0);
        """)
        conn.commit()
        conn.close()

        store = Store(db)
        try:
            ddl = str(store.query_one(
                "SELECT sql FROM sqlite_master WHERE name='events'")["sql"])
            self.assertIn("REFERENCES event_series", ddl, "老库没补上外键")
            rows = {r["name"]: r["series"] for r in store.query(
                "SELECT name, series FROM events")}
            # 正常引用保留
            self.assertEqual(rows["M3-2026秋"], "M3")
            # 空串在迁移中转成 NULL
            self.assertIsNone(rows["未归类"])
            # 数据没丢
            self.assertEqual(len(rows), 2)
            bad = store.query("PRAGMA foreign_key_check")
            self.assertEqual([dict(x) for x in bad], [])
        finally:
            store.close()
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


class TestLiveDbHasFk(unittest.TestCase):
    def test_local_db(self) -> None:
        db = ROOT / "data" / "dddb.sqlite3"
        if not db.is_file():
            self.skipTest("本地库不存在")
        conn = sqlite3.connect("file:{}?mode=ro".format(db), uri=True)
        try:
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='events'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertIn("ON UPDATE CASCADE", str(ddl))


class TestNewButtonRow(unittest.TestCase):
    """「新建展会」与「新建展会系列」要在同一行(标题行按钮组)。"""

    def test_same_row(self) -> None:
        from dddb.public import templates as T

        ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                            user={"id": 1, "username": "e", "role": "editor"})
        html = T.events_index_page(ctx, events=[], kinds=[], series=[])
        # 两个按钮都在 title-actions 里(同一行), 且各自只出现一次
        import re

        m = re.search(r'<span class="title-actions">([\s\S]*?)</span>', html)
        self.assertIsNotNone(m, "没有 title-actions 按钮组")
        group = m.group(1)
        self.assertIn("新建展会", group)
        self.assertIn("新建展会系列", group)
        self.assertEqual(html.count('href="/new/event"'), 1,
                         "「新建展会」重复了")
        self.assertNotIn("new-btn-row", html, "旧的独立行应已删除")

    def test_css_present(self) -> None:
        css = (ROOT / "dddb/public/static/public.css").read_text(
            encoding="utf-8")
        block = css.split(".title-actions {")[1].split("}")[0]
        self.assertIn("inline-flex", block, "inline-flex 才能并排")


if __name__ == "__main__":
    unittest.main()
