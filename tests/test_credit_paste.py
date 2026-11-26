"""制作人员整段粘贴(用户 m14663): 解析 / 合并 / 表单 / 端到端保存."""

from __future__ import annotations

import shutil
import unittest
import urllib.parse

from tests import _tmp

PASTE = """\
制作: Nika Lenz (https://x.com/nika)
作曲: Nika Lenz / kaza
插画: かにくみーとい （https://example.com/illust）
演唱: kaza
"""


class TestCreditParser(unittest.TestCase):
    """纯解析器(dddb/creditparse.py)."""

    def test_links_in_parens_are_dropped(self):
        from dddb import creditparse as C
        parsed = C.parse_credits(PASTE)
        names = {c.name for c in parsed}
        self.assertIn("Nika Lenz", names)
        self.assertIn("かにくみーとい", names)
        for name in names:
            self.assertNotIn("http", name)

    def test_role_before_last_colon(self):
        # instrument:Piano: 某丙 -> 职责 instrument:Piano(与 infobox 同约定)
        from dddb import creditparse as C
        parsed = C.parse_credits("instrument:Piano: 某丙")
        self.assertEqual([(c.role, c.name) for c in parsed],
                         [("instrument:Piano", "某丙")])

    def test_multiple_names_split(self):
        from dddb import creditparse as C
        parsed = C.parse_credits("作曲: 甲 / 乙、丙,丁；戊")
        self.assertEqual([c.name for c in parsed], ["甲", "乙", "丙", "丁", "戊"])

    def test_header_and_blank_are_skipped(self):
        from dddb import creditparse as C
        self.assertEqual(C.parse_credits(""), [])
        self.assertEqual(C.parse_credits("职责: 名字"), [])
        self.assertEqual(C.parse_credits("staff\ncredits\nrole name"), [])
        self.assertEqual(C.parse_credits("没有冒号的一行"), [])

    def test_bullet_prefixes_stripped(self):
        from dddb import creditparse as C
        parsed = C.parse_credits("- 制作: 甲\n1. 作曲: 乙\n· 编曲: 丙")
        self.assertEqual([(c.role, c.name) for c in parsed],
                         [("制作", "甲"), ("作曲", "乙"), ("编曲", "丙")])

    def test_duplicate_rows_collapse(self):
        from dddb import creditparse as C
        parsed = C.parse_credits("制作: 甲\n制作: 甲\n制作: 乙")
        self.assertEqual([(c.role, c.name) for c in parsed],
                         [("制作", "甲"), ("制作", "乙")])

    def test_strip_links_variants(self):
        from dddb import creditparse as C
        self.assertEqual(C.strip_links("甲 (https://x.com/a)"), "甲")
        self.assertEqual(C.strip_links("甲 （说明）"), "甲")
        self.assertEqual(C.strip_links("甲 https://x.com/a"), "甲")
        self.assertEqual(C.strip_links("甲 (a (b))"), "甲")

    def test_mode_normalization(self):
        from dddb import creditparse as C
        self.assertEqual(C.normalize_credit_mode("APPEND"), "append")
        self.assertEqual(C.normalize_credit_mode("垃圾"), "replace")
        self.assertEqual(C.normalize_credit_mode(None), "replace")

    def test_merge_replace_and_append(self):
        from dddb import creditparse as C
        base = [{"role": "mix", "name": "旧"}]
        parsed = C.parse_credits("制作: 甲")
        self.assertEqual(C.merge_pasted_credits(base, [], "replace"), base)
        self.assertEqual(C.merge_pasted_credits(base, parsed, "replace"),
                         [{"role": "制作", "name": "甲"}])
        self.assertEqual(C.merge_pasted_credits(base, parsed, "append"),
                         [{"role": "mix", "name": "旧"},
                          {"role": "制作", "name": "甲"}])

    def test_append_dedupes(self):
        from dddb import creditparse as C
        base = [{"role": "制作", "name": "甲"}]
        parsed = C.parse_credits("制作: 甲")
        self.assertEqual(C.merge_pasted_credits(base, parsed, "append"), base)

    def test_to_infobox_text(self):
        from dddb import creditparse as C
        text = C.to_infobox_text(C.parse_credits("制作: 甲\n作曲: 乙"))
        self.assertTrue(text.startswith("[dddb]\ncredits || "))
        self.assertIn("credits || 制作: 甲", text)
        # 能被 infobox 反向解析回来
        from dddb import infobox as IB
        draft = IB.parse(text)
        self.assertEqual([(c["role"], c["name"]) for c in draft["credits"]],
                         [("制作", "甲"), ("作曲", "乙")])


