"""新建专辑的可靠性(用户 m13494).

线上真实事故: 编辑者填好一整张专辑点保存, 条目**其实已经建成了**, 但收尾
``_save_new_image`` 里读了不存在的 ``self.config``, 于是整次请求 500, 页面
什么都没跳, 草稿也没了。这一组测试锁住四条:

1. 新建路径不再引用 ``self.config``;
2. 成功 -> ``/album/<key>?created=1``(详情页, 不是回编辑页);
3. 失败 -> 500 但**把用户填的内容原样回填**到编辑表单;
4. 图片上传失败只是日志, 不能把整次创建打成 500。
"""

from __future__ import annotations

import inspect
import pathlib
import re
import unittest

from dddb import db as db_mod
from tests import _tmp
from dddb.public import site as site_mod
from dddb.public import templates as T

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS = (ROOT / "dddb" / "public" / "static" / "public.js").read_text(encoding="utf-8")
CSS = (ROOT / "dddb" / "public" / "static" / "public.css").read_text(encoding="utf-8")


def _code_only(src: str) -> str:
    """去掉注释行 —— ``self.config`` 在注释里出现是"解释为什么不能读它". """
    return "\n".join(line for line in src.splitlines()
                     if not line.lstrip().startswith("#"))


def _ctx(**kwargs):
    return T.SiteContext(lang="zh", csrf_token="t", is_editor=True, **kwargs)


class TestNoConfigAttribute(unittest.TestCase):
    """``PublicSite`` 没有 ``config``; 任何地方再读它就又 500 了."""

    def test_save_new_image_does_not_read_config(self):
        src = _code_only(inspect.getsource(site_mod.PublicSite._save_new_image))
        self.assertNotIn("self.config", src)

    def test_new_photos_does_not_read_config(self):
        src = _code_only(inspect.getsource(site_mod.PublicSite._save_new_photos))
        self.assertNotIn("self.config", src)

    def test_save_new_image_uses_data_covers_dir(self):
        src = inspect.getsource(site_mod.PublicSite._save_new_image)
        self.assertIn('getattr(self.data, "covers_dir", None)', src)


class TestCreateRedirect(unittest.TestCase):
    """建完必须落到详情页, 且带上 ``?created=1`` 供前端清草稿."""

    @classmethod
    def setUpClass(cls):
        cls.src = inspect.getsource(site_mod.PublicSite._do_new_entity)

    def test_redirects_to_detail_page(self):
        self.assertIn('"?created=1"', self.src)

    def test_does_not_redirect_back_to_edit(self):
        self.assertNotIn('+ "/edit"', self.src)

    def test_creates_photos_after_album(self):
        self.assertIn("self._save_new_photos(key, values)", self.src)

    def test_image_upload_failure_is_not_fatal(self):
        self.assertIn("新建条目后保存图片失败", self.src)
        # 包在 try 里: 有 except Exception 兜着
        block = self.src.split("self._save_new_image(", 1)[1][:400]
        self.assertTrue(block.startswith("kind, created_key") or "kind" in block)
        self.assertIn("except Exception", self.src)


class TestFailureKeepsData(unittest.TestCase):
    """报错必须回到"保存前那一屏", 一个字段都不能丢."""

    def test_safe_wrapper_exists(self):
        self.assertTrue(hasattr(site_mod.PublicSite, "_new_entity_with_fallback"))
        self.assertTrue(hasattr(site_mod.PublicSite, "_new_entity_failed"))

    def test_wrapper_catches_and_renders_form(self):
        src = inspect.getsource(site_mod.PublicSite._new_entity_with_fallback)
        self.assertIn("try:", src)
        self.assertIn("except Exception", src)
        self.assertIn("_new_entity_failed", src)

    def test_failure_page_reuses_submitted_values(self):
        src = inspect.getsource(site_mod.PublicSite._new_entity_failed)
        self.assertIn("edit_album_page", src)
        self.assertIn("creating=True", src)
        self.assertIn("dict(values", src)

    def test_failure_status_is_500(self):
        src = inspect.getsource(site_mod.PublicSite._new_entity_failed)
        self.assertIn("status=500", src)

    def test_write_dispatch_uses_safe_wrapper(self):
        src = inspect.getsource(site_mod.PublicSite._handle_write)
        self.assertIn("_new_entity_with_fallback", src)


