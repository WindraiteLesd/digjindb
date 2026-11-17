"""测试: 手动新增的词条行必须与服务端渲染结构一致.

历史 bug: buildPairRow 用通用模板(credit_name 纯文本框), 新行缺少
credit-person-input / credit-people-box, 导致回车加入逻辑匹配不到。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _js() -> str:
    return (ROOT / "dddb/public/static/public.js").read_text(encoding="utf-8")


def _render():
    from dddb.public import templates as T

    album = {
        "key": "d", "title": "T", "title_original": "T", "title_alt": "",
        "romaji": "", "circle": "", "artists": [],
        "credits": [{"role": "mastering", "name": "Ryo Mishima"}],
        "urls": {}, "tags": [], "genres": [], "tracks": [], "date": "",
        "event": "", "catalog_no": "", "price": "", "company": "",
        "hall": "", "booth": "", "description": "", "missing": [],
        "track_count": 0,
    }
    ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                        user={"id": 1, "username": "ed", "role": "editor"})
    return T.edit_album_page(ctx, album=album,
                             values=T.edit_values_from_album(album),
                             bind_options={"artist": []})


class TestBuildCreditRow(unittest.TestCase):
    """JS 新增的 credit 行结构。"""

    def setUp(self) -> None:
        self.js = _js()

    def test_build_credit_row_exists(self) -> None:
        self.assertIn("function buildCreditRow(", self.js)

    def test_build_pair_row_dispatches_to_credit(self) -> None:
        start = self.js.find("function buildPairRow(")
        end = self.js.find("\n  function ", start + 10)
        block = self.js[start:end if end > 0 else start + 2500]
        self.assertIn('if (kind === "credit") { return buildCreditRow(); }',
                      block,
                      "credit 必须走专门的结构")

    def test_credit_row_has_required_parts(self) -> None:
        """新行必须具备回车逻辑依赖的所有元素。"""
        start = self.js.find("function buildCreditRow(")
        end = self.js.find("\n  function ", start + 10)
        block = self.js[start:end if end > 0 else start + 3000]
        required = [
            "credit-row",              # class 要能匹配 .credit-row
            "credit_role",             # 职责输入框
            "credit-people-box",       # 人员容器
            "credit-people-chips",     # 行内 chip 容器
            "credit-person-input",     # **人员输入框(回车入口)**
            "credit_name",             # 隐藏字段
        ]
        for token in required:
            self.assertIn(token, block, f"新增行缺 {token}")

    def test_credit_row_class_matches_css(self) -> None:
        """新行的 class 必须与模板一致(pair-row credit-row)。"""
        start = self.js.find("function buildCreditRow(")
        end = self.js.find("\n  function ", start + 10)
        block = self.js[start:end if end > 0 else start + 3000]
        self.assertIn('row.className = "pair-row credit-row"', block)

    def test_credit_row_has_delete_button(self) -> None:
        start = self.js.find("function buildCreditRow(")
        end = self.js.find("\n  function ", start + 10)
        block = self.js[start:end if end > 0 else start + 3000]
        self.assertIn("data-pair-del", block)


class TestStructureParity(unittest.TestCase):
    """JS 新增行与服务端渲染行的结构对齐。"""

    def test_same_classes(self) -> None:
        html = _render()
        # 取第一个 credit-row 到下一个 pair-row 之前(或到 </div> 结束)
        start = html.find('<div class="pair-row credit-row"')
        self.assertGreater(start, 0, "找不到服务端渲染的词条行")
        nxt = html.find('<div class="pair-row', start + 10)
        server_row = html[start:nxt if nxt > 0 else start + 1500]

        js = _js()
        start = js.find("function buildCreditRow(")
        end = js.find("\n  function ", start + 10)
        js_row = js[start:end if end > 0 else start + 3000]

        # 两边都要有的结构标记
        for token in ("credit-people-field", "credit-people-box",
                      "credit-people-chips", "credit-person-input"):
            self.assertIn(token, server_row, f"服务端缺 {token}")
            self.assertIn(token, js_row, f"JS 新增行缺 {token}")

    def test_hint_text_matches(self) -> None:
        """提示文案也要一致。"""
        html = _render()
        self.assertIn("输入后回车加入", html)
        js = _js()
        self.assertIn("输入后回车加入", js)


class TestEventDelegationCoversNewRows(unittest.TestCase):
    """事件委托要挂在 card 上(新行加进来也能用)。"""

    def setUp(self) -> None:
        self.js = _js()

    def _block(self) -> str:
        start = self.js.find("function setupCreditPeople(")
        end = self.js.find("\n  /*", start + 10)
        return self.js[start:end if end > 0 else start + 9000]

    def test_keydown_on_card(self) -> None:
        self.assertIn('card.addEventListener("keydown"', self._block())

    def test_input_on_card(self) -> None:
        self.assertIn('card.addEventListener("input"', self._block())

    def test_click_on_card(self) -> None:
        self.assertIn('card.addEventListener("click"', self._block())

    def test_mousedown_on_card(self) -> None:
        self.assertIn('card.addEventListener("mousedown"', self._block())

    def test_role_check_uses_class(self) -> None:
        """判断人员输入框用 classList(新行动态创建也能命中)。"""
        block = self._block()
        self.assertIn('input.classList.contains("credit-person-input")', block)


class TestSuggestPool(unittest.TestCase):
    """候选池缓存 + 新增行也能出候选。"""

    def test_pool_cached(self) -> None:
        js = _js()
        self.assertIn("__creditArtistPool", js,
                      "候选池要缓存(2000 个 DOM 每次查太慢)")

    def test_pool_is_array(self) -> None:
        js = _js()
        self.assertIn("pool.forEach(", js,
                      "缓存后是数组, 用 forEach 而不是 Array.prototype.forEach.call")


class TestFocusOnNewRow(unittest.TestCase):
    def test_focus_person_input(self) -> None:
        js = _js()
        self.assertIn('newRow.querySelector(".credit-person-input")', js,
                      "新增后焦点给人员输入框")


if __name__ == "__main__":
    unittest.main()
