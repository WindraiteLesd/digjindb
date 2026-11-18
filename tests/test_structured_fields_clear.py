"""测试: 删光外链/词条/曲目后保存必须真的生效.

对应反馈: "外部链接修改后不生效" / "删掉的又回来了" / "编辑页里自己就是旧的"。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests._tmp import temp_dir

ROOT = Path(__file__).resolve().parents[1]


class TestStructuredFieldsClear(unittest.TestCase):
    """结构化三件套(外链/词条/曲目): 提交了字段就该按用户所见写入."""

    def setUp(self) -> None:
        from dddb.public.site import parse_album_form

        self.parse = parse_album_form
        self.current = {
            "urls": {"官网": "https://a.com/", "推特": "https://x.com/b"},
            "credits": [{"role": "作曲", "name": "某人"},
                        {"role": "编曲", "name": "另一人"}],
            "tracks": [{"title": "曲一"}, {"title": "曲二"}],
        }

    def _patch(self, extra: dict) -> dict:
        values = {"title": ["T"], "title_original": ["T"], **extra}
        patch, errors = self.parse(values, current=self.current)
        self.assertEqual(errors, {}, f"不该有校验错误: {errors}")
        return patch

    # ---------- 外链 ----------
    def test_clear_all_urls(self) -> None:
        """删光外链 -> 真的清空(以前会被 _keep_original 拦下)."""
        patch = self._patch({"url_key": [""], "url_value": [""]})
        self.assertIn("urls", patch, "删光外链必须写入")
        self.assertEqual(patch["urls"], {}, "应为空 dict")

    def test_modify_url(self) -> None:
        patch = self._patch({"url_key": ["官网", "推特"],
                             "url_value": ["https://NEW.com/", "https://x.com/b"]})
        self.assertEqual(patch["urls"]["官网"], "https://NEW.com/")

    def test_delete_one_url_keep_other(self) -> None:
        patch = self._patch({"url_key": ["推特", ""],
                             "url_value": ["https://x.com/b", ""]})
        self.assertEqual(patch["urls"], {"推特": "https://x.com/b"})

    def test_add_url(self) -> None:
        patch = self._patch({"url_key": ["官网", "推特", "新站"],
                             "url_value": ["https://a.com/", "https://x.com/b",
                                           "https://new.com/"]})
        self.assertIn("新站", patch["urls"])

    # ---------- 词条 ----------
    def test_clear_all_credits(self) -> None:
        patch = self._patch({"credit_role": [""], "credit_name": [""]})
        self.assertIn("credits", patch, "删光词条必须写入")
        self.assertEqual(patch["credits"], [])

    # ---------- 曲目 ----------
    def test_clear_all_tracks(self) -> None:
        patch = self._patch({"track_title": [""], "track_index": [""]})
        self.assertIn("track_list", patch, "删光曲目必须写入")
        self.assertEqual(patch["track_list"], [])

    def test_modify_track(self) -> None:
        patch = self._patch({"track_title": ["新曲名"], "track_index": ["1"]})
        self.assertEqual(patch["track_list"][0]["title"], "新曲名")

    # ---------- 保护: 字段不存在时必须保留原值 ----------
    def test_empty_form_keeps_everything(self) -> None:
        """表单里完全没有这三块的字段 -> 一个都不许写(防清空)."""
        patch = self._patch({})
        for field in ("urls", "credits", "track_list"):
            self.assertNotIn(field, patch, f"{field} 不该被写(会清空数据)")

    def test_paste_only_does_not_clear_tracks(self) -> None:
        """只有粘贴框(没有曲目行) -> 不能清空已有曲目表(踩过的坑)."""
        patch = self._patch({"track_paste": ["随便一段没格式的文字"]})
        self.assertNotIn("track_list", patch,
                         "没有曲目行时不许动 track_list")

    def test_invalid_url_still_rejected(self) -> None:
        values = {"title": ["T"], "title_original": ["T"],
                  "url_key": ["坏链"], "url_value": ["ftp://bad.com/"]}
        _patch, errors = self.parse(values, current=self.current)
        self.assertIn("urls", errors)


class TestUrlsPersistedToDb(unittest.TestCase):
    """真正的落库验证: 改/删外链后重新读回来是新的."""

    def setUp(self) -> None:
        self._ctx = temp_dir("urlsdb-")
        self.root = Path(self._ctx.__enter__())
        from dddb.config import Config
        from dddb.db import Store
        from dddb.models import Album

        self.config = Config.default(root=self.root)
        self.config.ensure_dirs()
        store = Store(self.config.db_path)
        store.upsert_album(Album.from_dict({
            "key": "u", "title": "外链落库测试", "circle": "C",
            "artists": [], "tracks": [{"title": "曲一"}], "credits": [],
            "urls": {"官网": "https://old.com/"},
            "source_ids": {}}))
        store.close()

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def _edit(self, extra: dict) -> None:
        from dddb.db import Store
        from dddb.public.site import parse_album_form

        store = Store(self.config.db_path)
        album = store.get_album("u")
        values = {"title": ["外链落库测试"],
                  "title_original": ["外链落库测试"], **extra}
        patch, errors = parse_album_form(values, current=album.to_dict())
        self.assertEqual(errors, {}, errors)
        ok, message = store.update_album_fields("u", patch)
        store.close()
        self.assertTrue(ok, message)

    def _urls(self) -> dict:
        from dddb.db import Store

        store = Store(self.config.db_path)
        urls = store.get_album("u").urls
        store.close()
        return dict(urls)

    def test_modify_persists(self) -> None:
        self._edit({"url_key": ["官网"],
                    "url_value": ["https://brand-new.com/"]})
        self.assertEqual(self._urls(), {"官网": "https://brand-new.com/"})

    def test_clear_persists(self) -> None:
        """这条是关键: 删光外链后重新读必须为空(以前会"又回来了")."""
        self._edit({"url_key": [""], "url_value": [""]})
        self.assertEqual(self._urls(), {}, "删掉的外链不该又回来")

    def test_detail_page_shows_new_link(self) -> None:
        """改完后详情页 HTML 里要出现新链接的中转就会变(不再渲染旧条目数)."""
        from dddb.db import Store
        from dddb.public import templates as T
        from dddb.public.data import PublicData, ReadOnlyStore

        self._edit({"url_key": ["官网"],
                    "url_value": ["https://brand-new.com/"]})

        rs = ReadOnlyStore(self.config.db_path)
        try:
            data = PublicData(rs, covers_dir=Path(self.config.data_dir) / "covers")
            album = data.get_album("u")
            ctx = T.SiteContext(csrf_token="t", is_editor=False, lang="zh")
            html = T.album_detail_page(ctx, album=album)
            self.assertNotIn("old.com", html)
            # 链接走后站中转, 不暴露真实域名; 条数应为 1
            self.assertEqual(html.count('class="link-pill"'), 1)
            # /link 序号指向新链接
            self.assertEqual(data.outbound_link_urls("u"),
                             ["https://brand-new.com/"])
        finally:
            if hasattr(rs, "close"):
                rs.close()


if __name__ == "__main__":
    unittest.main()