class TestNewAlbumPageUI(unittest.TestCase):
    """新建页的封面上传与「其他照片」."""

    @classmethod
    def setUpClass(cls):
        cls.html = T.edit_album_page(_ctx(), album={}, values={}, errors={},
                                     creating=True)

    def test_cover_fallback_is_a_plus_button(self):
        self.assertIn("cover-add-thumb", self.html)
        self.assertIn("cover-plus", self.html)
        self.assertIn('data-cover-pick="1"', self.html)

    def test_cover_fallback_is_not_plain_text(self):
        self.assertNotIn("<span>新建</span>", self.html)

    def test_has_photo_slots(self):
        self.assertIn('data-photo-local="1"', self.html)
        self.assertIn('data-photo-add="1"', self.html)
        self.assertIn('id="photos-data"', self.html)
        self.assertIn('name="photos_data"', self.html)

    def test_has_hidden_multi_file_input(self):
        self.assertIn('id="photo-input"', self.html)
        self.assertIn("multiple", self.html)

    def test_has_draft_history_button(self):
        self.assertIn('data-draft-history="1"', self.html)

    def test_has_no_merge_or_delete_form(self):
        self.assertNotIn("merge-album-form", self.html)
        self.assertNotIn("delete-album-form", self.html)


class TestEditPageMergeUI(unittest.TestCase):
    """编辑页要有「合并专辑」, 且表单必须在主表单之外."""

    @classmethod
    def setUpClass(cls):
        cls.html = T.edit_album_page(_ctx(), album={"key": "a|b|2026", "title": "A"},
                                     values={}, errors={})

    def test_has_merge_form(self):
        self.assertIn('id="merge-album-form"', self.html)
        self.assertIn("/merge", self.html)
        self.assertIn('name="target"', self.html)

    def test_merge_form_is_outside_main_form(self):
        main_end = self.html.index("</form>")
        self.assertGreater(self.html.index('id="merge-album-form"'), main_end)

    def test_delete_still_there(self):
        self.assertIn("delete-album-form", self.html)

    def test_merge_asks_for_confirmation(self):
        self.assertIn("data-confirm", self.html)


class TestMergeRoute(unittest.TestCase):
    def test_merge_route_parses(self):
        self.assertEqual(
            site_mod.parse_route("/album/a%7Cb%7C2026/merge"),
            {"name": "album_merge", "arg": "a|b|2026"})

    def test_logo_route_parses(self):
        self.assertEqual(site_mod.parse_route("/logo/artist-x.png"),
                         {"name": "logo", "arg": "artist-x.png"})

    def test_logo_is_entity_route(self):
        self.assertIn("logo", site_mod._ENTITY_ROUTES)

    def test_album_key_from_plain_key(self):
        self.assertEqual(
            site_mod.PublicSite._album_key_from_input("a|b|2026"), "a|b|2026")

    def test_album_key_from_path(self):
        self.assertEqual(
            site_mod.PublicSite._album_key_from_input("/album/a|b|2026/edit"),
            "a|b|2026")

    def test_album_key_from_absolute_url(self):
        self.assertEqual(
            site_mod.PublicSite._album_key_from_input(
                "https://www.digjindb.top/album/a%7Cb%7C2026?x=1"),
            "a|b|2026")

    def test_album_key_from_blank(self):
        self.assertEqual(site_mod.PublicSite._album_key_from_input("  "), "")
        self.assertEqual(site_mod.PublicSite._album_key_from_input(None), "")

    def test_merge_handler_requires_editor(self):
        src = inspect.getsource(site_mod.PublicSite._do_album_merge)
        self.assertIn("can_edit(state)", src)

    def test_merge_handler_keeps_target(self):
        src = inspect.getsource(site_mod.PublicSite._do_album_merge)
        self.assertIn("keep_key=target", src)
        self.assertIn("drop_key=key", src)

    def test_merge_rejects_self(self):
        src = inspect.getsource(site_mod.PublicSite._do_album_merge)
        self.assertIn("不能把专辑合并到自己", src)


