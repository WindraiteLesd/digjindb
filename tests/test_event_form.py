"""会次表单: 去重 + 图片上传 + 字段可保存.

用户反馈:
  ① 会次编辑里字段重复: 「所属系列」与「类型」都是 M3,
     「会次号」与「会次标签」是同一个东西
  ② 还在用图片 URL 而不是直接上传
  ③ 最上面几个条目无法保存(series/vol_label/logo_url/links 根本没渲染)
  ④ 去掉多余的描述类字段
"""

from __future__ import annotations

import base64
import re
import unittest
from pathlib import Path

from tests._tmp import temp_path

ROOT = Path(__file__).resolve().parents[1]

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
    "hQGAhKmMIQAAAABJRU5ErkJggg==")

SERIES = [{"name": "Music Media-Mix Market", "abbr": "M3"},
          {"name": "Comic Market", "abbr": "CM"}]


def _ctx(editor: bool = True):
    from dddb.public import templates as T

    return T.SiteContext(csrf_token="tok", is_editor=editor, lang="zh",
                         user={"id": 1, "username": "ed",
                               "role": "editor" if editor else "member"})


def _names(html: str) -> list[str]:
    skip = {"viewport", "color-scheme", "q", "background", "action",
            "csrf_token"}
    return [n for n in re.findall(r'name="([^"]+)"', html) if n not in skip]


class TestNoDuplicateFields(unittest.TestCase):
    """重复字段必须去掉。"""

    def test_no_kind_field(self) -> None:
        """「类型」与「所属系列」重复 -> 去掉 kind。"""
        from dddb.public import templates as T

        names = [f[0] for f in T.NEW_ENTITY_SPECS["event"][1]]
        self.assertNotIn("kind", names, "「类型」与「所属系列」重复")

    def test_no_vol_field(self) -> None:
        """「会次号」与「会次标签」重复 -> 去掉 vol, 从标签反推。"""
        from dddb.public import templates as T

        names = [f[0] for f in T.NEW_ENTITY_SPECS["event"][1]]
        self.assertNotIn("vol", names, "「会次号」应从「会次标签」反推")

    def test_no_description_field(self) -> None:
        """不加描述类字段。"""
        from dddb.public import templates as T

        names = [f[0] for f in T.NEW_ENTITY_SPECS["event"][1]]
        for bad in ("description", "desc", "note", "bio"):
            self.assertNotIn(bad, names, f"不该有 {bad}")


class TestCreateFormComplete(unittest.TestCase):
    """新建表单: 每个 spec 字段都要渲染出来(之前缺 5 个)。"""

    def setUp(self) -> None:
        from dddb.public import templates as T

        self.html = T.new_entity_page(
            _ctx(), "event", bind_options={"event_series": SERIES})
        self.names = _names(self.html)

    def test_all_spec_fields_rendered(self) -> None:
        from dddb.public import templates as T

        for key, _label, _type, _hint in T.NEW_ENTITY_SPECS["event"][1]:
            self.assertIn(key, self.names, f"字段 {key} 没渲染(填了也存不了)")

    def test_series_is_select(self) -> None:
        self.assertIn('<select name="series"', self.html)
        self.assertIn("Music Media-Mix Market", self.html)

    def test_logo_is_file_upload(self) -> None:
        self.assertIn('type="file"', self.html)
        self.assertIn('name="logo"', self.html)
        self.assertNotIn('name="logo_url"', self.html)


