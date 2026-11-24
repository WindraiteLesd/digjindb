"""用户 m13077 提的五项问题的回归测试.

1. 简介换行被吃掉(存库时 ``\\s+`` 压成空格)
2. 制作人员职责顺序: 制作 > 艺人 > 作曲 > 编曲 > 演唱 > 作词
3. 外链名按域名自动识别(含两种 Embed Link), 同时保留下拉候选与自定义
4. 外链在新标签页打开
5. 编辑历史对比(曲目/外链/词条/简介等)改得出来
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from dddb.public import linknames
from dddb.db import Store
from dddb.models import Album
from dddb.public import site as S
from dddb.public import templates as T
from tests._tmp import temp_dir

ROOT = Path(__file__).resolve().parents[1]


def _album(key: str = "k1", **kw):
    base = {"key": key, "title": "标题", "circle": "社团", "release_date": "2026-01-01"}
    base.update(kw)
    return Album.from_dict(base)


class TestDescriptionKeepsNewlines(unittest.TestCase):
    """① 简介是多行字段: 换行不能被 ``\\s+``→空格 的清洗吃掉."""

    def test_model_keeps_paragraphs(self) -> None:
        album = _album(description="第一段\n\n第二段\n第三行")
        self.assertEqual(album.description, "第一段\n\n第二段\n第三行")

    def test_blank_runs_collapse_to_one(self) -> None:
        album = _album(description="甲\n\n\n\n乙")
        self.assertEqual(album.description, "甲\n\n乙")

    def test_trailing_spaces_and_crlf(self) -> None:
        album = _album(description="甲  \r\n\r\n\t乙  ")
        self.assertEqual(album.description, "甲\n\n乙")

    def test_single_line_unchanged(self) -> None:
        album = _album(description="  只有  一行  ")
        self.assertEqual(album.description, "只有 一行")

    def test_store_roundtrip(self) -> None:
        ctx = temp_dir("desc-")
        root = ctx.__enter__()
        try:
            store = Store(root / "d.sqlite3")
            try:
                store.upsert_album(_album(description="第一段\n\n第二段"))
                self.assertEqual(store.get_album("k1").description, "第一段\n\n第二段")
                store.update_album_fields("k1", {"description": "甲\n\n乙\n\n丙"})
                self.assertEqual(store.get_album("k1").description, "甲\n\n乙\n\n丙")
            finally:
                store.close()
        finally:
            ctx.__exit__(None, None, None)

    def test_multiline_helper_is_scoped_to_album(self) -> None:
        """多行清洗只给专辑简介用, 社团/其它字段仍走压空白."""
        src = (ROOT / "dddb/models.py").read_text(encoding="utf-8")
        self.assertIn("def _clean_multiline(", src)
        self.assertEqual(src.count("_clean_multiline("), 3,
                         "只该用在 helper 定义 + Album 两处")


class TestRoleOrder(unittest.TestCase):
    """② 制作排第一, 其次艺人/作曲/编曲/演唱/作词."""

    #: circle / label 是详情页的**分组标题**(社团 / 厂牌), 排在个人职责之前是既有约定
    EXPECTED_HEAD = ("produce", "artist", "compose", "arrange", "vocal", "lyric")

    def _person_roles(self) -> tuple[str, ...]:
        return tuple(r for r in T.ROLE_ORDER if r not in ("circle", "label"))

    def test_person_roles_start_with_produce(self) -> None:
        self.assertEqual(self._person_roles()[: len(self.EXPECTED_HEAD)],
                         self.EXPECTED_HEAD)

    def test_group_headers_stay_first(self) -> None:
        """详情页「社团 / 厂牌」两组仍在最前(它们不是某人的职责)."""
        self.assertEqual(T.ROLE_ORDER[:2], ("circle", "label"))

    def test_sort_role_agrees(self) -> None:
        indexes = [T.sort_role(role)[0] for role in self.EXPECTED_HEAD]
        self.assertEqual(indexes, sorted(indexes))
        self.assertLess(T.sort_role("produce"), T.sort_role("artist"))
        self.assertLess(T.sort_role("arrange"), T.sort_role("vocal"))

    def test_labels(self) -> None:
        self.assertEqual(T.role_label("produce"), "制作")
        self.assertEqual(T.role_label("artist"), "艺人")
        self.assertEqual(T.role_label("compose"), "作曲")
        self.assertEqual(T.role_label("arrange"), "编曲")
        self.assertEqual(T.role_label("vocal"), "演唱")
        self.assertEqual(T.role_label("lyric"), "作词")

    def test_edit_chip_puts_produce_first(self) -> None:
        album = _album(credits=[
            {"role": "vocal", "name": "甲"},
            {"role": "produce", "name": "甲"},
            {"role": "arrange", "name": "甲"},
        ])
        html = T.edit_album_page(_ctx(), album=album,
                                 values=T.edit_values_from_album(album))
        chip = re.findall(r'<span class="person-kind">([^<]*)</span><b>([^<]*)</b>', html)
        pairs = {name: kind for kind, name in chip}
        self.assertEqual(pairs.get("甲"), "制作/编曲/演唱")


class TestLinkNameGuess(unittest.TestCase):
    """③ 域名 → 标准外链名."""

    CASES = {
        "https://music.apple.com/jp/album/1": "Apple Music",
        "https://soundcloud.com/artist/track": "SoundCloud",
        "https://on.soundcloud.com/abc": "SoundCloud",
        "https://open.spotify.com/album/1": "Spotify",
        "https://play.spotify.com/album/1": "Spotify",
        "https://x.com/someone": "X (Twitter)",
        "https://twitter.com/someone": "X (Twitter)",
        "https://www.youtube.com/watch?v=1": "YouTube",
        "https://youtu.be/abc": "YouTube",
        "https://artist.bandcamp.com/album/x": "Bandcamp",
        "https://shop.melonbooks.co.jp/x": "Melonbooks",
        "https://linktr.ee/x": "linktr.ee",
    }

    def test_known_hosts(self) -> None:
        for url, expected in self.CASES.items():
            with self.subTest(url=url):
                self.assertEqual(linknames.guess_link_name(url), expected)

    def test_embed_codes(self) -> None:
        sc = '<iframe width="100%" src="https://w.soundcloud.com/player/?url=x"></iframe>'
        yt = '<iframe src="https://www.youtube.com/embed/abc"></iframe>'
        self.assertEqual(linknames.guess_link_name(sc), "Embed Link (SoundCloud)")
        self.assertEqual(linknames.guess_link_name(yt), "Embed Link (Youtube)")

    def test_unknown_and_empty(self) -> None:
        for value in ("", "   ", "https://example.com/x", "不是网址"):
            with self.subTest(value=value):
                self.assertEqual(linknames.guess_link_name(value), "")

    def test_choices_keep_both_embeds(self) -> None:
        choices = linknames.name_choices()
        self.assertIn("Embed Link (Youtube)", choices)
        self.assertIn("Embed Link (SoundCloud)", choices)
        self.assertIn("Apple Music", choices)
        self.assertEqual(len(choices), len(set(choices)), "候选有重复")

    def test_form_offers_datalist_and_free_text(self) -> None:
        album = _album(urls={"x": {"url": "https://x.com/a", "position": 1}})
        html = T.edit_album_page(_ctx(), album=album,
                                 values=T.edit_values_from_album(album))
        self.assertIn('<datalist id="dl-url_key">', html)
        self.assertIn('list="dl-url_key"', html)
        # 仍然是普通文本输入框, 可以自定义名字
        self.assertRegex(html, r'name="url_key"[^>]*')
        self.assertNotIn('<select name="url_key"', html)

    def test_js_fills_only_when_empty(self) -> None:
        js = (ROOT / "dddb/public/static/public.js").read_text(encoding="utf-8")
        self.assertIn("function setupLinkAutoName(", js)
        self.assertIn("setupLinkAutoName();", js)
        block = _fn(js, "setupLinkAutoName")
        self.assertIn('getAttribute("name") !== "url_value"', block)
        self.assertIn("if (String(keyInput.value || \"\").trim()) { return; }", block)
        self.assertIn("Embed Link (SoundCloud)", block)
        self.assertIn("Embed Link (Youtube)", block)


class TestExternalLinksNewTab(unittest.TestCase):
    """④ 站外链接要在新标签页打开."""

    def test_all_pills_open_in_new_tab(self) -> None:
        src = (ROOT / "dddb/public/templates.py").read_text(encoding="utf-8")
        self.assertEqual(src.count('class="link-pill"'), 4, "外链落点数量变了")
        blocks = re.findall(r'class="link-pill".{0,220}?rel="[^"]*"', src, re.S)
        self.assertEqual(len(blocks), 4)
        for block in blocks:
            with self.subTest(block=block[:60]):
                self.assertIn('target="_blank"', block)
                self.assertIn("noopener", block)
                self.assertIn("noreferrer", block)

    def test_album_detail_renders_target(self) -> None:
        html = _detail()
        pills = re.findall(r'<a class="link-pill"[^>]*>', html)
        self.assertGreater(len(pills), 0)
        for pill in pills:
            self.assertIn('target="_blank"', pill)


class TestHistoryDiff(unittest.TestCase):
    """⑤ 编辑历史对比: 字段白名单必须是 Album 真有的键, 且两边同形状."""

    def test_fields_are_real_album_keys(self) -> None:
        keys = set(_album().to_dict().keys())
        for field in S._HISTORY_DIFF_FIELDS:
            with self.subTest(field=field):
                self.assertIn(field, keys, f"{field} 不是 Album 的字段")

    def test_tracks_not_track_list(self) -> None:
        self.assertIn("tracks", S._HISTORY_DIFF_FIELDS)
        self.assertNotIn("track_list", S._HISTORY_DIFF_FIELDS)

    def test_detects_each_change(self) -> None:
        ctx = temp_dir("hist-")
        root = ctx.__enter__()
        try:
            store = Store(root / "h.sqlite3")
            try:
                store.upsert_album(_album(
                    description="一段", urls={"x": "https://x.com/a"},
                    tracks=[{"index": 1, "title": "A", "artists": ["P"]}],
                    credits=[{"role": "produce", "name": "P"}],
                ))
                base = store.get_album("k1").to_dict()

                def changed(patch):
                    store.update_album_fields("k1", patch)
                    now = store.get_album("k1").to_dict()
                    return [f for f in S._HISTORY_DIFF_FIELDS
                            if S._history_value(f, base.get(f))
                            != S._history_value(f, now.get(f))]

                self.assertEqual(changed({}), [])
                self.assertIn("tracks", changed(
                    {"track_list": [{"index": 1, "title": "B", "artists": ["P"]}]}))
                self.assertIn("urls", changed(
                    {"urls": {"x": "https://x.com/a", "y": "https://youtu.be/z"}}))
                self.assertIn("description", changed({"description": "一段\n\n二段"}))
            finally:
                store.close()
        finally:
            ctx.__exit__(None, None, None)

    def test_urls_normalised_both_shapes(self) -> None:
        as_dict = {"x": "https://x.com/a"}
        as_list = [{"name": "x", "url": "https://x.com/a", "href": "/link/k/1"}]
        self.assertEqual(S._history_value("urls", as_dict),
                         S._history_value("urls", as_list))

    def test_page_compares_album_to_album(self) -> None:
        src = (ROOT / "dddb/public/site.py").read_text(encoding="utf-8")
        block = src[src.index("def page_history"):]
        block = block[: block.index("\n    def _do_album_revert")]
        self.assertIn("store.get_album(key)", block)
        self.assertIn("_HISTORY_DIFF_FIELDS", block)
        # 不能再用视图跟快照比
        self.assertNotIn("current = album.to_dict()", block)

    def test_short_renders_structures(self) -> None:
        self.assertEqual(T._short({"x": "https://x.com/a"}), "x: https://x.com/a")
        self.assertEqual(T._short([{"index": 1, "title": "A"}]), "1. A")
        self.assertEqual(T._short([{"role": "produce", "name": "P"}]), "produce: P")


def _ctx():
    """构造一个最小 SiteContext(访客/编辑者都行, 这里给编辑者以便渲染全部控件)."""
    return T.SiteContext(lang="zh", csrf_token="t", is_editor=True)


def _detail():
    album = {
        "key": "k1", "title": "标题", "circle": "社团", "date": "2026-01-01",
        "urls": [{"name": "x", "url": "https://x.com/a", "href": "/link/k/1",
                  "position": 1}],
        "tracks": [], "credits": [], "photos": [], "tags": [], "genres": [],
        "artists": [], "description": "",
    }
    return T.album_detail_page(_ctx(), album=album)


def _fn(js: str, name: str) -> str:
    start = js.index("function " + name)
    depth = 0
    for index in range(js.index("{", start), len(js)):
        char = js[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return js[start: index + 1]
    return js[start:]


if __name__ == "__main__":
    unittest.main()