class TestMergeAlbumsMergesCreditsAndPhotos(unittest.TestCase):
    """底库的 merge_albums 以前只并标量/曲目/外链, 制作人员与附图会丢."""

    def setUp(self):
        self.tmp = _tmp.temp_path("merge")
        self.store = db_mod.Store(self.tmp / "t.sqlite3")
        from dddb.models import Album

        keep = Album.from_dict({
            "key": "k|keep|2026", "title": "Keep", "circle": "C",
            "credits": [{"name": "甲", "role": "produce"}],
            "photos": [{"url": "/photo/k/a.png", "note": "甲"}],
        })
        drop = Album.from_dict({
            "key": "k|drop|2026", "title": "Drop",
            "credits": [{"name": "甲", "role": "produce"},
                        {"name": "乙", "role": "arrange"}],
            "photos": [{"url": "/photo/k/a.png", "note": "重复"},
                       {"url": "/photo/k/b.png", "note": "乙"}],
        })
        self.store.upsert_album(keep)
        self.store.upsert_album(drop)

    def tearDown(self):
        self.store.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_merge_returns_true(self):
        self.assertTrue(self.store.merge_albums("k|keep|2026", "k|drop|2026"))

    def test_credits_union_without_duplicates(self):
        self.store.merge_albums("k|keep|2026", "k|drop|2026")
        album = self.store.get_album("k|keep|2026")
        got = sorted((c.role, c.name) for c in album.credits)
        self.assertEqual(got, [("arrange", "乙"), ("produce", "甲")])

    def test_photos_union_by_url(self):
        self.store.merge_albums("k|keep|2026", "k|drop|2026")
        album = self.store.get_album("k|keep|2026")
        urls = sorted(str(p.get("url")) for p in album.photos)
        self.assertEqual(urls, ["/photo/k/a.png", "/photo/k/b.png"])

    def test_drop_row_is_gone(self):
        self.store.merge_albums("k|keep|2026", "k|drop|2026")
        self.assertIsNone(self.store.get_album("k|drop|2026"))


class TestSaveNewPhotos(unittest.TestCase):
    """新建页带上的其他照片(data URL) -> 建完专辑再落盘."""

    def setUp(self):
        self.tmp = _tmp.temp_path("newphotos")
        self.covers = self.tmp / "covers"
        self.covers.mkdir()
        self.db = self.tmp / "t.sqlite3"
        self.store = db_mod.Store(self.db)
        from dddb.models import Album

        self.store.upsert_album(Album.from_dict(
            {"key": "x|y|2026", "title": "Y"}))

    def tearDown(self):
        self.store.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _site(self):
        class _Data:
            covers_dir = str(self.covers)

            def invalidate(self):
                pass

        site = site_mod.PublicSite.__new__(site_mod.PublicSite)
        site.db_path = self.db
        site.data = _Data()
        return site

    def test_writes_photo_and_payload(self):
        import base64
        png = base64.b64encode(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 40).decode("ascii")
        values = {"photos_data": ['[{"data": "data:image/png;base64,'
                                  + png + '", "note": "封面背面"}]']}
        saved = self._site()._save_new_photos("x|y|2026", values)
        self.assertEqual(saved, 1)
        album = self.store.get_album("x|y|2026")
        self.assertEqual(len(album.photos), 1)
        self.assertEqual(album.photos[0].get("note"), "封面背面")
        self.assertTrue(str(album.photos[0].get("url")).startswith("/photo/"))

    def test_bad_payload_is_ignored(self):
        saved = self._site()._save_new_photos("x|y|2026",
                                              {"photos_data": ["not json"]})
        self.assertEqual(saved, 0)

    def test_empty_payload_is_ignored(self):
        self.assertEqual(self._site()._save_new_photos("x|y|2026", {}), 0)