class TestMergeIntoPatch(unittest.TestCase):
    """服务端 patch 合并(不建站点)."""

    def test_missing_paste_is_noop(self):
        from dddb.public import site as S
        patch = {"credits": [{"role": "mix", "name": "旧"}]}
        self.assertEqual(S.merge_credits_from_paste(patch, {}), (False, False))
        self.assertEqual(patch["credits"], [{"role": "mix", "name": "旧"}])

    def test_unrecognized_keeps_existing(self):
        from dddb.public import site as S
        patch = {"credits": [{"role": "mix", "name": "旧"}]}
        has, missed = S.merge_credits_from_paste(
            patch, {"credit_paste": ["没有冒号的一段"]})
        self.assertEqual((has, missed), (True, True))
        self.assertEqual(patch["credits"], [{"role": "mix", "name": "旧"}])

    def test_replace_mode_overwrites(self):
        from dddb.public import site as S
        patch = {"credits": [{"role": "mix", "name": "旧"}]}
        has, missed = S.merge_credits_from_paste(
            patch, {"credit_paste": [PASTE], "credit_mode": ["replace"]})
        self.assertEqual((has, missed), (True, False))
        self.assertNotIn({"role": "mix", "name": "旧"}, patch["credits"])
        self.assertEqual(len(patch["credits"]), 5)

    def test_append_mode_keeps_existing(self):
        from dddb.public import site as S
        patch = {"credits": [{"role": "mix", "name": "旧"}]}
        S.merge_credits_from_paste(
            patch, {"credit_paste": [PASTE], "credit_mode": ["append"]})
        self.assertEqual(patch["credits"][0], {"role": "mix", "name": "旧"})
        self.assertEqual(len(patch["credits"]), 6)

    def test_links_never_reach_credits(self):
        from dddb.public import site as S
        patch: dict = {}
        S.merge_credits_from_paste(patch, {"credit_paste": [PASTE]})
        for item in patch["credits"]:
            self.assertNotIn("http", item["name"])


class TestPasteBlockRender(unittest.TestCase):
    """编辑页/新建页的粘贴区渲染."""

    @classmethod
    def setUpClass(cls):
        from dddb.public import templates as T
        cls.T = T
        cls.ctx = T.SiteContext(lang="zh", csrf_token="tk", is_editor=True)
        cls.album = {"key": "k", "title": "T", "credits": [], "tracks": [],
                     "urls": [], "artists": []}

    def render(self, **values):
        base = {"credits": [], "tracks": [], "urls": []}
        base.update(values)
        return self.T.edit_album_page(self.ctx, album=self.album, values=base)

    def test_block_present_with_own_stage_button(self):
        html = self.render()
        self.assertIn('class="trk-paste credit-paste"', html)
        self.assertIn('name="credit_paste"', html)
        self.assertIn('data-credit-apply="1"', html)
        self.assertIn("填入词条（暂存）", html)
        # 用户 m16094: 点完不直接提交, 只是先填进下面的词条
        self.assertNotIn("保存并填入词条", html)

    def test_button_never_submits_whole_form(self):
        html = self.render()
        start = html.index('class="trk-paste credit-paste"')
        end = html.index("</details>", start)
        block = html[start:end]
        self.assertIn('type="button"', block)
        self.assertNotIn('type="submit"', block)
        self.assertNotIn("formaction", block)
        self.assertLess(html.index('class="edit-form"'),
                        html.index('data-credit-apply="1"'))

    def test_hint_explains_link_dropping(self):
        html = self.render()
        self.assertIn("括号里的链接会被忽略", html)
        self.assertIn("点最下面的「保存」才会真正写进库里", html)

    def test_open_when_pasted(self):
        html = self.render(credit_paste=PASTE, credit_mode="append")
        self.assertIn('class="trk-paste credit-paste" open', html)
        self.assertIn('value="append" checked', html)
        self.assertIn("Nika Lenz", html)

    def test_closed_when_empty(self):
        html = self.render()
        self.assertNotIn('class="trk-paste credit-paste" open', html)
        self.assertIn('value="replace" checked', html)

    def test_new_album_page_also_has_it(self):
        html = self.T.edit_album_page(
            self.ctx, album={}, values={"credits": [], "tracks": [], "urls": []},
            creating=True, bind_options=None)
        self.assertIn('data-credit-apply="1"', html)
        self.assertIn('name="credit_paste"', html)


class TestFormRoundTrip(unittest.TestCase):
    """校验失败时要能原样回填粘贴内容."""

    def test_display_values_keep_paste(self):
        from dddb.public import site as S
        shown = S.form_display_values({
            "credit_paste": [PASTE], "credit_mode": ["append"],
            "credit_role": ["制作"], "credit_name": ["甲"],
        })
        self.assertEqual(shown["credit_paste"], PASTE)
        self.assertEqual(shown["credit_mode"], "append")

    def test_display_values_default_mode(self):
        from dddb.public import site as S
        shown = S.form_display_values({})
        self.assertEqual(shown["credit_paste"], "")
        self.assertEqual(shown["credit_mode"], "replace")


