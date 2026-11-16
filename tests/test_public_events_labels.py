"""用户端回归守卫: 展会名带序号后缀 + 代码/数据里不出现来源名.

① 展会筛选标签
   曾经用 `event_kind_label(event_kind)` 当标签 → M3-2026春 / M3-58 / M3-2026秋
   全部显示成 "M3", 序号后缀全丢。现在必须显示展会**名**。

② 来源名
   用户端要求"数据库与代码里都不出现来源名"; 带来源站名的外链顺序表
   已移到站长端 `dnh/webui/linkorder.py`。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from dnh.config import Config
from dnh.db import Store
from dnh.public.data import PublicData, ReadOnlyStore
from tests._tmp import temp_dir

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIR = ROOT / "dnh" / "public"

#: 用户端不该出现的来源站名
SOURCE_NAMES = ("dojinmusic", "dojin-music", "bangumi", "bgm.tv", "vgmdb",
                "doujinstyle", "dizzylab", "discogs", "touhoudb", "thwiki")


class TestEventLabels(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = temp_dir("pl-events-")
        self.root = self._ctx.__enter__()
        self.cfg = Config.default(root=self.root)
        self.cfg.ensure_dirs()
        self.store = Store(self.cfg.db_path)
        for title, event in (("A", "M3-2026春"), ("B", "M3-2026秋"),
                             ("C", "M3-2026春"), ("D", "C108")):
            from dnh.models import Album

            album = Album(title=title, title_original=title, circle="C",
                          event=event, source_ids={"t": title})
            album.key = album.compute_key()
            self.store.upsert_album(album, event=event)
        self.store.close()

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def _data(self):
        # 签名是 PublicData(store, *, root=...)
        return PublicData(ReadOnlyStore(self.cfg.db_path), root=self.root)

    def test_event_label_is_event_name(self) -> None:
        """**核心回归**: 标签必须是展会名, 不能是类型标签."""
        rows = self._data().events()
        labels = [r["label"] for r in rows]
        self.assertIn("M3-2026春", labels)
        self.assertIn("M3-2026秋", labels)
        self.assertIn("C108", labels)
        self.assertNotIn("M3", labels, "裸 'M3' 就是丢序号后缀的老 bug")

    def test_event_values_and_kind_still_present(self) -> None:
        rows = {r["value"]: r for r in self._data().events()}
        self.assertEqual(rows["M3-2026春"]["value"], "M3-2026春")
        self.assertEqual(rows["M3-2026春"]["kind"], "m3")
        self.assertEqual(rows["M3-2026春"]["count"], 2)
        # 类型标签仍单独保留(展会类型筛选用)
        self.assertIn("kind_label", rows["M3-2026春"])

    def test_kind_filter_keeps_kind_labels(self) -> None:
        rows = self._data().event_kinds()
        self.assertTrue(all(r["value"] and r["label"] for r in rows))


class TestNoSourceNamesInPublic(unittest.TestCase):
    def test_no_url_priority_table_with_source_keys(self) -> None:
        """用户端不再持有带来源站名的外链顺序表."""
        text = (PUBLIC_DIR / "data.py").read_text(encoding="utf-8")
        self.assertNotIn("_URL_PRIORITY", text)

    def test_source_names_absent_from_public_code(self) -> None:
        """`dnh/public` 全目录不得出现来源站名."""
        hits: list[str] = []
        for path in PUBLIC_DIR.rglob("*"):
            if path.suffix not in (".py", ".css", ".html", ".js"):
                continue
            body = path.read_text(encoding="utf-8", errors="replace").lower()
            for name in SOURCE_NAMES:
                if name.lower() in body:
                    hits.append(f"{path.name}: {name}")
        self.assertEqual(hits, [], f"用户端出现来源名: {hits}")

    def test_url_priority_lives_on_admin_side(self) -> None:
        """顺序表在站长端; 本仓库(线上云端部分)不含站长端, 跳过."""
        try:
            from dnh.webui.linkorder import URL_PRIORITY
        except ImportError:
            self.skipTest("站长端不在本仓库内")

        self.assertIn("special", URL_PRIORITY)
        self.assertIn("vgmdb", URL_PRIORITY)

    def test_url_sort_key_is_neutral(self) -> None:
        """排序函数是恒等的: 外链顺序就是用户填写的顺序.

        早先它会把 special/official/site 提到最前 —— 但编辑页按库里的顺序
        展示, 两边不一致会让用户觉得"改完顺序被搞乱了"(261 张专辑受影响)。
        现在恒定返回 0, 保持原序; 同时保证 /link 序号与界面顺序一致。
        """
        from dnh.public.data import _url_sort_key

        self.assertEqual(_url_sort_key("special"), _url_sort_key("anything_else"))
        self.assertEqual(_url_sort_key("vgmdb"), _url_sort_key("whatever"))
        self.assertEqual(_url_sort_key("x"), 0)

    def test_outbound_order_preserved(self) -> None:
        """_outbound_named_rows 必须保持传入顺序."""
        from dnh.public.data import _outbound_named_rows, _outbound_rows

        urls = {
            "circle_site": "https://a.com/",
            "dojinmusic": "https://d.com/",
            "special": "https://sp.com/",
            "x": "https://x.com/",
        }
        self.assertEqual([n for n, _u in _outbound_named_rows(urls)],
                         list(urls.keys()))
        self.assertEqual(_outbound_rows(urls), list(urls.values()))


if __name__ == "__main__":
    unittest.main()