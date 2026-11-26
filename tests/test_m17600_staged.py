"""用户 m17600 的三件事.

1. 粘贴框里若是 infobox 全文(带 ``tracks ||`` 前缀), 要先剥成纯曲目表;
2. 点过「填入曲目表（暂存）」之后, 用户在下面表格里改过的东西**不能**
   被存储时重新解析粘贴原文盖回去(``track_staged`` 标记);
3. 外链名识别要认识 Geek Jack / TANO*C Store。

另外顺手锁住手机端曲目展开区不再被压成一列单字(同一批反馈里的截图)。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from dddb import creditparse, trackparse
from dddb.public import linknames, site, templates as T

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_JS = ROOT / "dddb" / "public" / "static" / "public.js"
PUBLIC_CSS = ROOT / "dddb" / "public" / "static" / "public.css"

INFOBOX_PASTE = """[dddb]
title || T
circle || C
disc || 1
tracks || 1. A [3:00] / X
tracks || 2. B [4:00] / Y
disc || 2: Nightmare
tracks || 1. C [2:00]
credits || produce: P
"""


def _ctx() -> T.SiteContext:
    return T.SiteContext(lang="zh", csrf_token="tk", is_editor=True)


def _base_values(**extra):
    values = {
        "credits": [],
        "tracks": [],
        "urls": [],
        "discs": {},
        "title_original": "T",
    }
    values.update(extra)
    return values


class TestStripInfoboxTracks(unittest.TestCase):
    """1. infobox 全文 -> 纯曲目表."""

    def test_plain_text_passes_through_untouched(self) -> None:
        text = "01. 曲名 - 艺人 4:12\n02. 曲名 / 艺人"
        self.assertEqual(trackparse.strip_infobox_tracks(text), text)

    def test_only_tracks_and_disc_lines_survive(self) -> None:
        out = trackparse.strip_infobox_tracks(INFOBOX_PASTE)
        self.assertEqual(
            out,
            "Disc 1\n1. A [3:00] / X\n2. B [4:00] / Y\n"
            "Disc 2\n1. C [2:00]",
        )
        # 其它字段一行都不许留
        for word in ("title ||", "circle ||", "credits ||", "[dddb]"):
            self.assertNotIn(word, out)

    def test_continuation_lines_are_joined(self) -> None:
        text = "tracks || 1. A [3:00]\n|| / X\n|| / Y\n"
        self.assertEqual(trackparse.strip_infobox_tracks(text),
                         "1. A [3:00] / X / Y")

    def test_parse_full_infobox_keeps_discs(self) -> None:
        tracks = trackparse.parse_tracklist(INFOBOX_PASTE)
        self.assertEqual([t.disc for t in tracks], [1, 1, 2])
        self.assertEqual([t.title for t in tracks], ["A", "B", "C"])
        self.assertEqual(tracks[0].artists, ["X"])
        self.assertEqual(tracks[0].duration, 180)

    def test_case_insensitive_prefix(self) -> None:
        out = trackparse.strip_infobox_tracks("TRACKS || 1. A\n")
        self.assertEqual(out, "1. A")


class TestStagedFlag(unittest.TestCase):
    """2. track_staged 标记: 打了就不再解析粘贴原文."""

    def test_helper_reads_the_hidden_field(self) -> None:
        self.assertTrue(site.tracks_already_staged({"track_staged": ["1"]}))
        self.assertFalse(site.tracks_already_staged({"track_staged": [""]}))
        self.assertFalse(site.tracks_already_staged({"track_staged": ["0"]}))
        self.assertFalse(site.tracks_already_staged({}))

    def test_form_display_values_keeps_it(self) -> None:
        values = {"track_paste": ["01. A 3:00"], "track_staged": ["1"]}
        shown = site.form_display_values(values)
        self.assertEqual(shown.get("track_staged"), "1")
        self.assertEqual(site.form_display_values({}).get("track_staged"), "")

    def test_template_renders_the_hidden_field(self) -> None:
        for staged in ("", "1"):
            page = T.edit_album_page(
                _ctx(), album={"key": "k", "title": "T"},
                values=_base_values(track_paste="01. A 3:00", track_staged=staged))
            self.assertIn('name="track_staged" value="{}"'.format(staged), page)
            self.assertEqual(page.count('name="track_staged"'), 1)

    def test_new_album_page_has_it_too(self) -> None:
        page = T.edit_album_page(_ctx(), album={"key": "", "title": ""},
                                 values=_base_values(), creating=True)
        self.assertIn('name="track_staged"', page)

    def test_edit_path_skips_paste_when_staged(self) -> None:
        src = (ROOT / "dddb" / "public" / "site.py").read_text(encoding="utf-8")
        start = src.index("        paste_text = raw_field(values, \"track_paste\")")
        block = src[start:start + 900]
        self.assertIn("staged = tracks_already_staged(values)", block)
        self.assertIn("has_paste and not staged", block)
        # 新建页那条链路也要挡
        new_block = src[src.index("paste_text = raw_field(values, \"track_paste\")",
                                  start + 900):]
        self.assertIn("not tracks_already_staged(values)", new_block[:400])

    def test_frontend_sets_the_flag_after_filling(self) -> None:
        js = PUBLIC_JS.read_text(encoding="utf-8")
        self.assertIn("function setupPasteFill(", js)
        self.assertIn("input[name=\"track_staged\"]", js)
        idx = js.index("input[name=\"track_staged\"]")
        self.assertIn('stagedField.value = "1"', js[idx:idx + 200])


class TestLinkNameRules(unittest.TestCase):
    """3. Geek Jack / TANO*C Store 识别."""

    def test_geek_jack(self) -> None:
        self.assertEqual(
            linknames.guess_link_name("https://shop.geekjack.net/products/x"),
            "Geek Jack")
        self.assertEqual(
            linknames.guess_link_name("https://www.geekjack.net/x"), "Geek Jack")

    def test_tanoc_store(self) -> None:
        self.assertEqual(
            linknames.guess_link_name("https://www.tanocstore.net/items/1"),
            "TANO*C Store")
        self.assertEqual(
            linknames.guess_link_name("https://tanocstore.net/x"),
            "TANO*C Store")

    def test_others_unaffected(self) -> None:
        self.assertEqual(linknames.guess_link_name("https://example.com/x"), "")
        self.assertEqual(linknames.guess_link_name("https://x.booth.pm/items/1"),
                         "BOOTH")

    def test_names_show_up_in_the_datalist(self) -> None:
        names = linknames.name_choices()
        self.assertIn("Geek Jack", names)
        self.assertIn("TANO*C Store", names)

    def test_frontend_rules_mirror_the_python_ones(self) -> None:
        js = PUBLIC_JS.read_text(encoding="utf-8")
        self.assertIn("shop.geekjack.net", js)
        self.assertIn("tanocstore.net", js)
        self.assertIn("Geek Jack", js)
        self.assertIn("TANO*C Store", js)


class TestMobileExtraRow(unittest.TestCase):
    """手机端展开区不许被压成一列单字(用户 m17600 的截图)."""

    def test_value_has_its_own_wrapper(self) -> None:
        page = T.album_detail_page(
            _ctx(),
            album={"key": "k", "title": "T", "tracks": [{
                "index": 1, "disc": 1, "title": "A", "artists": ["X"],
                "title_zh": "翻译歌名很长很长的一句话", "duration_text": "3:00",
            }]},
            can_edit=False)
        self.assertIn('class="track-extra-value"', page)

    def test_mobile_rule_is_block_not_table_row(self) -> None:
        css = PUBLIC_CSS.read_text(encoding="utf-8")
        mobile = css[css.index("@media (max-width: 820px) {"):]
        mobile = mobile[:mobile.index("@media (max-width: 420px) {")]
        rule = re.search(r"\.tracklist tr\.track-extra-row \{([^}]*)\}", mobile)
        self.assertIsNotNone(rule)
        self.assertIn("display: block", rule.group(1))
        self.assertNotIn("table-row", rule.group(1))

    def test_value_wraps(self) -> None:
        css = PUBLIC_CSS.read_text(encoding="utf-8")
        rule = re.search(r"\.track-extra-value \{([^}]*)\}", css)
        self.assertIsNotNone(rule)
        self.assertIn("overflow-wrap", rule.group(1))


class TestSaveDoesNotOverwriteStagedTracks(unittest.TestCase):
    """真发一次 POST: 打了标记就以表格里那份为准(用户 m17600)."""

    PASTE = "01. 粘贴来的歌 1:00\n02. 也是粘贴来的 2:00"

    def setUp(self) -> None:
        from tests import _tmp
        import shutil

        from dddb import db as db_mod
        from dddb.public import auth
        from dddb.public.site import build_site

        self._shutil = shutil
        self.tmp = _tmp.temp_path("m17600")
        data = self.tmp / "data"
        data.mkdir(parents=True, exist_ok=True)
        covers = data / "covers"
        covers.mkdir()
        db_mod.Store(data / "t.sqlite3").close()
        self.site = build_site(
            None, root=self.tmp, db_path=data / "t.sqlite3",
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

    def tearDown(self) -> None:
        self.site.close()
        self._shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, path, fields):
        import urllib.parse

        body = urllib.parse.urlencode(fields, doseq=True).encode()
        return self.site.handle(
            "POST", path,
            headers={"content-type": "application/x-www-form-urlencoded",
                     "content-length": str(len(body)),
                     "host": "127.0.0.1:9", "origin": "http://127.0.0.1:9"},
            body=body)

    def _create(self, extra=()):
        fields = [
            ("csrf_token", "tk"),
            ("title", "暂存标记测试"),
            ("circle", "Test Circle"),
            ("release_date", "2026-02-02"),
            ("track_index", "1"), ("track_title", "表格里的标题"),
            ("track_disc", "1"), ("track_artists", "某人"),
        ]
        fields.extend(extra)
        return self._post("/new/album", fields)

    def _titles(self):
        import json

        store = self.site.writable_store()
        try:
            row = store.execute(
                "SELECT payload FROM albums ORDER BY rowid DESC LIMIT 1").fetchone()
        finally:
            store.close()
        return [t.get("title") for t in json.loads(row["payload"]).get("tracks") or []]

    def test_staged_keeps_the_table(self) -> None:
        resp = self._create([
            ("track_paste", self.PASTE),
            ("track_mode", "replace"),
            ("track_staged", "1"),
        ])
        self.assertEqual(resp.status, 303, resp.status)
        self.assertEqual(self._titles(), ["表格里的标题"])

    def test_without_the_flag_the_paste_wins(self) -> None:
        resp = self._create([
            ("track_paste", self.PASTE),
            ("track_mode", "replace"),
        ])
        self.assertEqual(resp.status, 303, resp.status)
        self.assertEqual(self._titles(), ["粘贴来的歌", "也是粘贴来的"])


class TestVersionsBumped(unittest.TestCase):
    def _version(self, name: str) -> int:
        src = (ROOT / "dddb" / "public" / "templates.py").read_text(encoding="utf-8")
        match = re.search(re.escape(name) + r"\?v=(\d+)", src)
        self.assertIsNotNone(match, name)
        return int(match.group(1))

    def test_assets_are_fresh(self) -> None:
        self.assertGreaterEqual(self._version("public.js"), 72)
        self.assertGreaterEqual(self._version("public.css"), 83)


if __name__ == "__main__":
    unittest.main()
