"""用户 m19886: 艺人页专辑卡片 / 头像 / 社团简介不再被整条替换清空.

三条报告:
1. 艺人页的「参与专辑」没有封面、没有展会、没有社团;
2. 社团徽标、艺人头像不显示;
3. 改了一下活动类型, 社团简介就丢了 —— 因为编辑走的是 ``upsert_circle``
   (整条替换), 而表单里没有简介字段。
"""

from __future__ import annotations

import re
import shutil
import time
import unittest
from pathlib import Path

from dddb import models
from dddb.db import Store
from dddb.public import auth
from dddb.public import data as data_mod
from dddb.public import site as site_mod
from dddb.public import templates as T
from dddb.public.users import UserStore
from tests import _tmp

ROOT = Path(__file__).resolve().parent.parent

CIRCLE = "某社团"
ARTIST = "甲"
EVENT = "M3-2026春"
DESCRIPTION = "老简介不该丢"


def _ctx() -> T.SiteContext:
    return T.SiteContext(lang="zh", csrf_token="tk", is_editor=True,
                         is_superuser=True)


class _Case(unittest.TestCase):
    """临时库 + 一张带封面/展会/社团的专辑 + 一个社团 + 一位艺人."""

    def setUp(self) -> None:
        self.tmp = _tmp.temp_path("m19886-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.db_path = self.tmp / "dddb.sqlite3"
        self.covers = self.tmp / "covers"
        self.covers.mkdir(parents=True, exist_ok=True)
        self.users_path = self.tmp / "users.sqlite3"

        store = Store(self.db_path)
        try:
            store.upsert_album(models.Album.from_dict({
                "key": "k1",
                "title": "测试专辑",
                "circle": CIRCLE,
                "artists": ARTIST,
                "event": EVENT,
                "release_date": "2026-04-27",
                "cover_local": "k1.img",
                "tracks": [{"disc": "1", "index": "1",
                            "title": "曲一", "artists": ARTIST}],
            }))
            store.upsert_circle(models.Circle.from_dict({
                "name": CIRCLE,
                "activity_type": "both",
                "founded": "2002-12-30",
                "last_active": "2006-10-09",
                "description": DESCRIPTION,
                "urls": {"官网": "https://x.example/"},
                "staff": [{"role": "owner", "name": "BIRD"}],
            }))
            store.upsert_artist(ARTIST, {
                "name": ARTIST, "avatar": "https://x.example/a.png"})
        finally:
            store.close()
        (self.covers / "k1.img").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

        users = UserStore(self.users_path)
        ok, message = users.create_user("ed1", "pw-123456", role="editor")
        self.assertTrue(ok, message)
        self.uid = int((users.get("ed1") or {}).get("id") or 1)

        self.site = site_mod.build_site(
            None, root=self.tmp, db_path=self.db_path,
            users_db=self.users_path,
            sessions_db=self.tmp / "sessions.sqlite3",
            covers_dir=self.covers,
        )
        self.addCleanup(self.site.close)
        sess = auth.Session(sid="s1", user_id=self.uid, username="ed1",
                            role="editor", expires_at=time.time() + 3600)
        state = auth.AuthState(session=sess)
        self.site.sessions.state = lambda cookies: state
        self.site.same_origin = lambda *a, **k: (True, "")
        self.site.sessions.signer.check = lambda b, g: True

    def get(self, path: str) -> tuple[int, str]:
        resp = self.site.handle("GET", path, headers={}, body=b"")
        return resp.status, resp.body.decode("utf-8", "replace")

    def post(self, path: str, fields: dict[str, str]):
        body = "&".join(f"{k}={v}" for k, v in
                        [("csrf_token", "tk"), *fields.items()]).encode()
        return self.site.handle(
            "POST", path, body=body,
            headers={"content-type": "application/x-www-form-urlencoded",
                     "origin": "http://x", "host": "x", "referer": "http://x/"})


class TestArtistAlbumsCarryCardFields(_Case):
    """参与专辑列表要带封面 / 展会 / 社团, 否则卡片是空的."""

    def test_public_data_provides_view(self) -> None:
        store = Store(self.db_path)
        try:
            pd = data_mod.PublicData(store, covers_dir=self.covers)
            derived = pd.artists_all()
        finally:
            store.close()
        row = next(item for item in derived if item["name"] == ARTIST)
        self.assertEqual(1, len(row["albums"]))
        album = row["albums"][0]
        self.assertEqual("k1", album["key"])
        self.assertEqual("测试专辑", album["title"])
        self.assertTrue(album.get("cover"), "参与专辑要有封面地址")
        self.assertTrue(album.get("has_cover"))
        self.assertEqual(EVENT, album.get("event"))
        self.assertEqual(CIRCLE, album.get("circle"))
        self.assertEqual(1, album.get("track_count"))

    def test_artist_page_renders_cover_and_event(self) -> None:
        status, html = self.get("/artist/" + ARTIST)
        self.assertEqual(200, status)
        self.assertIn('class="album-card"', html)
        self.assertIn("/cover/k1", html)
        self.assertIn(EVENT, html)


class TestAvatars(_Case):
    def test_index_uses_avatar_image(self) -> None:
        status, html = self.get("/artists")
        self.assertEqual(200, status)
        self.assertIn('<img class="artist-avatar"', html)
        self.assertIn("https://x.example/a.png", html)

    def test_index_falls_back_to_letter(self) -> None:
        html = T.artists_index_page(_ctx(), artists=[
            {"name": "无头像", "album_count": 1}])
        self.assertIn('class="artist-avatar" aria-hidden="true"', html)
        self.assertIn(">无<", html)

    def test_detail_renders_avatar(self) -> None:
        status, html = self.get("/artist/" + ARTIST)
        self.assertEqual(200, status)
        self.assertIn("https://x.example/a.png", html)


class TestCircleLogoFallsBackToLocal(_Case):
    def test_logo_local_is_used(self) -> None:
        store = Store(self.db_path)
        try:
            store.update_circle_fields(CIRCLE, {"logo_url": "",
                                                "logo_local": "/logo/x.png"})
            pd = data_mod.PublicData(store, covers_dir=self.covers)
            detail = pd.circle_detail(CIRCLE)
        finally:
            store.close()
        self.assertEqual("/logo/x.png", detail["logo"])


class TestCirclePartialUpdate(_Case):
    """``update_circle_fields`` 只写 patch 里的键, 其余保持原值."""

    def test_only_activity_type_changes(self) -> None:
        store = Store(self.db_path)
        try:
            ok, message = store.update_circle_fields(
                CIRCLE, {"activity_type": "同人社团"})
            self.assertTrue(ok, message)
            circle = store.get_circle(CIRCLE)
        finally:
            store.close()
        self.assertIsNotNone(circle)
        self.assertEqual("同人社团", circle.activity_type)
        self.assertEqual(DESCRIPTION, circle.description)
        self.assertEqual("2006-10-09", circle.last_active)
        self.assertEqual([("owner", "BIRD")],
                         [(person.role, person.name) for person in circle.staff])


class TestEntityFormBackfillsStore(unittest.TestCase):
    """编辑页的成员框与「最后活动」必须来自库里的真值."""

    def test_source_reads_get_circle(self) -> None:
        import inspect
        src = inspect.getsource(
            site_mod.PublicSite._entity_form_values)
        circle_part = src.split('if kind == "circle":')[1].split(
            'if kind in ("event", "tag"):')[0]
        self.assertIn("get_circle", circle_part)
        self.assertNotIn('"staff": ""', circle_part)
        self.assertNotIn('detail.get("last_date")', circle_part)


class TestNoMoreWholeRecordReplace(unittest.TestCase):
    """两处曾用 ``upsert_circle`` 的地方必须改成局部更新."""

    def test_entity_update_uses_partial(self) -> None:
        import inspect
        src = inspect.getsource(site_mod.PublicSite._do_entity_update)
        self.assertIn("update_circle_fields", src)
        # 注释里提到旧写法是正常的, 只要**调用**没有了就行
        self.assertNotIn("store.upsert_circle(", src)

    def test_logo_upload_uses_partial(self) -> None:
        import inspect
        src = inspect.getsource(site_mod.PublicSite._save_new_image)
        self.assertIn("update_circle_fields", src)
        # 注释里提到旧写法是正常的, 只要**调用**没有了就行
        self.assertNotIn("store.upsert_circle(", src)


class TestActivityTypeIsFreeText(unittest.TestCase):
    def test_spec_has_description_field(self) -> None:
        fields = T.NEW_ENTITY_SPECS["circle"][1]
        keys = [item[0] for item in fields]
        self.assertIn("description", keys)
        self.assertIn("activity_type", keys)

    def test_new_page_datalist(self) -> None:
        html = T.new_entity_page(_ctx(), "circle")
        self.assertIn('list="dl-new-activity_type"', html)
        self.assertIn('<datalist id="dl-new-activity_type">', html)
        self.assertNotIn('<select name="activity_type"', html)


class TestEditingActivityKeepsDescription(_Case):
    """端到端: 只改活动类型, 简介与成员必须还在."""

    def test_description_survives_edit(self) -> None:
        resp = self.post("/circle/" + CIRCLE + "/edit", {
            "name": CIRCLE,
            "activity_type": "同人社团",
            "founded": "2002-12-30",
            "last_active": "2006-10-09",
            "description": DESCRIPTION,
            "urls": "官网: https://x.example/",
            "staff": "owner: BIRD",
        })
        self.assertIn(resp.status, (200, 303))
        status, html = self.get("/circle/" + CIRCLE)
        self.assertEqual(200, status)
        self.assertIn(DESCRIPTION, html)

    def test_edit_without_description_field_keeps_it(self) -> None:
        """老表单(没有简介框)提交也不能把简介冲掉."""
        resp = self.post("/circle/" + CIRCLE + "/edit", {
            "name": CIRCLE,
            "activity_type": "同人社团",
            "founded": "2002-12-30",
            "last_active": "2006-10-09",
        })
        self.assertIn(resp.status, (200, 303))
        store = Store(self.db_path)
        try:
            circle = store.get_circle(CIRCLE)
        finally:
            store.close()
        self.assertEqual(DESCRIPTION, circle.description)
        self.assertEqual("同人社团", circle.activity_type)
        self.assertEqual([("owner", "BIRD")],
                         [(person.role, person.name) for person in circle.staff])


if __name__ == "__main__":
    unittest.main()
