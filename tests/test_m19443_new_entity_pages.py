"""新建条目（社团 / 艺人 / 展会 / 标签）零专辑时也要能看能搜。

用户 m19443: 刚建好的社团 303 跳到详情页, 页面却是「404 没有这个社团」——
``circle_detail`` 只查 albums 表, 一张碟都没有就直接 None。
"""

from __future__ import annotations

import shutil
import unittest
from pathlib import Path

from tests import _tmp
from dddb import models
from dddb.db import Store
from dddb.public.data import PublicData
import dddb.public.site as S

ROOT = Path(__file__).resolve().parent.parent


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = _tmp.temp_path("m19443")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / "dddb.sqlite3"
        self.store = Store(self.db)
        self.addCleanup(self.store.close)
        self.data = PublicData(self.store, covers_dir=self.tmp / "covers")

    def site(self):
        site = S.build_site(
            None, root=self.tmp, db_path=self.db,
            users_db=self.tmp / "users.sqlite3",
            sessions_db=self.tmp / "sessions.sqlite3",
            covers_dir=self.tmp / "covers",
        )
        self.addCleanup(site.close)
        return site


class TestCircleWithoutAlbums(_Base):
    def test_detail_resolves_from_circles_table(self) -> None:
        self.store.upsert_circle(models.Circle.from_dict({
            "name": "Circle B.z.B.", "activity_type": "同人社团",
            "description": "刚建的"}))
        detail = self.data.circle_detail("Circle B.z.B.")
        self.assertIsNotNone(detail, "零专辑的社团也该有详情页")
        self.assertEqual("Circle B.z.B.", detail["name"])
        self.assertEqual([], detail["albums"])
        self.assertEqual("同人社团", detail["activity_type"])
        self.assertEqual("刚建的", detail["description"])

    def test_unknown_circle_still_404(self) -> None:
        self.assertIsNone(self.data.circle_detail("No Such Circle"))

    def test_page_is_200_not_404(self) -> None:
        self.store.upsert_circle(models.Circle.from_dict({"name": "NewCircle"}))
        resp = self.site().handle("GET", "/circle/NewCircle", headers={}, body=b"")
        self.assertEqual(200, resp.status)
        text = resp.body.decode("utf-8", "replace")
        self.assertIn("NewCircle", text)
        self.assertNotIn("没有这个社团", text)

    def test_index_lists_circle_without_albums(self) -> None:
        self.store.upsert_circle(models.Circle.from_dict({"name": "NewCircle"}))
        values = [c["value"] for c in self.data.circles_all()]
        self.assertIn("NewCircle", values)
        count = next(c["count"] for c in self.data.circles_all()
                     if c["value"] == "NewCircle")
        self.assertEqual(0, count)


class TestArtistWithoutAlbums(_Base):
    def test_index_lists_artist_without_albums(self) -> None:
        self.store.upsert_artist("Solo Guy", {"name": "Solo Guy"})
        names = [a["name"] for a in self.data.artists_all()]
        self.assertIn("Solo Guy", names)
        item = next(a for a in self.data.artists_all() if a["name"] == "Solo Guy")
        self.assertEqual(0, item["album_count"])

    def test_page_is_200(self) -> None:
        self.store.upsert_artist("Solo Guy", {"name": "Solo Guy"})
        resp = self.site().handle("GET", "/artist/Solo%20Guy", headers={}, body=b"")
        self.assertEqual(200, resp.status)
        self.assertIn("Solo Guy", resp.body.decode("utf-8", "replace"))


class TestEventWithoutAlbums(_Base):
    def test_events_lists_edition_without_albums(self) -> None:
        self.store.save_event("M3-2027春", kind="M3", year=2027)
        values = [e["value"] for e in self.data.events()]
        self.assertIn("M3-2027春", values)
        entry = next(e for e in self.data.events() if e["value"] == "M3-2027春")
        self.assertEqual(0, entry["count"])
        self.assertIn("logo", entry)
        self.assertIn("links", entry)

    def test_page_is_200(self) -> None:
        self.store.save_event("M3-2027春", kind="M3", year=2027)
        resp = self.site().handle(
            "GET", "/event/M3-2027%E6%98%A5", headers={}, body=b"")
        self.assertEqual(200, resp.status)


class TestTagWithoutAlbums(_Base):
    def test_tags_lists_new_tag(self) -> None:
        ok, _msg = self.store.save_tag("新标签", kind="genre")
        self.assertTrue(ok)
        values = [t["value"] for t in self.data.tags()]
        self.assertIn("新标签", values)
        entry = next(t for t in self.data.tags() if t["value"] == "新标签")
        self.assertEqual(0, entry["count"])

    def test_page_is_200(self) -> None:
        self.store.save_tag("新标签")
        resp = self.site().handle(
            "GET", "/tag/%E6%96%B0%E6%A0%87%E7%AD%BE", headers={}, body=b"")
        self.assertEqual(200, resp.status)


class TestMergeKeepsExistingRows(_Base):
    def test_album_backed_circle_not_duplicated(self) -> None:
        self.store.upsert_circle(models.Circle.from_dict({"name": "Dup"}))
        self.store.upsert_album(models.Album.from_dict({
            "key": "k|dup|2026", "title": "T", "circle": "Dup",
            "release_date": "2026-01-01"}))
        values = [c["value"] for c in self.data.circles_all()]
        self.assertEqual(1, values.count("Dup"), "有专辑的社团不能被并出两条")
        entry = next(c for c in self.data.circles_all() if c["value"] == "Dup")
        self.assertEqual(1, entry["count"])

    def test_album_backed_event_not_duplicated(self) -> None:
        self.store.save_event("C999", kind="CM")
        self.store.upsert_album(models.Album.from_dict({
            "key": "k|c999|2026", "title": "T", "event": "C999",
            "release_date": "2026-01-01"}))
        values = [e["value"] for e in self.data.events()]
        self.assertEqual(1, values.count("C999"))


if __name__ == "__main__":
    unittest.main()
