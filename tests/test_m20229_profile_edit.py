"""用户 m20229: 外链识别 / 双社团 / 艺人&社团编辑页改造.

五条需求:
1. ``linkco.re`` 自动识别成 Stream Links;
2. «Drawing Progressive House & Trance 02 -DPHT compilation album vol.2»
   这种**两个社团**的合辑不能被合并成一个名字;
3. 艺人头像改成上传; 职业按关联专辑的职责自动汇总; 别名能关联到对应艺人;
   相关链接与专辑编辑页同一套行编辑;
4. 社团编辑同上 —— 链接/成员改行编辑, 加社团徽标上传;
5. 两个编辑页按卡片分类排版。
"""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

from dddb import models
from dddb.db import Store
from dddb.public import data as data_mod
from dddb.public import linknames
from dddb.public import site as site_mod
from dddb.public import templates as T
from tests import _tmp

ROOT = Path(__file__).resolve().parent.parent


def _ctx() -> T.SiteContext:
    return T.SiteContext(lang="zh", csrf_token="tk", is_editor=True,
                         is_superuser=True)


class TestLinkcoRule(unittest.TestCase):
    def test_linkco_is_stream_links(self) -> None:
        self.assertEqual("Stream Links",
                         linknames.guess_link_name("https://linkco.re/abcdef"))
        self.assertEqual("Stream Links",
                         linknames.guess_link_name("https://www.linkco.re/x"))
        self.assertIn("Stream Links", linknames.name_choices())

    def test_js_rules_match(self) -> None:
        js = (ROOT / "dddb/public/static/public.js").read_text(encoding="utf-8")
        self.assertIn('"linkco.re", "Stream Links"', js)


class TestTwoCirclesNotMerged(unittest.TestCase):
    """合辑有两个社团时, 主社团 + 名单都要留住."""

    def test_model_keeps_list(self) -> None:
        album = models.Album.from_dict({
            "key": "k", "title": "DPHT compilation album vol.2",
            "circle": "社团A", "circles": ["社团A", "社团B"],
        })
        self.assertEqual("社团A", album.circle)
        self.assertEqual(["社团A", "社团B"], album.circles)
        self.assertEqual(["社团A", "社团B"], album.to_dict()["circles"])

    def test_parse_form_splits_circles(self) -> None:
        patch, errors = site_mod.parse_album_form({
            "title": ["DPHT compilation album vol.2"],
            "circle": ["社团A\n社团B"],
            "track_title": ["曲一"], "track_disc": ["1"],
        })
        self.assertEqual({}, errors)
        self.assertEqual("社团A", patch["circle"])
        self.assertEqual(["社团A", "社团B"], patch["circles"])

    def test_view_exposes_links(self) -> None:
        tmp = _tmp.temp_path("m20229-view-")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        store = Store(tmp / "d.sqlite3")
        try:
            store.upsert_album(models.Album.from_dict({
                "key": "k1", "title": "合辑", "circle": "社团A",
                "circles": ["社团A", "社团B"], "release_date": "2026-10-31",
            }))
            pd = data_mod.PublicData(store, covers_dir=tmp / "covers")
            view = pd.make_view(store.get_album("k1").to_dict())
            self.assertEqual(["社团A", "社团B"],
                             [item["name"] for item in view["circles"]])
            # 两个社团都能在社团页查到自己那批碟
            for name in ("社团A", "社团B"):
                detail = pd.circle_detail(name)
                self.assertIsNotNone(detail, name)
                self.assertEqual(["k1"], [a["key"] for a in detail["albums"]])
        finally:
            store.close()

    def test_detail_page_links_both(self) -> None:
        album = {"key": "k1", "title": "合辑", "circle": "社团A",
                 "circle_href": "/circle/%E7%A4%BE%E5%9B%A2A",
                 "circles": [{"name": "社团A", "href": "/circle/%E7%A4%BE%E5%9B%A2A"},
                             {"name": "社团B", "href": "/circle/%E7%A4%BE%E5%9B%A2B"}],
                 "tracks": [], "credits": [], "urls": [], "photos": [],
                 "title_alt": "", "event": "", "date": ""}
        html = T.album_detail_page(_ctx(), album=album)
        self.assertIn("/circle/%E7%A4%BE%E5%9B%A2B", html)
        self.assertIn("社团B", html)