class TestEditFormComplete(unittest.TestCase):
    """编辑表单: 同样要完整 + 图片预览 + 值回填。"""

    def setUp(self) -> None:
        from dddb.public import templates as T

        self.values = {
            "name": "M3-2026秋", "series": "Music Media-Mix Market",
            "vol_label": "M3-58", "year": "2026", "season": "秋",
            "start_date": "2026-10-25", "end_date": "2026-10-26",
            "tags": "东方",
            "links": "官网: https://m3.example.com/",
        }
        self.html = T.edit_profile_page(
            _ctx(), "event", "M3-2026秋", values=self.values,
            back_url="/event/x", series_options=SERIES, has_logo=True)
        self.names = _names(self.html)

    def test_all_spec_fields_rendered(self) -> None:
        from dddb.public import templates as T

        for key, _label, _type, _hint in T.NEW_ENTITY_SPECS["event"][1]:
            self.assertIn(key, self.names, f"编辑表单缺 {key}")

    def test_multipart_form(self) -> None:
        """没有 multipart 文件不会随 POST 发出。"""
        self.assertIn("multipart/form-data", self.html)

    def test_logo_upload_with_preview(self) -> None:
        self.assertIn('type="file"', self.html)
        self.assertIn("series-edit-thumb", self.html,
                      "已有图应显示预览")

    def test_values_filled(self) -> None:
        self.assertIn("M3-58", self.html)
        self.assertIn("m3.example.com", self.html)
        self.assertIn("2026-10-25", self.html)

    def test_series_selected(self) -> None:
        self.assertIn("selected", self.html)

    def test_date_input_type(self) -> None:
        self.assertIn('type="date"', self.html)


class TestVolFromLabel(unittest.TestCase):
    """会次号从标签反推: 取**最后一组**数字。"""

    def test_cases(self) -> None:
        from dddb.public.eventseries import vol_from_label

        self.assertEqual(vol_from_label("M3-58"), 58, "不能是 358")
        self.assertEqual(vol_from_label("C108"), 108)
        self.assertEqual(vol_from_label("例大祭21"), 21)
        self.assertEqual(vol_from_label("秋例13"), 13)
        self.assertEqual(vol_from_label(""), 0)
        self.assertEqual(vol_from_label("没有数字"), 0)


class TestEventLogoUpload(unittest.TestCase):
    """会次图上传: 落盘 + 入库 logo_local."""

    def setUp(self) -> None:
        from dddb.db import Store
        from dddb.public.site import build_site

        self.tmp = temp_path("event-logo")
        self.db = self.tmp / "t.sqlite3"
        self.covers = self.tmp / "covers"
        self.covers.mkdir(parents=True)
        store = Store(self.db)
        store.save_event("M3-2026秋", vol=58, vol_label="M3-58")
        store.close()
        self.site = build_site(
            None, root=self.tmp, db_path=self.db,
            users_db=self.tmp / "u.sqlite3",
            sessions_db=self.tmp / "s.sqlite3", covers_dir=self.covers)

    def tearDown(self) -> None:
        import shutil

        self.site.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_upload_writes_logo_local(self) -> None:
        from dddb.db import Store

        B = "----ev"
        parts = [
            ('--{}\r\nContent-Disposition: form-data; name="logo";'
             ' filename="e.png"\r\nContent-Type: image/png\r\n\r\n'
             .format(B)).encode() + PNG,
        ]
        body = (b"\r\n".join(parts) + b"\r\n--" + B.encode() + b"--\r\n")
        headers = {"content-type":
                   "multipart/form-data; boundary={}".format(B)}
        values = self.site._values_from_multipart(headers, body)
        self.assertIn("logo", values, "文件被丢了")

        saved = self.site._save_event_logo_file("M3-2026秋", values)
        self.assertTrue(saved, "图没落盘")
        self.assertTrue(Path(saved).is_file())

        store = Store(self.db)
        row = store.query_one(
            "SELECT logo_local FROM events WHERE name = ?", ("M3-2026秋",))
        store.close()
        self.assertEqual(str(row["logo_local"]), saved)


class TestBindOptionsSeries(unittest.TestCase):
    """bind_options 要给会次表单提供系列候选。"""

    def test_provides_event_series(self) -> None:
        from dddb.db import Store
        from dddb.public.site import build_site

        tmp = temp_path("bind-series")
        (tmp / "covers").mkdir(parents=True, exist_ok=True)
        db = tmp / "t.sqlite3"
        store = Store(db)
        store.save_series("Music Media-Mix Market", abbr="M3")
        store.close()
        site = build_site(None, root=tmp, db_path=db,
                          users_db=tmp / "u.sqlite3",
                          sessions_db=tmp / "s.sqlite3",
                          covers_dir=tmp / "covers")
        try:
            opts = site.bind_options()
            self.assertIn("event_series", opts)
            self.assertEqual(opts["event_series"][0]["abbr"], "M3")
        finally:
            site.close()
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
