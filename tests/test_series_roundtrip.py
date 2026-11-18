"""系列编辑页的保存与回填必须完整.

历史 bug(用户实测):
  ① 链接保存了但**详情页不显示** —— ``event_series_page`` 根本没渲染 links
  ② 图片上传后能显示, 但**再次编辑就没了** —— ``event_series()`` 视图丢掉了
     ``logo_local`` / ``wiki`` / ``links`` 等原始字段, 编辑页拿到的是空值

这两个都不是"保存失败", 而是**读取路径缺字段**。
"""

from __future__ import annotations

import base64
import unittest
from pathlib import Path

from tests._tmp import temp_path

ROOT = Path(__file__).resolve().parents[1]

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
    "hQGAhKmMIQAAAABJRU5ErkJggg==")


def _ctx(editor: bool = True):
    from dddb.public import templates as T

    return T.SiteContext(csrf_token="tok", is_editor=editor, lang="zh",
                         user={"id": 1, "username": "ed",
                               "role": "editor" if editor else "member"})


class TestViewCarriesEditableFields(unittest.TestCase):
    """event_series() 必须带出编辑页要用的原始字段。"""

    def setUp(self) -> None:
        from dddb.db import Store
        from dddb.public.data import PublicData, ReadOnlyStore

        self.tmp = temp_path("series-fields")
        self.db = self.tmp / "t.sqlite3"
        self.covers = self.tmp / "covers"
        self.covers.mkdir(parents=True)

        store = Store(self.db)
        logo = self.covers / "M3-logo.png"
        logo.write_bytes(PNG)
        store.save_series(
            "M3", abbr="M3", full_name="Music Media-Mix Market",
            logo_local=str(logo),
            poster_url="https://e.com/poster.png",
            wiki="# 介绍\n\n**粗体**",
            links={"官网": "https://m3.example.com/",
                   "售票": "https://t.example.com/"})
        store.save_event("M3-2026秋", series="M3", vol=58,
                         vol_label="M3-58")
        store.close()

        self.store = ReadOnlyStore(self.db)
        self.data = PublicData(self.store, root=self.tmp,
                               covers_dir=self.covers)
        self.items = self.data.event_series()

    def tearDown(self) -> None:
        import shutil

        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_keeps_wiki(self) -> None:
        self.assertIn("介绍", self.items[0]["wiki"],
                      "wiki 丢了 -> 编辑页回填不出来")

    def test_keeps_links(self) -> None:
        links = self.items[0]["links"]
        self.assertEqual(len(links), 2, "links 丢了 -> 编辑页回填不出来")
        self.assertIn("官网", links)

    def test_keeps_logo_local(self) -> None:
        self.assertTrue(self.items[0]["logo_local"],
                        "logo_local 丢了 -> 编辑页认不出已有图")
        self.assertTrue(self.items[0]["poster_url"])

    def test_still_has_display_fields(self) -> None:
        """索引页要的字段不能被破坏。"""
        item = self.items[0]
        self.assertTrue(item["logo"].startswith("/event-series-logo/"))
        self.assertTrue(item["has_logo"])
        self.assertEqual(len(item["editions"]), 1)


class TestEditPageFillIn(unittest.TestCase):
    """再次打开编辑页, 内容必须回填。"""

    def setUp(self) -> None:
        from dddb.db import Store
        from dddb.public.data import PublicData, ReadOnlyStore

        self.tmp = temp_path("series-fillin")
        self.db = self.tmp / "t.sqlite3"
        self.covers = self.tmp / "covers"
        self.covers.mkdir(parents=True)
        logo = self.covers / "M3-logo.png"
        logo.write_bytes(PNG)

        store = Store(self.db)
        store.save_series(
            "M3", abbr="M3", full_name="Music Media-Mix Market",
            logo_local=str(logo),
            wiki="# 介绍",
            links={"官网": "https://m3.example.com/"})
        store.close()

        self.store = ReadOnlyStore(self.db)
        self.data = PublicData(self.store, root=self.tmp,
                               covers_dir=self.covers)
        self.series = self.data.event_series()[0]

    def tearDown(self) -> None:
        import shutil

        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_wiki_filled(self) -> None:
        from dddb.public import templates as T

        html = T.event_series_edit_page(_ctx(), series=self.series)
        self.assertIn("介绍", html)

    def test_links_filled(self) -> None:
        from dddb.public import templates as T

        html = T.event_series_edit_page(_ctx(), series=self.series)
        self.assertIn("m3.example.com", html)

    def test_logo_preview_shown(self) -> None:
        """已有图 -> 出 <img>, 不是"无图"占位。"""
        from dddb.public import templates as T

        html = T.event_series_edit_page(_ctx(), series=self.series)
        import re

        imgs = re.findall(r'<img[^>]*series-edit-thumb[^>]*>', html)
        self.assertTrue(imgs, "已有 logo 但编辑页显示无图")
        self.assertIn("/event-series-logo/", imgs[0])


