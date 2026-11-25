"""封面裁切 + 其他照片.

用户要求:
    "选择文件、上传封面和左侧封面加号三个按钮的作用是相同的，
     但都是同样的问题。修复后顺带把这里的改好，
     选择文件后弹出预览和框选范围界面再按上传，
     最后成功显示在封面槽位，而右侧加号是添加其他照片，
     其他照片不含封面，可以配备注"

实现:
    * 三个入口(选择文件 / 上传封面 / 左侧加号)统一 -> 选图 -> **裁切弹窗**
    * 右侧「＋ 添加照片」 -> 选图 -> 裁切弹窗 -> 落到**其他照片**
    * 其他照片存在 payload.photos, **不参与 cover_local**
    * 照片可加备注、可删除
    * 上传成功后**整页刷新**(服务端渲染, 不靠前端状态拼接)
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests._tmp import temp_path

ROOT = Path(__file__).resolve().parents[1]
CROP_JS = ROOT / "dddb/public/static/cover-crop.js"


class TestCropScript(unittest.TestCase):
    def test_exists(self) -> None:
        self.assertTrue(CROP_JS.is_file(), "缺 cover-crop.js")

    def test_modal_structure(self) -> None:
        js = CROP_JS.read_text(encoding="utf-8")
        for token in ("crop-modal", "crop-stage", "crop-frame",
                      "crop-handle", "canvas", "toBlob"):
            self.assertIn(token, js, f"缺 {token}")

    def test_entries_share_one_picker(self) -> None:
        """封面入口(按钮 + 左侧大图)都走 pick("cover")；
        大加号走 pick("photo")(用户要求: 加号 = 加照片)。"""
        js = CROP_JS.read_text(encoding="utf-8")
        self.assertGreaterEqual(js.count('pick("cover")'), 2,
                                "封面入口没有统一")
        self.assertIn('pick("photo")', js)

    def test_photo_add_uses_photo_kind(self) -> None:
        """照片入口是大加号, 不再有独立按钮。"""
        js = CROP_JS.read_text(encoding="utf-8")
        self.assertIn('pick("photo")', js)
        idx = js.find("data-cover-add")
        self.assertGreater(idx, 0)
        self.assertIn('pick("photo")', js[idx:idx + 400])

    def test_refresh_after_upload(self) -> None:
        """上传成功后整页刷新(不靠前端拼状态)。"""
        js = CROP_JS.read_text(encoding="utf-8")
        self.assertIn("location.href", js)
        self.assertIn("Date.now()", js)

    def test_note_only_for_photo(self) -> None:
        """备注输入框只在 photo 模式显示。"""
        js = CROP_JS.read_text(encoding="utf-8")
        self.assertIn('kind === "photo"', js)

    def test_upload_uses_correct_field(self) -> None:
        js = CROP_JS.read_text(encoding="utf-8")
        self.assertIn('"photo"', js)
        self.assertIn('"cover"', js)


class TestTemplateWiring(unittest.TestCase):
    def _html(self, photos=None):
        from dddb.public import templates as T

        ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                            user={"id": 1, "username": "e", "role": "editor"})
        album = {"key": "A|B|2026", "title_original": "B",
                 "has_cover": False, "cover_local": "", "cover": "",
                 "photos": photos or []}
        return T.edit_album_page(ctx, album=album,
                                 values=T.edit_values_from_album(album),
                                 creating=False)

    def test_photo_block_rendered(self) -> None:
        """有照片才渲染区块; 不再有标题文案。

        编辑页的附图入口是封面上那个加号(`data-cover-add`),
        「其他照片」现在只作为合并选项文字出现, 不再是一段标题。
        """
        html = self._html([{"url": "/photo/x/1.jpg", "note": "会场照"}])
        self.assertIn("photo-block", html)
        self.assertNotIn("<h3>其他照片</h3>", html)
        self.assertNotIn('class="photo-head"', html)

    def test_photo_actions(self) -> None:
        html = self._html()
        for attr in ("data-photo-action", "data-photo-del-action",
                     "data-photo-note-action"):
            self.assertIn(attr, html, f"缺 {attr}")

    def test_photo_note_filled(self) -> None:
        html = self._html([{"url": "/photo/x/1.jpg", "note": "会场照"}])
        self.assertIn("会场照", html)

    def test_crop_script_included(self) -> None:
        html = self._html()
        self.assertIn("cover-crop.js", html)

    def test_upload_hint_simplified(self) -> None:
        """提示已简化: 不再有"先调整范围"的长句。"""
        html = self._html()
        self.assertIn("PNG / JPEG / WebP", html)
        self.assertNotIn("选择后先调整范围", html)

    def test_cover_and_photos_are_separate(self) -> None:
        """封面与照片是两个区块(照片有内容时才出现)。"""
        html = self._html([{"url": "/p/1.jpg", "note": ""}])
        self.assertIn("edit-cover-row", html)
        self.assertIn("photo-block", html)


class TestPhotosData(unittest.TestCase):
    def test_add_and_read(self) -> None:
        from dddb.public.photos import add_photo, photos_of

        p, ok = add_photo("{}", url="/photo/a/1.jpg", path="/tmp/1.jpg",
                          note="会场照")
        self.assertTrue(ok)
        got = photos_of(json.dumps(p))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["note"], "会场照")

    def test_no_duplicate(self) -> None:
        from dddb.public.photos import add_photo

        p, _ = add_photo("{}", url="/photo/a/1.jpg", path="/tmp/1.jpg")
        _, ok = add_photo(json.dumps(p), url="/photo/a/1.jpg",
                          path="/tmp/1.jpg")
        self.assertFalse(ok, "重复 url 不该再加")

    def test_limit(self) -> None:
        from dddb.public.photos import MAX_PHOTOS, add_photo

        payload = "{}"
        for i in range(MAX_PHOTOS):
            payload, ok = add_photo(payload, url="/photo/a/{}.jpg".format(i),
                                    path="/tmp/{}.jpg".format(i))
            self.assertTrue(ok)
        _, ok = add_photo(payload, url="/photo/a/extra.jpg", path="/x.jpg")
        self.assertFalse(ok, "超过上限应拒绝")

    def test_remove(self) -> None:
        from dddb.public.photos import add_photo, photos_of, remove_photo

        p, _ = add_photo("{}", url="/photo/a/1.jpg", path="/tmp/1.jpg")
        p, removed = remove_photo(json.dumps(p), "/photo/a/1.jpg")
        self.assertEqual(removed, "/tmp/1.jpg")
        self.assertEqual(photos_of(json.dumps(p)), [])

    def test_set_note(self) -> None:
        from dddb.public.photos import add_photo, photos_of, set_photo_note

        p, _ = add_photo("{}", url="/photo/a/1.jpg", path="/tmp/1.jpg")
        p, hit = set_photo_note(json.dumps(p), "/photo/a/1.jpg", "特典图")
        self.assertTrue(hit)
        self.assertEqual(photos_of(json.dumps(p))[0]["note"], "特典图")

    def test_photo_dir_stable(self) -> None:
        from dddb.public.photos import photo_dir

        a = photo_dir("/covers", "A|B|2026")
        b = photo_dir("/covers", "A|B|2026")
        self.assertEqual(a, b)
        self.assertIn("photos", str(a))
        c = photo_dir("/covers", "另一张|专辑")
        self.assertNotEqual(a, c, "不同专辑要用不同目录")


class TestPhotosInView(unittest.TestCase):
    def test_view_outputs_photos(self) -> None:
        from dddb.db import Store
        from dddb.models import Album
        from dddb.public.data import PublicData, ReadOnlyStore

        tmp = temp_path("photos-view")
        data = tmp / "data"
        data.mkdir(parents=True)
        covers = data / "covers"
        covers.mkdir()
        db = data / "dddb.sqlite3"
        store = Store(db)
        store.upsert_album(Album.from_dict({
            "key": "A|B|2026", "title": "B", "title_original": "B",
            "photos": [{"url": "/photo/x/1.jpg", "note": "会场照"}],
            "source_ids": {}}))
        store.close()
        try:
            rs = ReadOnlyStore(db)
            pub = PublicData(rs, root=tmp, covers_dir=covers)
            a = pub.get_album("A|B|2026")
            rs.close()
            self.assertTrue(a.get("photos"), "视图没输出 photos")
            self.assertEqual(a["photos"][0]["note"], "会场照")
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


class TestRoutes(unittest.TestCase):
    def test_parse(self) -> None:
        from dddb.public.site import parse_route

        self.assertEqual(parse_route("/album/A%7CB/photo")["name"],
                         "album_photo")
        self.assertEqual(parse_route("/album/A%7CB/photo/del")["name"],
                         "album_photo_del")
        self.assertEqual(parse_route("/album/A%7CB/photo/note")["name"],
                         "album_photo_note")
        self.assertEqual(parse_route("/photo/A%7CB/x.jpg")["name"],
                         "photo")

    def test_handlers_exist(self) -> None:
        from dddb.public.site import PublicSite

        for name in ("serve_photo", "_do_album_photo",
                     "_do_album_photo_del", "_do_album_photo_note"):
            self.assertTrue(hasattr(PublicSite, name), f"缺 {name}")


class TestOldUploadDisabled(unittest.TestCase):
    """旧的封面上传逻辑要停用, 避免两套抢同一个 input。"""

    def test_old_handlers_not_called(self) -> None:
        """boot 里两行旧调用必须被注释掉(否则和 cover-crop.js 打架)。"""
        js = (ROOT / "dddb/public/static/public.js").read_text(
            encoding="utf-8")
        boot = js.split("function boot()")[1].split("\n  }")[0]
        for call in ("setupCoverUpload();", "setupCoverGallery();"):
            for line in boot.split("\n"):
                if call in line:
                    self.assertIn("//", line,
                                  "{} 还在 boot 里没注释".format(call))


if __name__ == "__main__":
    unittest.main()
