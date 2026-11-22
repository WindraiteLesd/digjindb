"""孤儿会次不该从索引里消失.

真实事故:
    系列改名 ``M3`` -> ``Music Media-Mix Market`` 时, 有一条会次
    (``M3-2026秋``, 1096 张专辑)的 ``events.series`` 没跟着改,
    留下指向旧 key 的孤儿。而 ``event_series()`` 按
    ``WHERE events.series = <系列key>`` 查会次 ——
    孤儿在全系列下都查不到, 于是整届专辑从索引页消失(数据仍在库里)。

修法: 数据层把孤儿会次按名字解析挂回匹配的系列。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests._tmp import temp_path

ROOT = Path(__file__).resolve().parents[1]


class TestOrphanEventGuard(unittest.TestCase):
    def setUp(self) -> None:
        from dddb.db import Store
        from dddb.models import Album
        from dddb.public.data import PublicData, ReadOnlyStore

        self.tmp = temp_path("orphan-event")
        self.db = self.tmp / "t.sqlite3"
        store = Store(self.db)
        store.save_series("Music Media-Mix Market", abbr="M3",
                          full_name="Music Media-Mix Market")
        # 正常挂载
        store.save_event("M3-2026春", series="Music Media-Mix Market",
                         vol=57, vol_label="M3-57")
        # 孤儿: 指向已不存在的旧 key。
        # 注意: 有了外键之后, 正常写入**造不出**孤儿了(这正是我们想要的)。
        # 所以先关掉 FK 造一条历史遗留的脏数据, 再打开 —— 模拟老库形态。
        store.save_event("M3-2026秋", series="Music Media-Mix Market",
                         vol=58, vol_label="M3-58")
        store.execute("PRAGMA foreign_keys = OFF")
        try:
            store.execute(
                "UPDATE events SET series = 'M3' WHERE name = 'M3-2026秋'")
        finally:
            store.execute("PRAGMA foreign_keys = ON")
        for ev, n, date in (
            ("M3-2026春", 2, "2026-04-26"),
            ("M3-2026秋", 5, "2026-10-25"),
        ):
            for i in range(n):
                store.upsert_album(Album.from_dict({
                    "key": "{}-{}".format(ev, i),
                    "title": "T{}".format(i),
                    "title_original": "T{}".format(i),
                    "event": ev, "release_date": date,
                    "source_ids": {}}))
        store.close()
        self.store = ReadOnlyStore(self.db)
        self.data = PublicData(self.store, root=self.tmp)

    def tearDown(self) -> None:
        import shutil

        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_orphan_still_listed(self) -> None:
        items = self.data.event_series()
        self.assertTrue(items, "应有系列")
        values = [e["value"] for e in items[0]["editions"]]
        self.assertIn("M3-2026秋", values,
                      "孤儿会次从索引里消失了(真实事故)")

    def test_orphan_count_included(self) -> None:
        items = self.data.event_series()
        self.assertEqual(items[0]["count"], 7, "孤儿会次的专辑数要算进去")

    def test_no_duplicate_editions(self) -> None:
        """正常挂载的不能被重复列一次。"""
        items = self.data.event_series()
        values = [e["value"] for e in items[0]["editions"]]
        self.assertEqual(len(values), len(set(values)), "会次重复了")
        self.assertEqual(len(values), 2)

    def test_orphan_has_display_name(self) -> None:
        items = self.data.event_series()
        by = {e["value"]: e for e in items[0]["editions"]}
        self.assertEqual(by["M3-2026秋"]["display"],
                         "[M3] M3-2026秋 (M3-58)")

    def test_orphan_sorted_by_vol(self) -> None:
        """孤儿插进来后仍按会次号倒序。"""
        items = self.data.event_series()
        vols = [e["vol"] for e in items[0]["editions"]]
        self.assertEqual(vols, sorted(vols, reverse=True))


class TestLiveDbNoOrphans(unittest.TestCase):
    """本地库不该有孤儿会次。"""

    def test_no_orphan(self) -> None:
        import sqlite3

        db = ROOT / "data" / "dddb.sqlite3"
        if not db.is_file():
            self.skipTest("本地库不存在")
        conn = sqlite3.connect("file:{}?mode=ro".format(db), uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT e.name, e.series FROM events e"
                " WHERE e.series != '' AND e.series NOT IN"
                " (SELECT name FROM event_series)").fetchall()
        except sqlite3.Error:
            self.skipTest("读库失败")
        finally:
            conn.close()
        self.assertEqual([dict(r) for r in rows], [], "有孤儿会次")


if __name__ == "__main__":
    unittest.main()
