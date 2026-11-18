"""展会索引页的标题行按钮不能重复.

用户截图: 标题行出现两个「新建展会」——
    一个是 ``layout`` 按 ``active="events"`` 自动渲染的,
    另一个是我在 body 顶部又手写了一遍。

修法: body 里不再渲染按钮, 全部走 ``title_actions``
(传了它就由调用方完全控制, layout 不会再补一遍)。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _ctx(editor: bool = True):
    from dddb.public import templates as T

    return T.SiteContext(csrf_token="t", is_editor=editor, lang="zh",
                         user={"id": 1, "username": "e",
                               "role": "editor" if editor else "member"})


def _html(editor: bool = True) -> str:
    from dddb.public import templates as T

    return T.events_index_page(_ctx(editor), events=[], kinds=[], series=[])


class TestNoDuplicateButton(unittest.TestCase):
    def test_new_event_once(self) -> None:
        html = _html()
        # 链接出现次数(而不是文字包含)
        n = html.count('href="/new/event"')
        self.assertEqual(n, 1, f"「新建展会」出现 {n} 次(应 1 次)")

    def test_new_series_once(self) -> None:
        html = _html()
        n = html.count('href="/event-series/new"')
        self.assertEqual(n, 1, f"「新建展会系列」出现 {n} 次(应 1 次)")

    def test_both_in_same_group(self) -> None:
        """两个按钮必须在同一个 title-actions 容器里(同一行)。"""
        html = _html()
        m = re.search(r'<span class="title-actions">([\s\S]*?)</span>', html)
        self.assertIsNotNone(m, "没找到 title-actions")
        group = m.group(1)
        self.assertIn('href="/new/event"', group, "新建展会不在按钮组里")
        self.assertIn('href="/event-series/new"', group,
                      "新建展会系列不在同一组")

    def test_no_stray_row(self) -> None:
        """之前那个 body 里的独立行要彻底去掉。"""
        html = _html()
        self.assertNotIn("new-btn-row", html)

    def test_member_sees_none(self) -> None:
        html = _html(editor=False)
        self.assertNotIn('href="/new/event"', html)
        self.assertNotIn('href="/event-series/new"', html)

    def test_css_no_dead_rule(self) -> None:
        css = (ROOT / "dddb/public/static/public.css").read_text(
            encoding="utf-8")
        self.assertNotIn(".new-btn-row", css, "残留的无用样式")

    def test_title_actions_layout(self) -> None:
        """按钮组是 inline-flex + gap, 才能并排。"""
        css = (ROOT / "dddb/public/static/public.css").read_text(
            encoding="utf-8")
        block = css.split(".title-actions {")[1].split("}")[0]
        self.assertIn("inline-flex", block)
        self.assertIn("gap", block)


class TestOtherPagesStillHaveOne(unittest.TestCase):
    """其它索引页(专辑/社团/艺人/标签)各有自己的新建按钮, 不受影响。"""

    def _page(self, active: str) -> str:
        from dddb.public import templates as T

        buttons = T._entity_buttons(_ctx(), active)
        return buttons

    def test_each_has_its_own(self) -> None:
        cases = {
            "albums": "/new/album",
            "circles": "/new/circle",
            "artists": "/new/artist",
            "tags": "/new/tag",
        }
        for active, href in cases.items():
            with self.subTest(active=active):
                html = self._page(active)
                self.assertIn(href, html)
                self.assertEqual(html.count(href), 1)

    def test_events_no_longer_auto(self) -> None:
        """events 页的按钮由 title_actions 控制, _entity_buttons 里
        仍然会有 event(其它页面可能用), 但展会页不重复调用。"""
        html = _html()
        self.assertEqual(html.count('href="/new/event"'), 1)


if __name__ == "__main__":
    unittest.main()
