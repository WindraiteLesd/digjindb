"""人工编辑保护 + 跨源去重多键匹配.

真实问题: 一张专辑被**手工改过信息**之后, 下一次从别的源采集会把它当成新碟
再插一条 —— 因为去重靠的是"规范化标题 + 社团相等", 而标题被改过就配不上了。
"""

from __future__ import annotations

import unittest

from dddb.config import Config
from dddb.db import Store
from dddb.models import Album
from tests._tmp import temp_dir


def album(title: str, *, circle: str = "Circle", catalog: str = "",
          source: str = "dojinmusic", sid: str = "1", trust: int = 100) -> Album:
    item = Album(title=title, title_original=title, circle=circle,
                 catalog_no=catalog, event="M3-2026春",
                 source_ids={source: sid}, trust=trust)
    item.key = item.compute_key()
    return item


class TestEditedProtection(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_dir("edited-")
        self.root = self._ctx.__enter__()
        self.cfg = Config.default(root=self.root)
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._ctx.__exit__(None, None, None)

    def test_manual_edit_marks_flag(self) -> None:
        item = album("Original")
        self.store.upsert_album(item, event=item.event)
        self.assertFalse(self.store.get_album(item.key).edited)
        ok, err = self.store.update_album_fields(item.key, {"title": "改过的标题"})
        self.assertTrue(ok, err)
        self.assertTrue(self.store.get_album(item.key).edited, "人工编辑后应置位 edited")

    def test_edited_fields_not_overwritten_by_higher_trust(self) -> None:
        """核心回归: 用户改过的标题, 不能被**可信度更高**的来源改回去."""
        item = album("Original")
        self.store.upsert_album(item, event=item.event)
        self.store.update_album_fields(item.key, {"title": "手工改的"})

        # 更高可信度的来源带来不同标题
        again = album("Original", source="dojinmusic", sid="1", trust=100)
        again.title = "来源说的标题"
        again.title_original = "来源说的标题"
        self.store.upsert_album(again, event=again.event)

        self.assertEqual(self.store.get_album(item.key).title, "手工改的")

    def test_untouched_album_still_merges(self) -> None:
        """没编辑过的专辑, 该按可信度正常合并 —— 别把保护做成"一律不更新"."""
        item = album("A", trust=30)
        self.store.upsert_album(item, event=item.event)
        better = album("A", source="dojinmusic", sid="1", trust=100)
        better.description = "补上的简介"
        self.store.upsert_album(better, event=better.event)
        self.assertEqual(self.store.get_album(item.key).description, "补上的简介")


class TestDedupeByCatalog(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_dir("cat-")
        self.root = self._ctx.__enter__()
        self.cfg = Config.default(root=self.root)
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._ctx.__exit__(None, None, None)

    def test_same_catalog_different_title_merges(self) -> None:
        """标题被改过、但编号一致 -> 认回同一张, 不再插重复行."""
        first = album("Original Title", catalog="ABC-001")
        self.store.upsert_album(first, event=first.event)
        self.store.update_album_fields(first.key, {"title": "完全不同的标题",
                                                   "title_original": "完全不同的标题"})
        before = len(self.store.all_albums())

        second = album("Original Title", circle="Other Circle",
                       catalog="ABC-001", source="bangumi", sid="99", trust=80)
        self.store.upsert_album(second, event=second.event)

        self.assertEqual(len(self.store.all_albums()), before, "编号相同不该产生新行")

    def test_same_source_id_merges(self) -> None:
        """同一个站点 id -> 同一张碟(原生 ID 命中优先级最高)."""
        first = album("T1", source="bangumi", sid="777")
        self.store.upsert_album(first, event=first.event)
        self.store.update_album_fields(first.key, {"title": "改过", "title_original": "改过"})
        before = len(self.store.all_albums())
        second = album("T1", source="bangumi", sid="777")
        self.store.upsert_album(second, event=second.event)
        self.assertEqual(len(self.store.all_albums()), before)

    def test_different_catalog_same_title_still_merges(self) -> None:
        """标题+社团都相同 -> 原本就按重复处理(编号不同也不翻案).

        这是**既有**行为, 这里用测试把它钉住: 编号判据只负责"编号相同必定是同一张",
        不反过来把"编号不同"当成"一定不是同一张"(同社团同名碟极少, 且这种误合并
        比漏合并容易发现)。
        """
        a = album("Same", catalog="A-1", source="dojinmusic", sid="1")
        b = album("Same", catalog="B-2", source="dojinmusic", sid="2")
        self.store.upsert_album(a, event=a.event)
        self.store.upsert_album(b, event=b.event)
        self.assertEqual(len(self.store.all_albums()), 1)


if __name__ == "__main__":
    unittest.main()