class TestSeriesPageShowsLinks(unittest.TestCase):
    """系列详情页必须渲染相关链接。"""

    def test_links_rendered(self) -> None:
        from dddb.public import templates as T

        html = T.event_series_page(_ctx(), series={
            "name": "M3", "abbr": "M3",
            "full_name": "Music Media-Mix Market",
            "logo": "", "poster": "", "wiki": "",
            "links": {"官网": "https://m3.example.com/",
                      "售票": "https://t.example.com/"},
        }, editions=[])
        self.assertIn('class="link-pill"', html, "系列详情页没渲染链接")
        self.assertEqual(html.count('class="link-pill"'), 2)
        self.assertIn("m3.example.com", html)
        self.assertIn("series-links", html)

    def test_no_links_no_block(self) -> None:
        from dddb.public import templates as T

        html = T.event_series_page(_ctx(), series={
            "name": "M3", "abbr": "M3", "full_name": "M3",
            "logo": "", "poster": "", "wiki": "", "links": {},
        }, editions=[])
        self.assertNotIn("series-links", html)

    def test_dangerous_link_skipped(self) -> None:
        """javascript: 链接不渲染。"""
        from dddb.public import templates as T

        html = T.event_series_page(_ctx(), series={
            "name": "M3", "abbr": "M3", "full_name": "M3",
            "logo": "", "poster": "", "wiki": "",
            "links": {"坏": "javascript:alert(1)"},
        }, editions=[])
        self.assertNotIn("javascript:", html)


class TestSaveThenReadRoundTrip(unittest.TestCase):
    """保存 -> 读库 -> 视图 -> 编辑页, 全链路一致。"""

    def setUp(self) -> None:
        from dddb.db import Store
        from dddb.public.site import build_site

        self.tmp = temp_path("series-roundtrip")
        self.db = self.tmp / "t.sqlite3"
        self.covers = self.tmp / "covers"
        self.covers.mkdir(parents=True)
        Store(self.db).close()
        self.site = build_site(
            None, root=self.tmp, db_path=self.db,
            users_db=self.tmp / "u.sqlite3",
            sessions_db=self.tmp / "s.sqlite3", covers_dir=self.covers)

    def tearDown(self) -> None:
        import shutil

        self.site.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trip(self) -> None:
        from dddb.db import Store

        B = "----rt"
        parts = []

        def field(k, v):
            parts.append(
                ('--{}\r\nContent-Disposition: form-data; name="{}"'
                 '\r\n\r\n{}'.format(B, k, v)).encode())

        def file_part(k, fn, ct, data):
            parts.append(
                ('--{}\r\nContent-Disposition: form-data; name="{}";'
                 ' filename="{}"\r\nContent-Type: {}\r\n\r\n'
                 .format(B, k, fn, ct)).encode() + data)

        field("name", "M3")
        field("abbr", "M3")
        field("full_name", "Music Media-Mix Market")
        field("wiki", "# 标题\n\n正文")
        field("links", "官网: https://m3.example.com/")
        file_part("logo", "logo.png", "image/png", PNG)
        body = (b"\r\n".join(parts) + b"\r\n--" + B.encode() + b"--\r\n")
        headers = {"content-type":
                   "multipart/form-data; boundary={}".format(B)}

        values = self.site._values_from_multipart(headers, body)
        saved = self.site._save_series_image("M3", "logo", values)
        store = Store(self.db)
        store.save_series(
            "M3", abbr="M3", full_name="Music Media-Mix Market",
            logo_local=saved, wiki=values["wiki"][0],
            links=self.site._parse_urls(values["links"][0]))
        store.close()

        # 视图
        from dddb.public.data import PublicData, ReadOnlyStore

        rs = ReadOnlyStore(self.db)
        pub = PublicData(rs, root=self.tmp, covers_dir=self.covers)
        series = pub.event_series()[0]
        rs.close()
        self.assertIn("标题", series["wiki"])
        self.assertIn("官网", series["links"])
        self.assertTrue(series["logo_local"])

        # 编辑页回填
        from dddb.public import templates as T

        edit = T.event_series_edit_page(_ctx(), series=series)
        self.assertIn("标题", edit)
        self.assertIn("m3.example.com", edit)
        self.assertIn("/event-series-logo/", edit)

        # 详情页显示
        detail = T.event_series_page(_ctx(), series=series, editions=[])
        self.assertIn('class="link-pill"', detail)
        self.assertIn("m3.example.com", detail)


if __name__ == "__main__":
    unittest.main()
