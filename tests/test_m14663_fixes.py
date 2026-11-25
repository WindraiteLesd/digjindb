"""测试: 用户 m14663 报的三个 bug.

① 「＋ 添加外链」按钮点击后看起来完全失效。
   根因: `<datalist id="dl-url_key">` 夹在 `.rows` 与 `.pair-add` 之间, 而
   `setupPairRows()` 找容器兜底时写成了
       container = prev.classList && prev.classList.contains("rows") ? prev : prev;
   —— 三元两边相同, 等于无条件取前一个兄弟。于是新行被 appendChild 进
   `<datalist>`(浏览器不渲染), 按钮看起来没反应。
   同时动态新建的外链行没带 `list="dl-url_key"`, 候选下拉也丢了。

② 新建页封面加号歪在左上角。
   根因: `.edit-cover-row .edit-cover-img { display: block }` 特异性(0,2,0)
   压掉了 `.cover-fallback { display: grid; place-items: center }`(0,1,0) 与
   `.cover-add-thumb { display: flex; align-items: center }`。

③ infobox 的封面不会自动缓存。
   根因: `dddb/infobox.py build()` 在没有远程地址时输出 `local:<本地路径>`,
   而 `infobox.js applyCover()` 无脑 POST 给 /cover-cache/new, 服务端
   `is_public_image_url()` 只接受 http/https → 必然失败。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _js() -> str:
    return _read("dddb/public/static/public.js")


def _css() -> str:
    return _read("dddb/public/static/public.css")


def _infobox_js() -> str:
    return _read("dddb/public/static/infobox.js")


def _setup_pair_rows_body() -> str:
    """截出 setupPairRows 函数体."""
    js = _js()
    idx = js.find("function setupPairRows(")
    if idx < 0:
        return ""
    depth = 0
    for i in range(idx, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[idx:i + 1]
    return ""


def _edit_html() -> str:
    from dddb.public import templates as T

    album = {
        "key": "d", "title": "T", "title_original": "T", "title_alt": "",
        "romaji": "", "circle": "", "artists": [],
        "credits": [{"role": "作曲", "name": "甲"}],
        "urls": [{"key": "circle_site", "url": "https://a.com/"}],
        "tags": [], "genres": [], "tracks": [], "date": "", "event": "",
        "catalog_no": "", "price": "", "hall": "", "booth": "",
        "description": "", "missing": [], "track_count": 0,
    }
    ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                        user={"id": 1, "username": "ed", "role": "editor"})
    return T.edit_album_page(ctx, album=album,
                             values=T.edit_values_from_album(album))


class TestLinkAddButtonFix(unittest.TestCase):
    """① datalist 不能在 .rows 与 .pair-add 之间."""

    def setUp(self) -> None:
        self.html = _edit_html()

    def test_datalist_before_rows(self) -> None:
        """datalist 必须在 .rows 之前 —— 否则「＋」的兜底会把行塞进 datalist."""
        datalist_at = self.html.find('<datalist id="dl-url_key">')
        self.assertGreaterEqual(datalist_at, 0, "缺少外链名候选 datalist")
        # 外链那个 .rows(第二个 .rows, 第一个是词条) 必须在 datalist 之后
        positions = [m.start() for m in re.finditer(r'<div class="rows">', self.html)]
        self.assertGreaterEqual(len(positions), 2, "词条与外链都要有 .rows")
        url_rows_at = positions[-1]
        self.assertGreater(datalist_at, -1)
        self.assertGreater(
            url_rows_at, datalist_at,
            "datalist 夹在 .rows 与 .pair-add 之间: 「＋」新建的行会被"
            "appendChild 进 datalist, 浏览器不渲染 → 按钮看起来失效")

    def test_nothing_between_rows_and_pair_add(self) -> None:
        """外链 .rows 与 .pair-add 之间不允许夹别的元素."""
        m = re.search(
            r'<div class="rows">([\s\S]*?)</div>\s*(<datalist[\s\S]*?</datalist>)?\s*'
            r'(<div class="pair-add">)',
            self.html)
        self.assertIsNotNone(m)
        self.assertIsNone(m.group(2),
                          "datalist 仍然夹在 .rows 与 .pair-add 之间")

    def test_dynamic_rows_get_datalist(self) -> None:
        """JS 动态建的外链行也要带 list="dl-url_key"."""
        js = _js()
        self.assertIn("DATALIST_FIELDS", js)
        self.assertRegex(js, r"url_key\s*:\s*\"dl-url_key\"")
        self.assertIn('setAttribute("list"', js)

    def test_previous_sibling_lookup_is_not_a_noop_ternary(self) -> None:
        """兜底不能是 `cond ? prev : prev` 这种两边相同的三元."""
        body = _setup_pair_rows_body()
        self.assertTrue(body, "找不到 setupPairRows 函数体")
        self.assertIsNone(
            re.search(r'contains\(\s*"\w+"\s*\)\s*\?\s*prev\s*:\s*prev', body),
            "`x ? prev : prev` 恒等于 prev, 会让容器落到 datalist 上")

    def test_lookup_walks_back_to_rows(self) -> None:
        """兜底必须向前遍历找 .rows, 不能只取一个兄弟."""
        body = _setup_pair_rows_body()
        self.assertRegex(
            body, r"while\s*\(\s*prev\s*\)",
            "兜底应向前遍历兄弟节点找 .rows")
        self.assertIn('contains("rows")', body)

    def test_placeholder_hints_auto_naming(self) -> None:
        """链接名输入框的占位符应说明"填网址自动识别"."""
        self.assertIn("填网址自动识别", _js())


class TestCoverPlusCentered(unittest.TestCase):
    """② 封面加号必须落在格子正中心."""

    def test_fallback_selector_beats_block_rule(self) -> None:
        css = _css()
        rule = re.search(
            r"\.edit-cover-row\s+\.cover-fallback\.edit-cover-img\s*\{([^}]*)\}",
            css)
        self.assertIsNotNone(
            rule,
            "缺少 `.edit-cover-row .cover-fallback.edit-cover-img` 规则: "
            "它被 `.edit-cover-row .edit-cover-img { display:block }` 压掉了")
        body = rule.group(1)
        self.assertIn("display: flex", body)
        self.assertIn("align-items: center", body)
        self.assertIn("justify-content: center", body)

    def test_block_rule_still_exists(self) -> None:
        """原来的 display:block 规则也要在(有封面图时仍需要它)."""
        self.assertRegex(
            _css(), r"\.edit-cover-row\s+\.edit-cover-img\s*\{[^}]*display:\s*block")

    def test_plus_is_css_drawn(self) -> None:
        """"＋"必须用伪元素画, 全角字符在中文字体里竖笔不居中."""
        css = _css()
        self.assertIn(".cover-add-thumb .cover-plus::before", css)
        self.assertIn(".cover-add-thumb .cover-plus::after", css)


class TestInfoboxCoverLocal(unittest.TestCase):
    """③ local: 开头的封面地址不能 POST 出去."""

    def setUp(self) -> None:
        self.js = _infobox_js()

    def _apply_cover_body(self) -> str:
        idx = self.js.find("function applyCover(")
        self.assertGreaterEqual(idx, 0)
        depth = 0
        for i in range(idx, len(self.js)):
            if self.js[i] == "{":
                depth += 1
            elif self.js[i] == "}":
                depth -= 1
                if depth == 0:
                    return self.js[idx:i + 1]
        return ""

    def test_skips_local_prefix(self) -> None:
        body = self._apply_cover_body()
        self.assertIn("local:", body,
                      "applyCover 必须识别 local: 前缀")
        self.assertRegex(body, r"/\^local:/i")

    def test_checks_http_scheme(self) -> None:
        body = self._apply_cover_body()
        self.assertIn("^https?:", body,
                      "只有 http(s) 地址才该发去缓存")
        self.assertRegex(body, r"\^https\?:.*?\.test\(",
                         "http(s) 判定要真的被调用")

    def test_returns_false_when_skipped(self) -> None:
        """跳过时要返回 false, 不然"已填入"提示会说谎."""
        body = self._apply_cover_body()
        skip = body[:body.find("var cacheField")]
        self.assertIn("return false", skip)

    def test_build_emits_local_marker(self) -> None:
        """服务端确实会输出 local: 前缀 —— 这就是必须跳过它的原因."""
        from dddb import infobox as IB

        text = IB.build({"title": "T", "cover_local": "covers/upload/x.jpg"})
        self.assertIn("local:", text)

    def test_no_raw_url_in_body(self) -> None:
        """POST 的 url 必须用清理过的 raw, 不能用原值."""
        body = self._apply_cover_body()
        self.assertIn('body.set("url", raw)', body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
