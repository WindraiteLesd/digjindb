"""社团页专辑卡片: 必须带封面与曲目数.

历史 bug: ``circle_detail`` 手工拼 album 字典, 只有 key/title/href/date/event,
导致卡片一律显示占位封面 + "无曲目"(即使库里有封面和曲目)。
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _make_db(path: Path) -> None:
    """造一个小库: 同一社团下 3 张, 封面/曲目覆盖三种情况。"""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE albums (key TEXT PRIMARY KEY, title TEXT, title_original TEXT,"
        " circle TEXT, circle_orig TEXT, event TEXT, release_date TEXT,"
        " track_count INTEGER, cover_local TEXT, cover_url TEXT, payload TEXT,"
        " updated_at REAL, year TEXT, event_kind TEXT,"
        " norm_title TEXT, norm_circle TEXT)")
    conn.execute("CREATE TABLE album_tags (album_key TEXT, tag TEXT)")
    rows = [
        # 有本地封面 + 有曲目
        ("c-a", "有声封面", 7, "/tmp/cover-a.img", "",
         {"artists": ["A"], "credits": [], "urls": {}, "tags": [], "genres": []}),
        # 只有远程封面 + 有曲目
        ("c-b", "远程封面", 11, "", "https://example.com/b.jpg",
         {"artists": ["B"], "credits": [], "urls": {}, "tags": [], "genres": []}),
        # 无封面 + 有曲目(关键: 曲目数要显示出来)
        ("c-c", "没封面", 5, "", "",
         {"artists": ["C"], "credits": [], "urls": {}, "tags": [], "genres": []}),
    ]
    for key, title, tracks, local, url, extra in rows:
        # make_view 读的是 **payload** 里的 cover_local/cover_url,
        # 所以测试数据必须写进 payload(不是只写数据库列)
        payload = {"key": key, "title": title, "title_original": title,
                   "circle": "测试社团", "tracks": [{"title": "t"}] * tracks,
                   "release_date": "2026-01-01",
                   "cover_local": local, "cover_url": url, **extra}
        conn.execute(
            "INSERT INTO albums VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (key, title, title, "测试社团", "", "", "2026-01-01", tracks,
             local, url, json.dumps(payload, ensure_ascii=False), 0.0,
             "2026", "", title.lower(), "测试社团"))
    conn.commit()
    conn.close()


class TestCircleAlbumCards(unittest.TestCase):
    def setUp(self) -> None:
        from tests._tmp import temp_path

        # 用项目自带的 temp_path: 它清理时容忍只读句柄(WinError 5)
        self.tmp = temp_path("circle-cards")
        self.db = self.tmp / "t.sqlite3"
        _make_db(self.db)

        from dddb.public.data import PublicData, ReadOnlyStore

        self.store = ReadOnlyStore(self.db)
        self.data = PublicData(self.store, root=self.tmp,
                               covers_dir=self.tmp / "covers")

    def tearDown(self) -> None:
        import shutil

        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _albums(self):
        detail = self.data.circle_detail("测试社团")
        self.assertIsNotNone(detail)
        return detail["albums"]

    def test_cards_have_cover_fields(self) -> None:
        """卡片必须有 has_cover / cover 字段(否则一律占位块)。"""
        albums = self._albums()
        self.assertEqual(len(albums), 3)
        for a in albums:
            self.assertIn("has_cover", a, "缺 has_cover -> 封面显示不出来")
            self.assertIn("cover", a, "缺 cover -> 卡片没有图片地址")

    def test_cover_detected(self) -> None:
        """有封面的两张要识别出来, 没封面的那张保持 False。"""
        albums = {a["key"]: a for a in self._albums()}
        self.assertTrue(albums["c-a"]["has_cover"], "本地封面应识别为有封面")
        self.assertTrue(albums["c-b"]["has_cover"], "远程封面应识别为有封面")
        self.assertFalse(albums["c-c"]["has_cover"], "无封面应为 False")
        self.assertIn("/cover/", albums["c-a"]["cover"])
        self.assertIn("/cover/", albums["c-b"]["cover"])

    def test_track_count_present(self) -> None:
        """曲目数必须来自真实数据, 不能是 0。"""
        albums = {a["key"]: a for a in self._albums()}
        self.assertEqual(albums["c-a"]["track_count"], 7)
        self.assertEqual(albums["c-b"]["track_count"], 11)
        self.assertEqual(albums["c-c"]["track_count"], 5)

    def test_page_shows_covers_and_tracks(self) -> None:
        """渲染出来的页面: 有封面的是 img, 曲目数不再显示"无曲目"。"""
        from dddb.public import templates as T

        detail = self.data.circle_detail("测试社团")
        ctx = T.SiteContext(csrf_token="t", lang="zh")
        html = T.circle_detail_page(ctx, circle=detail)

        # 两张有封面的 -> 至少 2 个 <img>
        self.assertGreaterEqual(html.count("<img"), 2)
        # 曲目数出现在页面上
        self.assertIn("7", html)
        self.assertIn("11", html)
        # 有曲目的专辑不该被标成"无曲目"
        self.assertNotIn("无曲目", html)

    def test_still_has_date_and_event(self) -> None:
        """社团页资料区还要用 date/event, make_view 之后要补回去。"""
        for a in self._albums():
            self.assertIn("date", a)
            self.assertIn("event", a)


if __name__ == "__main__":
    unittest.main()