class TestLogoRoute(unittest.TestCase):
    """``/logo/<file>``: 艺人头像/社团徽标要能读回来(新建时写的就是这个地址)."""

    def setUp(self):
        self.tmp = _tmp.temp_path("logo")
        self.covers = self.tmp / "covers"
        (self.covers / "logos").mkdir(parents=True)
        self.db = self.tmp / "t.sqlite3"
        db_mod.Store(self.db).close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _site(self):
        class _Data:
            covers_dir = str(self.covers)

            def invalidate(self):
                pass

        site = site_mod.PublicSite.__new__(site_mod.PublicSite)
        site.db_path = self.db
        site.data = _Data()
        return site

    def test_serves_existing_file(self):
        (self.covers / "logos" / "artist-x.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        resp = self._site().serve_logo_image("artist-x.png")
        self.assertEqual(resp.status, 200)

    def test_missing_file_is_404(self):
        self.assertEqual(self._site().serve_logo_image("nope.png").status, 404)

    def test_rejects_path_traversal(self):
        for bad in ("../secrets.txt", "..", "", "a/b.png"):
            self.assertEqual(self._site().serve_logo_image(bad).status, 404, bad)

    def test_written_url_is_served(self):
        """_save_new_image 写进条目的地址必须真的可用."""
        from dddb.models import Circle
        store = db_mod.Store(self.db)
        store.upsert_circle(Circle.from_dict(
            {"name": "某社团", "logo_url": "/logo/circle-x.png"}))
        store.close()
        (self.covers / "logos" / "circle-x.png").write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        self.assertEqual(self._site().serve_logo_image("circle-x.png").status, 200)


class TestFrontendWiring(unittest.TestCase):
    """public.js / public.css 里必须有的几块."""

    def test_cover_pick_accepts_multiple_entries(self):
        self.assertIn('querySelectorAll("[data-cover-pick]")', JS)

    def test_setup_local_photos_exists(self):
        self.assertIn("function setupLocalPhotos()", JS)
        self.assertIn("setupLocalPhotos();", JS)

    def test_draft_kept_on_submit(self):
        self.assertIn("PENDING_KEY", JS)
        self.assertIn("function settlePendingDraft()", JS)
        self.assertIn("settlePendingDraft();", JS)

    def test_draft_history_exists(self):
        self.assertIn(":history", JS)
        self.assertIn("function pushHistory(", JS)
        self.assertIn("function buildHistoryPanel(", JS)
        self.assertIn("data-draft-history", JS)

    def test_apply_draft_is_shared(self):
        self.assertIn("function applyDraft(saved, note)", JS)

    def test_css_has_new_blocks(self):
        for name in (".cover-add-thumb", ".photo-list", ".draft-history-panel",
                     "#merge-album-form"):
            self.assertIn(name, CSS, name)


class TestVersionsBumped(unittest.TestCase):
    def test_css_and_js_versions(self):
        html = T.edit_album_page(_ctx(), album={}, values={}, errors={},
                                 creating=True)
        self.assertIn("public.css?v=76", html)
        self.assertIn("public.js?v=66", html)


class TestSourceHasNoStaleText(unittest.TestCase):
    """新建页提示语要指向详情页(不是编辑页)."""

    def test_intro_says_detail_page(self):
        html = T.edit_album_page(_ctx(), album={}, values={}, errors={},
                                 creating=True)
        self.assertNotIn("会创建一张新专辑并进入它的编辑页", html)
        self.assertIn("详情页", html)

    def test_no_unused_placeholder(self):
        src = inspect.getsource(site_mod.PublicSite._do_new_entity)
        self.assertIsNone(re.search(r"self\.config", src))