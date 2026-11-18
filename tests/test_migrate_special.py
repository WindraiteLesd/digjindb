"""`special` -> `circle_site` 数据迁移.

背景: 老解析器把「社团名后面第一个链接」写进了 ``urls["special"]``, 但那其实是
**社团的官方/链接聚合页**(实测主机名全是 lit.link / linktr.ee / YouTube 频道 /
Google Sites / 个人域名), 不是这张碟的特设页。真正的特设页只来自 CD 记录自己的
``link_url``(且只有带「新譜情報」的条目才有)。

库里现存的 ``special`` 全部是错语义, 所以要整体挪到 ``circle_site``。
"""

from __future__ import annotations

import unittest

from dddb.config import Config
from dddb.db import Store
from dddb.models import Album
from tests._tmp import temp_dir


def album(title: str, urls: dict[str, str]) -> Album:
    item = Album(title=title, title_original=title, circle="C1",
                 event="M3-2026春", source_ids={"test": title}, urls=dict(urls))
    item.key = item.compute_key()
    return item


class TestMigrateSpecial(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_dir("mig-")
        self.root = self._ctx.__enter__()
        self.cfg = Config.default(root=self.root)
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._ctx.__exit__(None, None, None)

    def _save(self, *items: Album) -> None:
        for item in items:
            self.store.upsert_album(item, event=item.event)

    def test_moves_special_to_circle_site(self) -> None:
        self._save(album("A", {"special": "https://lit.link/circle", "x": "https://x.com/a"}))
        self.assertEqual(self.store.migrate_special_to_circle_site(), 1)

        got = {a.display_title: a for a in self.store.all_albums()}["A"]
        self.assertEqual(got.urls.get("circle_site"), "https://lit.link/circle")
        self.assertNotIn("special", got.urls, "迁移后不应再有 special")
        self.assertEqual(got.urls.get("x"), "https://x.com/a", "其他链接不受影响")

    def test_keeps_existing_circle_site(self) -> None:
        """已经有 circle_site 的, 不能被旧的 special 覆盖."""
        self._save(album("B", {"special": "https://old", "circle_site": "https://new"}))
        self.store.migrate_special_to_circle_site()
        got = {a.display_title: a for a in self.store.all_albums()}["B"]
        self.assertEqual(got.urls.get("circle_site"), "https://new")
        self.assertNotIn("special", got.urls)

    def test_noop_on_clean_data(self) -> None:
        self._save(album("C", {"circle_site": "https://ok"}))
        self.assertEqual(self.store.migrate_special_to_circle_site(), 0)

    def test_idempotent(self) -> None:
        self._save(album("D", {"special": "https://lit.link/x"}))
        self.assertEqual(self.store.migrate_special_to_circle_site(), 1)
        self.assertEqual(self.store.migrate_special_to_circle_site(), 0, "第二次应无事可做")

    def test_empty_special_ignored(self) -> None:
        self._save(album("E", {"special": ""}))
        self.assertEqual(self.store.migrate_special_to_circle_site(), 0)


if __name__ == "__main__":
    unittest.main()
