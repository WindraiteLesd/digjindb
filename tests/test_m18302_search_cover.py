"""用户 m18302: 纯数字搜索不该被当成实体编号 / 封面放大 / 手机端曲目表撑满.

背景
----
用户搜 "2000"(想找一张叫 2000 的碟) 却被甩进另一张专辑的详情页 —— 因为
``page_albums`` 以前把**任何**纯数字都当 ``entities.id`` 解析。修法:
只有 ``#12`` / ``albumId:12`` 这种明确写法才直通编号; 纯数字照常搜索,
真的搜不到时才回退成编号查一次。

同一个 bug 报告里还提到「详情页封面不能放大」「编辑页放大镜被点按上传
顶掉」「手机端曲目表只用了左半边」, 这里一并盯住。
"""

from __future__ import annotations

import re
import shutil
import unittest
from pathlib import Path

from dddb.models import Album
from dddb.public import templates as T
from dddb.public import site as site_mod
from tests import _tmp

ROOT = Path(__file__).resolve().parents[1]


def _css() -> str:
    return (ROOT / "dddb" / "public" / "static" / "public.css").read_text(encoding="utf-8")


def _js() -> str:
    return (ROOT / "dddb" / "public" / "static" / "public.js").read_text(encoding="utf-8")


def _crop_js() -> str:
    return (ROOT / "dddb" / "public" / "static" / "cover-crop.js").read_text(encoding="utf-8")


def _tpl() -> str:
    return (ROOT / "dddb" / "public" / "templates.py").read_text(encoding="utf-8")


class TestEntityIdPrefix(unittest.TestCase):
    """``_looks_like_entity_id``: 只有带前缀才算「我要按编号查」。"""

    def test_explicit_prefixes(self) -> None:
        for value in ("#12", " #12 ", "#0", "albumId:12", "albumid:12",
                      "ALBUMID:7", "  albumId:12  "):
            with self.subTest(value=value):
                self.assertTrue(site_mod._looks_like_entity_id(value), value)

    def test_plain_numbers_are_searches(self) -> None:
        """用户 m18302 的核心诉求: 纯数字是搜索词, 不是编号。"""
        for value in ("2000", "12", " 2000 ", "", "   ", "2000年", "3:42",
                      "#", "#abc", "albumId:", "albumId:abc", "circleId:12"):
            with self.subTest(value=value):
                self.assertFalse(site_mod._looks_like_entity_id(value), value)

    def test_page_albums_does_not_hijack_plain_number(self) -> None:
        """源码层面盯住: page_albums 里那段直通必须挂在 explicit_id 上。"""
        text = (ROOT / "dddb" / "public" / "site.py").read_text(encoding="utf-8")
        start = text.find("def page_albums(")
        self.assertGreater(start, 0, "找不到 page_albums")
        end = text.find("\n    def ", start + 10)
        body = text[start:end if end > start else len(text)]
        self.assertIn("_looks_like_entity_id(raw_q)", body)
        self.assertIn("explicit_id", body)
        # 旧写法: 无条件 resolve_entity("album", raw_q)
        self.assertNotIn('resolve_entity("album", raw_q)', body,
                         "纯数字不能再被无条件当成实体编号")