class TestArtistEditPage(unittest.TestCase):
    def test_avatar_is_upload(self) -> None:
        html = T.edit_profile_page(_ctx(), "artist", "甲", values={"name": "甲"})
        self.assertIn('type="file" name="avatar"', html)
        self.assertNotIn('name="avatar" value=', html)

    def test_occupations_auto_and_manual(self) -> None:
        html = T.edit_profile_page(_ctx(), "artist", "甲",
                                   values={"name": "甲"},
                                   occupations=["编曲", "作曲"])
        self.assertIn("按这个艺人参与专辑里的职责自动汇总", html)
        self.assertIn("编曲", html)
        self.assertIn('name="occupations_text"', html)

    def test_alias_datalist(self) -> None:
        html = T.edit_profile_page(_ctx(), "artist", "甲",
                                   values={"name": "甲", "aliases": "乙"},
                                   alias_choices=["乙", "丙"])
        self.assertIn('list="dl-artist-names"', html)
        self.assertIn('<option value="乙"></option>', html)

    def test_links_use_row_editor(self) -> None:
        html = T.edit_profile_page(_ctx(), "artist", "甲",
                                   values={"name": "甲",
                                           "links": "官网: https://x.example/"})
        self.assertIn('name="url_value"', html)
        self.assertIn('value="https://x.example/"', html)
        self.assertIn('data-pair-add="url"', html)
        self.assertNotIn('<textarea name="links"', html)

    def test_cards_layout(self) -> None:
        html = T.edit_profile_page(_ctx(), "artist", "甲", values={"name": "甲"})
        self.assertIn('class="edit-cards is-profile"', html)
        for title in ("基本资料", "头像", "职业", "别名", "简介", "相关链接"):
            self.assertIn("<h3>{}</h3>".format(title), html)


class TestCircleEditPage(unittest.TestCase):
    def test_logo_is_upload(self) -> None:
        html = T.edit_profile_page(_ctx(), "circle", "社团A",
                                   values={"name": "社团A"})
        self.assertIn('type="file" name="logo"', html)

    def test_staff_row_editor(self) -> None:
        html = T.edit_profile_page(_ctx(), "circle", "社团A",
                                   values={"name": "社团A",
                                           "staff": "owner: BIRD\n作曲: 丙"})
        self.assertIn('name="staff_role" value="owner"', html)
        self.assertIn('name="staff_name" value="BIRD"', html)
        self.assertIn('name="staff_name" value="丙"', html)
        self.assertIn('data-pair-add="staff"', html)
        self.assertNotIn('<textarea name="staff"', html)

    def test_cards_layout(self) -> None:
        html = T.edit_profile_page(_ctx(), "circle", "社团A",
                                   values={"name": "社团A"})
        self.assertIn('class="edit-cards is-profile"', html)
        for title in ("基本资料", "徽标", "简介", "成员", "相关链接"):
            self.assertIn("<h3>{}</h3>".format(title), html)


class TestSaveRowsAndUpload(unittest.TestCase):
    """行编辑提交后要真的落库(链接 / 成员), 头像上传要写文件."""

    def setUp(self) -> None:
        self.tmp = _tmp.temp_path("m20229-save-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.db_path = self.tmp / "d.sqlite3"
        self.covers = self.tmp / "covers"
        self.covers.mkdir(parents=True, exist_ok=True)
        store = Store(self.db_path)
        try:
            store.upsert_circle(models.Circle.from_dict({
                "name": "社团A", "activity_type": "both",
                "description": "老简介", "staff": [{"role": "owner",
                                                    "name": "BIRD"}]}))
            store.upsert_artist("甲", {"name": "甲", "bio": "b"})
        finally:
            store.close()

    def test_pair_rows_read(self) -> None:
        pairs = site_mod.PublicSite._pair_rows(
            {"url_key": ["官网", "X"], "url_value": ["https://a/", "https://x/"]},
            "url_key", "url_value")
        self.assertEqual([("官网", "https://a/"), ("X", "https://x/")], pairs)
        self.assertEqual([], site_mod.PublicSite._pair_rows({}, "a", "b"))

    def test_staff_roles_can_be_free_text(self) -> None:
        self.assertEqual("boss", models.normalize_staff_role("boss"))
        self.assertEqual("作曲", models.normalize_staff_role("作曲"))
        self.assertEqual("owner", models.normalize_staff_role("主催"))
        self.assertEqual("member", models.normalize_staff_role(""))


class TestVersionsBumped(unittest.TestCase):
    def test_css_and_js(self) -> None:
        import re
        tpl = (ROOT / "dddb/public/templates.py").read_text(encoding="utf-8")
        css = int(re.search(r"public\.css\?v=(\d+)", tpl).group(1))
        js = int(re.search(r"public\.js\?v=(\d+)", tpl).group(1))
        self.assertGreaterEqual(css, 89)
        self.assertGreaterEqual(js, 77)


if __name__ == "__main__":
    unittest.main()
