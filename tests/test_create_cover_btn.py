"""新建专辑页面的封面上传按钮。

新建模式还没有 album key, 封面只能随主表单提交, 不能走异步上传 +
裁切弹窗。所以它有自己的按钮(data-cover-pick)和本地预览,
与编辑模式的 data-cover-upload 分开。
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "dddb" / "public" / "templates.py"
JS = ROOT / "dddb" / "public" / "static" / "public.js"
CSS = ROOT / "dddb" / "public" / "static" / "public.css"


def _create_html() -> str:
    from dddb.public import templates as T

    ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                        user={"id": 1, "username": "e", "role": "editor"})
    return T.edit_album_page(ctx, album={},
                             values=T.edit_values_from_album({}),
                             creating=True, bind_options={"artist": []})


def _edit_html() -> str:
    from dddb.public import templates as T

    ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                        user={"id": 1, "username": "e", "role": "editor"})
    album = {"key": "k|t|2026", "title": "T", "title_original": "T",
             "has_cover": False, "cover_local": "", "cover": "", "urls": {},
             "credits": [], "tracks": [], "tags": [], "artists": []}
    return T.edit_album_page(ctx, album=album,
                             values=T.edit_values_from_album({}),
                             bind_options={"artist": []})


class TestCreateModeHasButton(unittest.TestCase):

    def test_button_present(self) -> None:
        html = _create_html()
        self.assertIn('data-cover-pick="1"', html)
        self.assertIn("上传封面", html)

    def test_preview_img_present(self) -> None:
        html = _create_html()
        self.assertIn("cover-preview-img", html)

    def test_no_async_action(self) -> None:
        """新建模式不能有异步上传地址(还没有 key)。"""
        html = _create_html()
        self.assertNotIn("data-cover-action", html)
        self.assertNotIn("data-cover-next", html)

    def test_no_gallery_slots(self) -> None:
        html = _create_html()
        self.assertNotIn("data-cover-slots", html)
        self.assertNotIn("data-photo-action", html)

    def test_native_input_still_there(self) -> None:
        """原生 file input 必须在, 它才是真正提交封面文件的字段。"""
        html = _create_html()
        self.assertIn('name="cover"', html)
        self.assertIn('class="cover-file"', html)

    def test_hint_kept(self) -> None:
        self.assertIn("保存后随专辑一起创建", _create_html())


class TestEditModeUnchanged(unittest.TestCase):

    def test_edit_uses_upload_btn(self) -> None:
        html = _edit_html()
        self.assertIn('data-cover-upload="1"', html)
        self.assertNotIn('data-cover-pick="1"', html)

    def test_edit_keeps_gallery(self) -> None:
        html = _edit_html()
        self.assertIn("data-cover-slots", html)
        self.assertIn("data-cover-action", html)


class TestFrontendHandler(unittest.TestCase):

    def setUp(self) -> None:
        self.js = JS.read_text(encoding="utf-8")

    def test_setup_cover_pick_exists(self) -> None:
        self.assertIn("function setupCoverPick(", self.js)

    def test_registered_in_boot(self) -> None:
        i = self.js.find("function boot()")
        self.assertGreater(i, 0)
        self.assertIn("setupCoverPick();", self.js[i:])

    def test_does_not_intercept_change(self) -> None:
        """不能像 cover-crop 那样拦 change 弹裁切框(没有 key 传不上去)。

        按钮本身**可以** preventDefault + input.click() —— 那只是打开
        文件选择框; 不该出现的是裁切弹窗与异步上传。
        """
        i = self.js.find("function setupCoverPick(")
        end = self.js.find("\n  function ", i + 10)
        block = self.js[i:end if end > 0 else len(self.js)]
        self.assertNotIn("openModal", block)
        self.assertNotIn("uploadBlob", block)
        self.assertNotIn("fetch(", block)

    def test_uses_filereader(self) -> None:
        """CSP 的 img-src 未必放行 blob:, 用 data: 更稳。"""
        i = self.js.find("function setupCoverPick(")
        end = self.js.find("\n  function ", i + 10)
        block = self.js[i:end if end > 0 else len(self.js)]
        self.assertIn("FileReader", block)
        self.assertIn("readAsDataURL", block)


class TestStyles(unittest.TestCase):

    def test_preview_css(self) -> None:
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".cover-preview-img", css)
        self.assertIn("object-fit: cover", css)


if __name__ == "__main__":
    unittest.main()