class TestAlbumKeyFromEntityId(unittest.TestCase):
    """``#12`` -> 那张专辑; 输入不合法时返回空串而不是抛异常。"""

    def setUp(self) -> None:
        from dddb.db import Store
        from dddb.public.site import build_site

        self.tmp = _tmp.temp_path("m18302-id")
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True)
        self.db = self.data / "t.sqlite3"
        self.covers = self.data / "covers"
        self.covers.mkdir()
        store = Store(self.db)
        store.upsert_album(Album.from_dict({
            "key": "2000|someone|2026", "title": "2000",
            "circle": "someone", "release_date": "2026-01-01"}))
        store.close()
        self.site = build_site(
            None, root=self.tmp, db_path=self.db,
            users_db=self.data / "u.sqlite3",
            sessions_db=self.data / "s.sqlite3",
            covers_dir=self.covers)
        from dddb.db import Store as S2
        probe = S2(self.db)
        try:
            self.entity_id = probe.ensure_entity("album", "2000|someone|2026")
        finally:
            probe.close()

    def tearDown(self) -> None:
        self.site.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hash_prefix_resolves(self) -> None:
        got = self.site._album_key_from_entity_id("#{}".format(self.entity_id))
        self.assertEqual(got, "2000|someone|2026")

    def test_albumid_prefix_resolves(self) -> None:
        got = self.site._album_key_from_entity_id(
            "albumId:{}".format(self.entity_id))
        self.assertEqual(got, "2000|someone|2026")

    def test_bad_input_returns_empty(self) -> None:
        for value in ("", "   ", "#", "#abc", "2000", "albumId:",
                      "albumId:abc", "circleId:1", "#0", "#-3"):
            with self.subTest(value=value):
                self.assertEqual(self.site._album_key_from_entity_id(value), "")

    def test_missing_entity_returns_empty(self) -> None:
        self.assertEqual(self.site._album_key_from_entity_id("#999999"), "")


class TestSearchShowsResultPage(unittest.TestCase):
    """搜 "2000" 应该看到搜索结果页(命中那张真叫 2000 的碟)。"""

    def setUp(self) -> None:
        from dddb.db import Store
        from dddb.public.site import build_site

        self.tmp = _tmp.temp_path("m18302-page")
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True)
        self.db = self.data / "t.sqlite3"
        self.covers = self.data / "covers"
        self.covers.mkdir()
        store = Store(self.db)
        store.upsert_album(Album.from_dict({
            "key": "2000|someone|2026", "title": "2000",
            "circle": "someone", "release_date": "2026-01-01"}))
        store.upsert_album(Album.from_dict({
            "key": "other|someone|2026", "title": "别的碟",
            "circle": "someone", "release_date": "2026-01-01"}))
        store.close()
        self.site = build_site(
            None, root=self.tmp, db_path=self.db,
            users_db=self.data / "u.sqlite3",
            sessions_db=self.data / "s.sqlite3",
            covers_dir=self.covers)

    def tearDown(self) -> None:
        self.site.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_plain_number_renders_list_not_detail(self) -> None:
        html = self.site.page_albums({"q": ["2000"]})
        self.assertIn("搜索", html, "应该渲染搜索结果页")
        self.assertIn("2000", html)
        # 详情页独有的东西不该出现
        self.assertNotIn('class="detail-wrap"', html)

    def test_number_with_no_hits_falls_back_to_entity_id(self) -> None:
        """一条都没搜到时才回退成编号 —— 保持老用户的习惯。"""
        from dddb.db import Store
        probe = Store(self.db)
        try:
            entity_id = probe.ensure_entity("album", "2000|someone|2026")
        finally:
            probe.close()
        # 搜一个绝对搜不到、又不是该专辑编号的数字
        html = self.site.page_albums({"q": [str(entity_id + 500000)]})
        self.assertIn("搜索", html)
        # 而真的编号还是能直达详情页
        html2 = self.site.page_albums({"q": ["#{}".format(entity_id)]})
        self.assertIn('class="detail-wrap"', html2, "#N 应该直达详情页")


class TestCoverLightbox(unittest.TestCase):
    """封面放大: 详情页点图能看原图, 编辑页放大镜不被「点图换封面」顶掉。"""

    def test_detail_page_has_lightbox_wiring(self) -> None:
        js = _js()
        self.assertIn("function openCoverLightbox(", js)
        self.assertIn("function setupDetailLightbox(", js)
        self.assertIn("setupDetailLightbox();", js, "boot 里要注册")
        self.assertIn("cover-lightbox", js)
        # 详情页那份要挂到 .detail-cover 上
        self.assertIn('document.querySelector(".detail-cover")', js)

    def test_lightbox_closes_on_escape_and_backdrop(self) -> None:
        js = _js()
        start = js.find("function openCoverLightbox(")
        self.assertGreater(start, 0)
        body = js[start:js.find("\n  }", start) + 4]
        self.assertIn('"Escape"', body)
        self.assertIn("removeEventListener", body)
        self.assertIn("box.remove()", body)

    def test_zoom_button_is_clickable_not_hover_only(self) -> None:
        """以前 opacity: 0 只在 hover 露面, 触屏永远点不到。"""
        css = _css()
        rule = re.search(r"\.edit-cover-zoom \{([^}]*)\}", css)
        self.assertIsNotNone(rule)
        body = rule.group(1)
        self.assertNotIn("opacity: 0;", body, "放大按钮不能默认完全透明")
        self.assertIn("z-index", body)
        self.assertIn(".edit-cover-zoom:focus-visible", css)

    def test_crop_js_lets_zoom_through(self) -> None:
        """点「放大」不能被「点图换封面」的裁切弹窗截走。"""
        text = _crop_js()
        self.assertIn("[data-cover-zoom]", text,
                      "cover-crop.js 里点图换封面要放过放大按钮")