class _SiteCase(unittest.TestCase):
    """真起一个 PublicSite(临时库), 鉴权/CSRF 短路成编辑者."""

    def setUp(self):
        from dddb import db as db_mod
        from dddb.public import auth
        from dddb.public.site import build_site

        self.tmp = _tmp.temp_path("creditpaste")
        data = self.tmp / "data"
        data.mkdir(parents=True, exist_ok=True)
        self.db_path = data / "t.sqlite3"
        covers = data / "covers"
        covers.mkdir()
        db_mod.Store(self.db_path).close()
        self.site = build_site(
            None, root=self.tmp, db_path=self.db_path,
            users_db=data / "u.sqlite3", sessions_db=data / "s.sqlite3",
            covers_dir=covers)
        state = auth.AuthState()
        state.session = auth.Session(sid="t", user_id=1, username="e",
                                     role="editor")
        state.csrf_token = "tk"
        self.site.sessions.state = lambda cookies: state        # type: ignore
        self.site.same_origin = lambda *a, **k: (True, "")      # type: ignore
        self.site.sessions.signer.check = lambda b, g: True     # type: ignore
        self.site.can_edit = lambda s: True                     # type: ignore
        self.state = state

    def tearDown(self):
        self.site.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post_form(self, path, fields):
        body = urllib.parse.urlencode(fields, doseq=True).encode()
        return self.site.handle(
            "POST", path,
            headers={"content-type": "application/x-www-form-urlencoded",
                     "content-length": str(len(body)),
                     "host": "127.0.0.1:9", "origin": "http://127.0.0.1:9"},
            body=body)


class TestSaveEndToEnd(_SiteCase):
    """真发一次 POST, 确认词条落库."""

    def _create(self, extra=None):
        fields = [
            ("csrf_token", "tk"),
            ("title", "粘贴词条测试"),
            ("circle", "Test Circle"),
            ("release_date", "2026-01-01"),
            ("credit_role", "mix"), ("credit_name", "旧的混音"),
            ("url_key", "官网"), ("url_value", "https://example.com/x"),
        ]
        fields.extend(extra or [])
        return self.post_form("/new/album", fields)

    def test_new_album_merges_paste(self):
        resp = self._create([("credit_paste", PASTE),
                             ("credit_mode", "append")])
        self.assertEqual(resp.status, 303, resp.status)
        store = self.site.writable_store()
        try:
            rows = store.execute(
                "SELECT payload FROM albums ORDER BY rowid DESC LIMIT 1").fetchall()
        finally:
            store.close()
        import json
        payload = json.loads(rows[0]["payload"])
        credits = payload.get("credits") or []
        # 库里每条还带 source 等字段, 这里只比 role/name
        pairs = [(c.get("role"), c.get("name")) for c in credits]
        self.assertEqual(pairs[0], ("mix", "旧的混音"))
        self.assertIn(("制作", "Nika Lenz"), pairs)
        self.assertIn(("作曲", "kaza"), pairs)
        for _, name in pairs:
            self.assertNotIn("http", name)

    def test_unrecognized_paste_does_not_block_save(self):
        resp = self._create([("credit_paste", "完全识别不出的一段文字")])
        self.assertEqual(resp.status, 303, resp.status)
        store = self.site.writable_store()
        try:
            rows = store.execute(
                "SELECT payload FROM albums ORDER BY rowid DESC LIMIT 1").fetchall()
        finally:
            store.close()
        import json
        credits = json.loads(rows[0]["payload"]).get("credits") or []
        self.assertEqual([(c.get("role"), c.get("name")) for c in credits],
                         [("mix", "旧的混音")])


class TestCssAndVersion(unittest.TestCase):
    """粘贴区的样式复用 .trk-paste, 版本号已 bump."""

    def test_css_reuses_track_paste_block(self):
        from dddb.public import templates as T
        css = (T.__file__ and "public.css")
        root = __import__("pathlib").Path(T.__file__).resolve().parents[2]
        text = (root / "dddb" / "public" / "static" / css).read_text("utf-8")
        for token in (".trk-paste", ".trk-paste-acts", ".trk-paste-hint"):
            self.assertIn(token, text)

    def test_versions_bumped(self):
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parents[1]
        src = (root / "dddb" / "public" / "templates.py").read_text("utf-8")
        css = int(re.search(r"public\.css\?v=(\d+)", src).group(1))
        js = int(re.search(r"public\.js\?v=(\d+)", src).group(1))
        ib = int(re.search(r"infobox\.js\?v=(\d+)", src).group(1))
        self.assertGreaterEqual(css, 79)
        self.assertGreaterEqual(js, 67)
        self.assertGreaterEqual(ib, 9)


if __name__ == "__main__":
    unittest.main()
