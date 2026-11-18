"""专辑库搜索: 多字段 + 归一化.

原来的 SQL 只查 ``title / title_original / circle``, 但输入框写着
「搜索标题 / 社团 / 曲目」—— **曲目和艺人根本没查**, 而且不做全半角/标点归一,
所以「Something blue*」只能靠库里恰好也带 ``*`` 才撞上。

现在覆盖: 标题 / 原名 / 社团 / 编号 / 归一化标题 / 归一化社团 / 艺人 / 曲目。
"""

from __future__ import annotations

import unittest

from dddb.config import Config
from dddb.db import Store
from dddb.models import Album, Track
from tests._tmp import temp_dir


def make_album(title: str, *, circle: str = "", artists=None,
               tracks=None, catalog: str = "") -> Album:
    item = Album(
        title=title, title_original=title, circle=circle,
        artists=list(artists or []), catalog_no=catalog,
        event="M3-2026春", source_ids={"test": title},
        tracks=[Track(title=t, index=i) for i, t in enumerate(tracks or [], 1)],
    )
    item.key = item.compute_key()
    return item


class TestAlbumSearch(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_dir("search-")
        self.root = self._ctx.__enter__()
        self.cfg = Config.default(root=self.root)
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.db_path)
        self.album = make_album(
            "だってもっと眠っていたい!", circle="Something blue*",
            artists=["高城みよ"], tracks=["夜のワルツ", "眠りの森"], catalog="SB-001")
        self.store.upsert_album(self.album, event=self.album.event)

    def tearDown(self) -> None:
        self.store.close()
        self._ctx.__exit__(None, None, None)

    def _search(self, q: str) -> list[Album]:
        return self.store.list_albums(query=q, limit=50)

    def test_title(self) -> None:
        self.assertEqual(len(self._search("だってもっと眠っていたい")), 1)

    def test_circle_with_asterisk(self) -> None:
        """**用户报的 case**: 社团名里带 * 也要搜得到."""
        self.assertEqual(len(self._search("Something blue*")), 1)

    def test_circle_without_punctuation(self) -> None:
        """去掉标点也能命中(归一化列)."""
        self.assertEqual(len(self._search("something blue")), 1)

    def test_fullwidth_query(self) -> None:
        self.assertEqual(len(self._search("ＳＯＭＥＴＨＩＮＧ　ＢＬＵＥ")), 1)

    def test_artist(self) -> None:
        self.assertEqual(len(self._search("高城みよ")), 1)

    def test_track_title(self) -> None:
        """输入框承诺能搜曲目 —— 现在真的能了."""
        self.assertEqual(len(self._search("眠りの森")), 1)

    def test_catalog_number(self) -> None:
        self.assertEqual(len(self._search("SB-001")), 1)

    def test_no_match(self) -> None:
        self.assertEqual(self._search("完全不存在的关键词"), [])

    def test_count_matches_list(self) -> None:
        """总数与列表必须用同一套条件(否则分页页数会错)."""
        q = "Something"
        self.assertEqual(self.store.count_albums(query=q), len(self._search(q)))


class TestDefaultOrder(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_dir("order-")
        self.root = self._ctx.__enter__()
        self.cfg = Config.default(root=self.root)
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self._ctx.__exit__(None, None, None)

    def test_default_is_recently_updated(self) -> None:
        import time

        first = make_album("AAA", circle="z-last")
        self.store.upsert_album(first, event=first.event)
        time.sleep(0.01)
        second = make_album("ZZZ", circle="a-first")
        self.store.upsert_album(second, event=second.event)

        rows = self.store.list_albums(limit=10)          # 不传 order
        self.assertEqual(rows[0].display_title, "ZZZ",
                         "默认应按更新时间倒序(最后更新的在前)")


if __name__ == "__main__":
    unittest.main()