class TestMobileTrackListFillsWidth(unittest.TestCase):
    """手机端曲目表展开区必须占满宽度(用户 m18302: 只用左半边)。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.css = _css()

    def _mobile_block(self) -> str:
        anchors = [m.start() for m in re.finditer(
            r"@media \(max-width: 820px\)", self.css)]
        for start in anchors:
            end = self.css.find("\n}", start)
            chunk = self.css[start:end if end > start else len(self.css)]
            if "track-extra-row" in chunk:
                return chunk
        self.fail("找不到手机端曲目表那段 @media")

    def test_extra_row_is_block_and_full_width(self) -> None:
        chunk = self._mobile_block()
        rule = re.search(r"\.tracklist tr\.track-extra-row \{([^}]*)\}", chunk)
        self.assertIsNotNone(rule)
        body = rule.group(1)
        self.assertIn("display: block", body)
        self.assertIn("width: 100%", body)

    def test_extra_cell_is_full_width(self) -> None:
        chunk = self._mobile_block()
        rule = re.search(
            r"\.tracklist tr\.track-extra-row > td \{([^}]*)\}", chunk)
        self.assertIsNotNone(rule)
        body = rule.group(1)
        self.assertIn("display: block", body)
        self.assertIn("width: 100%", body)
        self.assertIn("box-sizing: border-box", body)

    def test_inner_flex_container_fills_cell(self) -> None:
        chunk = self._mobile_block()
        self.assertIn(
            ".tracklist tr.track-extra-row > td > .track-extra { width: 100%; }",
            chunk)


class TestCssIsBalanced(unittest.TestCase):
    """注释/字符串感知的花括号配平 —— 多余 `}` 会把后面所有规则算错层级."""

    def test_no_stray_braces(self) -> None:
        text = _css()
        depth = 0
        stray = []
        i = 0
        line = 1
        size = len(text)
        while i < size:
            ch = text[i]
            if ch == "\n":
                line += 1
                i += 1
                continue
            if ch == "/" and i + 1 < size and text[i + 1] == "*":
                end = text.find("*/", i + 2)
                if end < 0:
                    break
                line += text.count("\n", i, end)
                i = end + 2
                continue
            if ch in "\"'":
                quote = ch
                j = i + 1
                while j < size:
                    if text[j] == "\\":
                        j += 2
                        continue
                    if text[j] == quote:
                        break
                    j += 1
                line += text.count("\n", i, j)
                i = j + 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0:
                    stray.append(line)
                    depth = 0
            i += 1
        self.assertEqual(stray, [], "public.css 有多余的 } 在第 " + repr(stray))
        self.assertEqual(depth, 0, "public.css 有未闭合的 {")


class TestVersionsBumped(unittest.TestCase):
    def _version(self, name: str) -> int:
        text = _tpl()
        match = re.search(re.escape(name) + r"\?v=(\d+)", text)
        self.assertIsNotNone(match, name)
        return int(match.group(1))

    def test_assets_not_downgraded(self) -> None:
        self.assertGreaterEqual(self._version("public.css"), 85)
        self.assertGreaterEqual(self._version("public.js"), 74)
        self.assertGreaterEqual(self._version("cover-crop.js"), 8)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
