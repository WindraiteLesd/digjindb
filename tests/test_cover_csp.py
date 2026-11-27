"""修: CSP 拦 blob -> 图片读取失败 + 文案精简.

用户截图: 裁切弹窗弹「图片读取失败，请换一张试试」。

根因: CSP 的 ``img-src`` 是 ``'self' https: data:``，**没有 blob:**。
``URL.createObjectURL(file)`` 产出 ``blob:https://...``，被 CSP 拦 ->
``img.onerror`` 立刻触发。

修法: 改用 ``FileReader → data: URL``(已在白名单里)，同时 CSP 也补上
``blob:`` 作为保险。

另一项(用户要求): 删掉「其他照片 不参与封面，可加备注」「＋ 添加照片」
「选择后先调整范围，确认才上传」三处文案；其他照片统一用大加号添加。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CROP = ROOT / "dddb/public/static/cover-crop.js"
SITE = ROOT / "dddb/public/site.py"
TPL = ROOT / "dddb/public/templates.py"


class TestCspAllowsBlob(unittest.TestCase):
    def test_img_src_has_blob(self) -> None:
        src = SITE.read_text(encoding="utf-8")
        m = re.search(r'"default-src \'none\'; img-src[^"]*"', src)
        self.assertIsNotNone(m, "找不到 CSP 字符串")
        self.assertIn("blob:", m.group(0),
                      "img-src 不含 blob: -> createObjectURL 会被拦")

    def test_still_has_self_data(self) -> None:
        src = SITE.read_text(encoding="utf-8")
        m = re.search(r'"default-src \'none\'; img-src[^"]*"', src)
        policy = m.group(0)
        for token in ("'self'", "data:"):
            self.assertIn(token, policy)


class TestUsesFileReader(unittest.TestCase):
    def test_file_reader_used(self) -> None:
        js = CROP.read_text(encoding="utf-8")
        self.assertIn("FileReader", js)
        self.assertIn("readAsDataURL", js)

    def test_no_object_url_in_code(self) -> None:
        """不该再用 createObjectURL 设置 src(会撞 CSP)。"""
        js = CROP.read_text(encoding="utf-8")
        # 去掉注释行再查
        code = "\n".join(
            ln for ln in js.split("\n") if not ln.strip().startswith("//"))
        self.assertNotIn("createObjectURL(file)", code,
                         "还在用 createObjectURL")

    def test_has_onerror(self) -> None:
        js = CROP.read_text(encoding="utf-8")
        self.assertIn("onerror", js)


class TestTextRemoved(unittest.TestCase):
    def _html(self, photos=None):
        from dddb.public import templates as T

        ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                            user={"id": 1, "username": "e", "role": "editor"})
        album = {"key": "A|B|2026", "title_original": "B", "has_cover": False,
                 "cover_local": "", "cover": "", "photos": photos or []}
        return T.edit_album_page(ctx, album=album,
                                 values=T.edit_values_from_album(album),
                                 creating=False)

    def test_no_photo_head_text(self) -> None:
        html = self._html([{"url": "/p/1.jpg", "note": "x"}])
        # 「其他照片」现在只作为加号的 title / 合并选项文字出现,
        # 不再是一段标题文案 —— 用户要求把它做成加号入口。
        for text in ("不参与封面", "可加备注"):
            self.assertNotIn(text, html, f"还有文案 {text}")
        self.assertNotIn("<h3>其他照片</h3>", html)
        self.assertNotIn('class="photo-head"', html)

    def test_add_photo_is_plus_button(self) -> None:
        """附图入口: 用户要求做成加号按钮, 不再是一行文字按钮。"""
        html = self._html([{"url": "/p/1.jpg", "note": "x"}])
        self.assertIn('data-cover-add="1"', html)
        self.assertNotIn("＋ 添加照片", html)

    def test_no_crop_hint(self) -> None:
        html = self._html()
        self.assertNotIn("选择后先调整范围", html)
        self.assertNotIn("确认才上传", html)

    def test_photo_block_hidden_when_empty(self) -> None:
        html = self._html()
        self.assertNotIn("photo-block", html,
                         "没有照片时不该渲染空区块")

    def test_photos_still_rendered(self) -> None:
        html = self._html([{"url": "/p/1.jpg", "note": "会场照"}])
        self.assertIn("photo-block", html)
        self.assertIn("/p/1.jpg", html)
        self.assertIn("会场照", html)

    def test_simple_hint_kept(self) -> None:
        html = self._html()
        self.assertIn("PNG / JPEG / WebP", html)


class TestPlusIsPhotoEntry(unittest.TestCase):
    def test_plus_picks_photo(self) -> None:
        js = CROP.read_text(encoding="utf-8")
        idx = js.find("data-cover-add")
        self.assertGreater(idx, 0)
        window = js[idx:idx + 400]
        self.assertIn('pick("photo")', window,
                      "大加号应作为添加照片的入口")

    def test_no_photo_add_binding(self) -> None:
        js = CROP.read_text(encoding="utf-8")
        self.assertNotIn('querySelector("[data-photo-add]")', js)

    def test_thumb_switches_cover(self) -> None:
        js = CROP.read_text(encoding="utf-8")
        idx = js.find("edit-cover-thumb")
        self.assertGreater(idx, 0)
        # 窗口要足够长: 用户 m18302 起这段点击处理前面多了一段注释
        # (说明「放大」按钮要放行), 写死 500 字符会截在注释里。
        window = js[idx:idx + 900]
        self.assertIn('pick("cover")', window)


if __name__ == "__main__":
    unittest.main()
