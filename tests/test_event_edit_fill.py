"""会次编辑页的四项修复.

用户报告:
    ① "所属展会、会次标签、外链在编辑界面不保存"
       -> 根因: ``_entity_form_values`` 的 event 分支只回填 7 个字段,
          少了 series / vol_label / links。编辑页这些框是空的, 用户
          以为丢了; 重填保存后又被"没带就保留原值"挡住, 看着像不保存。
    ② "标签这一条目和会次标签合并" -> 去掉 tags 字段
    ③ "这里的如 ******* 要全部删除" -> 去掉所有示例提示
    ④ "会次图在外面显示不全而且太小了" -> 放大图片
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
B = "----fillbnd"


def _multipart(fields, files=None):
    parts = []
    for k, v in fields.items():
        parts.append(
            ('--{}\r\nContent-Disposition: form-data; name="{}"\r\n\r\n{}'
             .format(B, k, v)).encode())
    for k, (fn, ct, data) in (files or {}).items():
        parts.append(
            ('--{}\r\nContent-Disposition: form-data; name="{}";'
             ' filename="{}"\r\nContent-Type: {}\r\n\r\n'
             .format(B, k, fn, ct)).encode() + data)
    body = b"\r\n".join(parts) + b"\r\n--" + B.encode() + b"--\r\n"
    return ({"content-type": "multipart/form-data; boundary={}".format(B)},
            body)


class TestEditFormFillsAllFields(unittest.TestCase):
    """编辑页必须把每个表单字段都回填。"""

    def setUp(self) -> None:
        from dddb.db import Store
        from dddb.public.site import build_site

        self.tmp = temp_path("fill-all")
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True)
        self.db = self.data / "dddb.sqlite3"
        self.covers = self.data / "covers"
        self.covers.mkdir()
        store = Store(self.db)
        store.save_series("Music Media-Mix Market", abbr="M3",
                          full_name="Music Media-Mix Market")
        store.save_series("Comic Market", abbr="CM",
                          full_name="Comic Market")
        store.save_event("M3-2026秋", series="Music Media-Mix Market",
                         vol=58, vol_label="M3-58",
                         year=2026, season="秋",
                         start_date="2026-09-20", tags=["东方"],
                         links={"官网": "https://m3.example.com/"})
        store.close()
        self.site = build_site(
            None, root=self.tmp, db_path=self.db,
            users_db=self.data / "u.sqlite3",
            sessions_db=self.data / "s.sqlite3", covers_dir=self.covers)

    def tearDown(self) -> None:
        import shutil

        self.site.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_values_include_all(self) -> None:
        vals = self.site._entity_form_values("event", "M3-2026秋")
        for key in ("name", "series", "vol_label", "year", "season",
                    "start_date", "links"):
            self.assertIn(key, vals, f"_entity_form_values 没给 {key}")
        self.assertEqual(vals["series"], "Music Media-Mix Market")
        self.assertEqual(vals["vol_label"], "M3-58")
        self.assertIn("官网", vals["links"], "links 没回填")

    def test_rendered_form_shows_values(self) -> None:
        from dddb.public import templates as T

        vals = self.site._entity_form_values("event", "M3-2026秋")
        html = T.edit_profile_page(
            T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                          user={"id": 1, "username": "e", "role": "editor"}),
            "event", "M3-2026秋", values=vals, back_url="/event/x",
            series_options=[{"name": "Music Media-Mix Market", "abbr": "M3"},
                            {"name": "Comic Market", "abbr": "CM"}],
            has_logo=False)
        self.assertIn("selected", html, "系列下拉没有选中当前值")
        self.assertIn("M3-58", html, "会次标签没回填")
        self.assertIn("m3.example.com", html, "外链没回填")


class TestFieldsActuallySave(unittest.TestCase):
    """走真实 HTTP 路径验证保存。"""

    def setUp(self) -> None:
        from dddb.db import Store
        from dddb.public.site import build_site

        self.tmp = temp_path("save-http")
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True)
        self.db = self.data / "dddb.sqlite3"
        self.covers = self.data / "covers"
        self.covers.mkdir()
        store = Store(self.db)
        store.save_series("Music Media-Mix Market", abbr="M3",
                          full_name="Music Media-Mix Market")
        store.save_series("Comic Market", abbr="CM",
                          full_name="Comic Market")
        store.save_event("M3-2026秋", series="Music Media-Mix Market",
                         vol=58, vol_label="M3-58")
        store.close()
        self.site = build_site(
            None, root=self.tmp, db_path=self.db,
            users_db=self.data / "u.sqlite3",
            sessions_db=self.data / "s.sqlite3", covers_dir=self.covers)

    def tearDown(self) -> None:
        import shutil

        if getattr(self, "_orig_check", None) is not None:
            self.site.sessions.signer.check = self._orig_check  # type: ignore
        self.site.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_series_label_links_saved(self) -> None:
        from dddb.db import Store
        from dddb.public import auth

        st = auth.AuthState()
        st.session = auth.Session(sid="t", user_id=1, username="e",
                                  role="editor")
        st.csrf_token = "tk"
        self.site.sessions.state = lambda c: st            # type: ignore
        self.site.same_origin = lambda *a, **k: (True, "")  # type: ignore
        signer = self.site.sessions.signer
        self._orig_check = signer.check
        signer.check = lambda b, g: True                    # type: ignore

        headers, body = _multipart({
            "csrf_token": "tk",
            "name": "M3-2026秋",
            "series": "Comic Market",
            "vol_label": "C999",
            "year": "2026", "season": "秋",
            "links": "新链接: https://new.example.com/",
        }, {"logo": ("l.png", "image/png", PNG)})
        resp = self.site.handle(
            "POST", "/event/M3-2026%E7%A7%8B/edit",
            headers={**headers, "host": "127.0.0.1:9",
                     "origin": "http://127.0.0.1:9"},
            body=body)
        self.assertLess(resp.status, 400)

        store = Store(self.db)
        got = next(e for e in store.list_events()
                   if e["name"] == "M3-2026秋")
        store.close()
        self.assertEqual(got["series"], "Comic Market", "系列没保存")
        self.assertEqual(got["vol_label"], "C999", "会次标签没保存")
        self.assertIn("新链接", got["links"] or {}, "外链没保存")


class TestTagsMerged(unittest.TestCase):
    """tags 与会次标签合并。"""

    def test_tags_field_gone(self) -> None:
        from dddb.public import templates as T

        names = [f[0] for f in T.NEW_ENTITY_SPECS["event"][1]]
        self.assertNotIn("tags", names, "tags 应与会次标签合并")
        self.assertIn("vol_label", names)

    def test_existing_tags_preserved_on_save(self) -> None:
        """表单已无 tags, 保存时不能把库里原有标签清空。"""
        from dddb.db import Store
        from dddb.public.site import build_site

        tmp = temp_path("tags-keep")
        data = tmp / "data"
        data.mkdir(parents=True)
        db = data / "dddb.sqlite3"
        covers = data / "covers"
        covers.mkdir()
        store = Store(db)
        store.save_series("Music Media-Mix Market", abbr="M3",
                          full_name="Music Media-Mix Market")
        store.save_event("M3-2026秋", series="Music Media-Mix Market",
                         vol=58, vol_label="M3-58", tags=["东方", "同人"])
        store.close()
        site = build_site(None, root=tmp, db_path=db,
                          users_db=data / "u.sqlite3",
                          sessions_db=data / "s.sqlite3",
                          covers_dir=covers)
        try:
            site.can_edit = lambda s: True           # type: ignore
            from dddb.public import auth

            st = auth.AuthState()
            st.session = auth.Session(sid="t", user_id=1, username="e",
                                      role="editor")
            site._do_entity_update("event", "M3-2026秋", {
                "name": ["M3-2026秋"],
                "series": ["Music Media-Mix Market"],
                "vol_label": ["M3-58"],
                "links": [""],
            }, st, [])
            store = Store(db)
            got = next(e for e in store.list_events()
                       if e["name"] == "M3-2026秋")
            store.close()
            self.assertIn("东方", got["tags"], "原有标签被清空了")
        finally:
            site.close()
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


class TestNoExampleHints(unittest.TestCase):
    """表单里不留「如 ...」示例提示。"""

    def _ctx(self):
        from dddb.public import templates as T

        return T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                             user={"id": 1, "username": "e", "role": "editor"})

    def test_event_form_no_hints(self) -> None:
        from dddb.public import templates as T

        html = T.new_entity_page(self._ctx(), "event")
        hints = re.findall(r'<span class="field-hint">([^<]*)</span>', html)
        self.assertEqual(hints, [], "会次表单还有示例提示: {}".format(hints))

    def test_no_ru_samples(self) -> None:
        from dddb.public import templates as T

        html = T.new_entity_page(self._ctx(), "event")
        for bad in ("如 M3-2026秋", "如 M3-58", "决定名称前缀"):
            self.assertNotIn(bad, html, f"还有提示 {bad}")

    def test_tag_form_no_note(self) -> None:
        from dddb.public import templates as T

        html = T.new_entity_page(self._ctx(), "tag")
        self.assertNotIn('name="note"', html, "标签表单还有「说明」")


class TestImagesEnlarged(unittest.TestCase):
    def test_card_logo_bigger(self) -> None:
        css = (ROOT / "dddb/public/static/public.css").read_text(
            encoding="utf-8")
        block = css.split(".series-card-logo {")[1].split("}")[0]
        height = int(re.search(r"height:\s*(\d+)px", block).group(1))
        self.assertGreaterEqual(height, 160,
                                "卡片图太小(原来 132)")

    def test_edition_thumb_bigger(self) -> None:
        css = (ROOT / "dddb/public/static/public.css").read_text(
            encoding="utf-8")
        block = css.split(".edition-thumb {")[1].split("}")[0]
        height = int(re.search(r"height:\s*(\d+)px", block).group(1))
        self.assertGreaterEqual(height, 130,
                                "会次缩略图太小(原来 96)")

    def test_hero_bigger(self) -> None:
        css = (ROOT / "dddb/public/static/public.css").read_text(
            encoding="utf-8")
        block = css.split(".series-hero {")[1].split("}")[0]
        m = re.search(r"max-height:\s*(\d+)px", block)
        self.assertGreaterEqual(int(m.group(1)), 400,
                                "hero 太小(原来 320)")

    def test_no_crop(self) -> None:
        """用 contain 不裁切(用户说"显示不全"是指尺寸)."""
        css = (ROOT / "dddb/public/static/public.css").read_text(
            encoding="utf-8")
        block = css.split(".series-card-logo {")[1].split("}")[0]
        self.assertIn("object-fit: contain", block)


if __name__ == "__main__":
    unittest.main()
