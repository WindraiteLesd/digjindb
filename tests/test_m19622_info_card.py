"""用户 m19622 的四条: 出品信息默认侧 / 8MB 文案 / 制作人多值 / 发布平台间距."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import dddb.public.site as S
import dddb.public.templates as T

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "dddb/public/static/public.css"


def _ctx() -> T.SiteContext:
    return T.SiteContext(lang="zh", csrf_token="tk", is_editor=True)


def _options() -> dict:
    return {"circle": [], "artist": [{"id": 7, "name": "Winter"}], "event": [], "tag": []}


def _new_page(**values) -> str:
    return T.edit_album_page(
        _ctx(), album={"key": "", "title": ""},
        values=values, creating=True, bind_options=_options())


def _edit_page(**values) -> str:
    album = {"key": "k", "title": "T"}
    album.update(values)
    return T.edit_album_page(
        _ctx(), album=album,
        values={"key": "k", "title": "T", "credits": [], "tracks": [],
                "urls": [], "discs": {}, **values},
        bind_options=_options())


class TestDefaultReleaseKind(unittest.TestCase):
    """新建页默认落在同人展侧(用户要求)。"""

    def test_new_page_defaults_to_event(self) -> None:
        html = _new_page()
        self.assertIn('value="event" checked', html)
        self.assertNotIn('value="indie" checked', html)

    def test_platform_without_event_means_indie(self) -> None:
        # 填了发布平台又没有展会 -> 才认为是独立同人侧
        html = _new_page(platform="BOOTH")
        self.assertIn('value="indie" checked', html)
        self.assertNotIn('value="event" checked', html)

    def test_saved_kind_is_kept(self) -> None:
        self.assertIn('value="indie" checked', _edit_page(release_kind="indie"))
        self.assertIn('value="event" checked', _edit_page(release_kind="event"))


class TestUploadHintSays8MB(unittest.TestCase):
    """阈值早就是 8MB 了, 界面文案还写着 2MB。"""

    def test_no_two_megabyte_hint_left(self) -> None:
        src = (ROOT / "dddb/public/templates.py").read_text(encoding="utf-8")
        self.assertNotIn("≤2MB", src)
        self.assertNotIn("每张最多 2MB", src)

    def test_new_page_hints(self) -> None:
        html = _new_page()
        self.assertIn("≤8MB", html)
        self.assertIn("每张最多 8MB", html)

    def test_matches_server_limit(self) -> None:
        # 文案说的数字必须和常量一致, 否则下次调阈值还会漏改
        self.assertGreaterEqual(S.MAX_IMAGE_BODY, 8 * 1024 * 1024)


class TestProducerIsMultiValue(unittest.TestCase):
    """制作人以前是单行文本框, 现在与社团同款(可加多个)。"""

    def test_form_uses_bind_field(self) -> None:
        html = _new_page()
        self.assertIn('name="producer"', html)
        # 绑定组件的隐藏域 + chip 容器
        self.assertRegex(html, r'<input type="hidden" name="producer" value="[^"]*"')
        self.assertIn('data-name="producer"', html)

    def test_does_not_reintroduce_artist_bind(self) -> None:
        # 候选复用艺人, 但 data-bind 必须是 producer ——
        # 「艺人不再是绑定栏位」是既有约定(test_edit_enhancements), 别顶掉它
        html = _new_page()
        self.assertIn('data-bind="producer"', html)
        self.assertNotIn('data-bind="artist"', html)

    def test_parse_merges_multiple(self) -> None:
        patch, errs = S.parse_album_form({
            "title_original": ["T"], "title": ["T"],
            "producer": ["甲\n乙\n甲"], "track_title": [], "track_index": [],
        })
        self.assertEqual(patch.get("producer"), "甲、乙")
        self.assertEqual(errs, {})

    def test_roundtrip_display(self) -> None:
        values = S.form_display_values({"producer": ["甲\n乙"]})
        self.assertEqual(values["producer"], "甲、乙")
        self.assertIn('value="甲、乙"', _edit_page(producer="甲、乙"))


class TestReleaseKindIsPersisted(unittest.TestCase):
    """侧别单选以前只在回填时读, 保存根本不写 —— 选了等于没选。"""

    def test_patch_has_release_kind(self) -> None:
        patch, _ = S.parse_album_form({
            "title_original": ["T"], "title": ["T"],
            "release_kind": ["indie"], "track_title": [], "track_index": [],
        })
        self.assertEqual(patch.get("release_kind"), "indie")

    def test_patch_lowercases(self) -> None:
        patch, _ = S.parse_album_form({
            "title_original": ["T"], "title": ["T"],
            "release_kind": ["INDIE"], "track_title": [], "track_index": [],
        })
        self.assertEqual(patch.get("release_kind"), "indie")


class TestNewAlbumKeepsInfoFields(unittest.TestCase):
    """新建时 patch 里有、payload 里被漏抄 —— 填了保存后就丢。"""

    def test_payload_copies_fields(self) -> None:
        import inspect

        src = inspect.getsource(S.PublicSite._do_new_entity)
        for name in ("original_work", "producer", "platform", "release_kind", "discs"):
            self.assertIn('"%s": ' % name, src, "新建 payload 少了 %s" % name)


class TestDetailShowsInfo(unittest.TestCase):
    """填了看不到, 也会让人以为丢了。"""

    def _rows(self, **extra):
        album = {"key": "k", "title": "T", "href": "/album/k",
                 "date": "2026-01-01", "tracks": [], "credits": [],
                 "urls": [], "photos": []}
        album.update(extra)
        html = T.album_detail_page(_ctx(), album=album)
        return re.findall(
            r'<div class="info-row [^"]*"><dt>([^<]*)</dt><dd>([^<]*)</dd></div>', html)

    def test_rows_present(self) -> None:
        rows = self._rows(original_work="某作品", producer="甲、乙", platform="BOOTH")
        labels = [label for label, _ in rows]
        self.assertIn("原作", labels)
        self.assertIn("制作人", labels)
        self.assertIn("发布平台", labels)

    def test_empty_rows_hidden(self) -> None:
        labels = [label for label, _ in self._rows()]
        self.assertNotIn("原作", labels)
        self.assertNotIn("制作人", labels)
        self.assertNotIn("发布平台", labels)


class TestInfoGroupSpacing(unittest.TestCase):
    """.info-group 以前只是 grid item, 里面字段没有 12px 间距。"""

    def test_group_is_grid(self) -> None:
        css = CSS.read_text(encoding="utf-8")
        rule = re.search(r"\.info-group \{([^}]*)\}", css)
        self.assertIsNotNone(rule, ".info-group 缺少间距规则")
        body = rule.group(1)
        self.assertIn("display: grid", body)
        self.assertIn("gap: 12px", body)

    def test_hidden_still_wins(self) -> None:
        css = CSS.read_text(encoding="utf-8")
        # [hidden] 那条必须排在后面, 否则 display:grid 会把隐藏顶掉
        self.assertLess(css.index(".info-group { display: grid"),
                        css.index(".info-group[hidden] { display: none; }"))


class TestNewAlbumStoresInfoFields(unittest.TestCase):
    """真提交一次新建专辑: 出品信息那几项必须落库(以前只有编辑页能存)."""

    def setUp(self) -> None:
        import shutil
        import time

        from dddb.db import Store
        from dddb.public import auth
        from dddb.public.users import UserStore
        from tests import _tmp

        self.tmp = _tmp.temp_path("m19622-album")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.db_path = self.tmp / "dddb.sqlite3"
        Store(self.db_path).close()
        users_path = self.tmp / "users.sqlite3"
        users = UserStore(users_path)
        ok, message = users.create_user("editor1", "pw-123456", role="editor")
        self.assertTrue(ok, message)
        uid = int((users.get("editor1") or {}).get("id") or 1)
        self.site = S.build_site(
            None, root=self.tmp, db_path=self.db_path, users_db=users_path,
            sessions_db=self.tmp / "sessions.sqlite3",
            covers_dir=self.tmp / "covers")
        self.addCleanup(self.site.close)
        sess = auth.Session(sid="s1", user_id=uid, username="editor1",
                            role="editor", expires_at=time.time() + 3600)
        state = auth.AuthState(session=sess)
        self.site.sessions.state = lambda cookies: state
        self.site.same_origin = lambda *a, **k: (True, "")
        self.site.sessions.signer.check = lambda b, g: True

    def test_fields_survive_creation(self) -> None:
        import urllib.parse

        from dddb.db import Store

        body = urllib.parse.urlencode({
            "csrf_token": "tk",
            "title_original": "测试专辑",
            "title": "测试专辑",
            "producer": "甲\n乙",
            "original_work": "某作品",
            "platform": "BOOTH",
            "release_kind": "indie",
        }).encode()
        resp = self.site.handle(
            "POST", "/new/album", body=body,
            headers={"content-type": "application/x-www-form-urlencoded",
                     "origin": "http://x", "host": "x", "referer": "http://x/"})
        self.assertEqual(303, resp.status, "新建专辑应该 303 跳详情页")
        store = Store(self.db_path)
        try:
            items = store.list_albums(limit=5)
            key = items[0].key if items else ""
            album = store.get_album(key)
        finally:
            store.close()
        self.assertIsNotNone(album)
        data = album.to_dict()
        self.assertEqual("甲、乙", data.get("producer"))
        self.assertEqual("某作品", data.get("original_work"))
        self.assertEqual("BOOTH", data.get("platform"))
        self.assertEqual("indie", data.get("release_kind"))


class TestVersionsBumped(unittest.TestCase):
    def test_css_version(self) -> None:
        src = (ROOT / "dddb/public/templates.py").read_text(encoding="utf-8")
        found = re.search(r"public\.css\?v=(\d+)", src)
        self.assertIsNotNone(found)
        self.assertGreaterEqual(int(found.group(1)), 88)


if __name__ == "__main__":
    unittest.main()
