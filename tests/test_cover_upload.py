"""测试: 封面替换后立即生效 + 上传跳回编辑页."""

from __future__ import annotations

import logging
import unittest
from pathlib import Path

from tests._tmp import temp_dir

ROOT = Path(__file__).resolve().parents[1]


class TestCoverUploadFlow(unittest.TestCase):
    def test_redirects_back_to_edit_page(self) -> None:
        """上传后要回到编辑页(表单 next), 不是专辑详情页."""
        source = (ROOT / "dddb/public/site.py").read_text(encoding="utf-8")
        start = source.find("def _do_album_cover")
        end = source.find("\n    def ", start + 10)
        block = source[start:end]
        self.assertIn("next_url", block, "要读表单里的 next")
        self.assertIn("/edit", block, "默认回编辑页")

    def test_invalidates_data_cache(self) -> None:
        """上传后必须让 data 缓存失效, 否则 /cover 还读旧路径."""
        source = (ROOT / "dddb/public/site.py").read_text(encoding="utf-8")
        start = source.find("def _do_album_cover")
        end = source.find("\n    def ", start + 10)
        block = source[start:end]
        self.assertIn("invalidate", block,
                      "缺 invalidate -> 替换封面后仍显示旧图")

    def test_writes_standard_cover_path(self) -> None:
        """封面写到 covers/<key>.img(封面服务认这个位置)."""
        source = (ROOT / "dddb/public/site.py").read_text(encoding="utf-8")
        start = source.find("def _do_album_cover")
        end = source.find("\n    def ", start + 10)
        block = source[start:end]
        self.assertIn(".img", block)
        self.assertIn("cover_local", block)

    def test_form_has_submit_once(self) -> None:
        from dddb.public import templates as T

        ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                            user={"id": 1, "username": "ed", "role": "editor"})
        album = {"key": "k", "title": "T", "title_original": "T",
                 "title_alt": "", "romaji": "", "circle": "", "artists": [],
                 "tracks": [], "credits": [], "urls": {}, "tags": [],
                 "genres": [], "missing": [], "cover": "", "has_cover": False,
                 "cover_local": "", "date": "", "event": "", "catalog_no": "",
                 "price": "", "hall": "", "booth": "", "updated_text": "",
                 "event_kind": "", "year": "", "description": "",
                 "track_count": 0}
        html = T.edit_album_page(ctx, album=album,
                                 values=T.edit_values_from_album(album))
        import re

        # 封面**不再是 form**(嵌在主表单里会被浏览器丢弃), 改成 div + JS 上传,
        # 所以"防重复提交"改由 JS 的按钮禁用实现, next 走 data-cover-next。
        match = re.search(r'<div id="cover-form"[\s\S]{0,900}?</div>\s*</div>',
                          html)
        self.assertIsNotNone(match, "找不到封面区")
        block = match.group(0)
        self.assertIn('data-cover-action', block, "要带上传地址")
        self.assertIn('data-cover-next', block, "要带 next 回编辑页")
        self.assertIn('data-cover-upload', block, "要有上传按钮")
        self.assertIn("cover-picked", block, "要显示已选文件名")
        self.assertIn("上传后会替换当前封面", block, "要说明会替换")
        # 整个封面区不能出现 form 标签(否则会嵌套)
        self.assertNotIn("<form", block, "封面区不能是 form")

    def test_no_auto_submit_js(self) -> None:
        """不应有代码在选中文件后自动提交表单."""
        js = (ROOT / "dddb/public/static/public.js").read_text(encoding="utf-8")
        self.assertNotIn("requestSubmit", js)
        # .submit( 只应出现在注释或不存在
        import re

        calls = [m.group(0) for m in re.finditer(r"\w+\.submit\(\)", js)]
        self.assertEqual(calls, [], f"不应自动提交: {calls}")


class TestCoverReplacement(unittest.TestCase):
    """真实替换封面: 旧图换成新图后 /cover 要返回新内容."""

    def test_replacement_updates_cover_local(self) -> None:
        from dddb.config import Config
        from dddb.db import Store
        from dddb.models import Album

        ctx = temp_dir("cover-")
        root = Path(ctx.__enter__())
        try:
            config = Config.default(root=root)
            config.ensure_dirs()
            store = Store(config.db_path)
            store.upsert_album(Album.from_dict({
                "key": "c", "title": "封面测试", "tracks": [], "source_ids": {},
                "cover_local": "/old/path.png"}))
            before = store.get_album("c").cover_local
            self.assertEqual(before, "/old/path.png")

            # 模拟上传后的更新
            new_path = str(Path(config.data_dir) / "covers" / "c.img")
            ok, message = store.update_album_fields("c", {"cover_local": new_path})
            self.assertTrue(ok, message)
            after = store.get_album("c").cover_local
            self.assertEqual(after, new_path, "cover_local 应指向新文件")
            self.assertNotEqual(before, after)
            store.close()
        finally:
            ctx.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()