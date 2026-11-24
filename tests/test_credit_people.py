"""测试: 制作人员按钮统一 + 候选框自动关闭."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _render(**overrides):
    from dddb.public import templates as T

    album = {
        "key": "d", "title": "T", "title_original": "T", "title_alt": "",
        "romaji": "", "circle": "", "artists": ["Riku", "Clion"],
        "credits": [
            {"role": "compose", "name": "Riku, Clion"},
            {"role": "Illustration/Design", "name": "川合機械"},
            {"role": "Mastering", "name": "Ryo Mishima"},
        ],
        "urls": {}, "tags": [], "genres": [], "tracks": [], "date": "",
        "event": "", "catalog_no": "", "price": "", "company": "",
        "hall": "", "booth": "", "description": "", "missing": [],
        "track_count": 0,
    }
    album.update(overrides)
    ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                        user={"id": 1, "username": "ed", "role": "editor"})
    return T.edit_album_page(
        ctx, album=album, values=T.edit_values_from_album(album),
        bind_options={"artist": [{"id": 1, "name": "Riku"},
                                 {"id": 2, "name": "Clion"}]})


def _js() -> str:
    return (ROOT / "dddb/public/static/public.js").read_text(encoding="utf-8")


def _css() -> str:
    return (ROOT / "dddb/public/static/public.css").read_text(encoding="utf-8")


class TestPeopleUnifiedList(unittest.TestCase):
    """上方按钮是所有人员的唯一列表。"""

    def setUp(self) -> None:
        self.html = _render()
        m = re.search(r'data-card="credits"([\s\S]*?)</section>', self.html)
        self.credits = m.group(1) if m else ""
        self.chips = re.findall(r'data-person="([^"]*)"', self.credits)

    def test_chips_include_credit_names(self) -> None:
        """词条里的艺人也要出现在上方按钮里。"""
        for name in ("Riku", "Clion", "川合機械", "Ryo Mishima"):
            self.assertIn(name, self.chips,
                          f"{name} 没出现在上方按钮")

    def test_chips_deduped(self) -> None:
        self.assertEqual(len(self.chips), len(set(self.chips)),
                         "上方按钮不能有重复")

    def test_artists_hidden_field_has_all(self) -> None:
        m = re.search(r'<input type="hidden" name="artists" value="([^"]*)"',
                      self.credits)
        self.assertIsNotNone(m)
        value = m.group(1)
        for name in ("Riku", "川合機械", "Ryo Mishima"):
            self.assertIn(name, value,
                          f"{name} 不在 artists 隐藏字段里")

    def test_credit_rows_keep_role_input(self) -> None:
        """词条行数 = 实际数据条数(**不再自动补空行**)。"""
        self.assertEqual(self.credits.count('name="credit_role"'), 3,
                         "有 3 条词条就只渲染 3 行")

    def test_no_trailing_empty_rows(self) -> None:
        roles = re.findall(r'name="credit_role" value="([^"]*)"', self.credits)
        for role in roles:
            self.assertTrue(role, f"出现了空词条行: {roles}")

    def test_credit_rows_use_input_field(self) -> None:
        """词条人员是**输入框**(输入回车加入), 不再是点选按钮。"""
        self.assertGreater(self.credits.count("credit-person-input"), 0,
                           "要有人员输入框")
        self.assertNotIn("data-credit-people", self.credits,
                         "点选按钮已移除")
        self.assertGreater(self.credits.count('name="credit_name"'), 0)

    def test_credit_row_shows_existing_people(self) -> None:
        """已有人员以行内 chip 显示。"""
        names = re.findall(r'class="credit-person-chip" data-name="([^"]*)"',
                           self.credits)
        self.assertTrue(names, "找不到行内人员 chip")
        self.assertIn("Ryo Mishima", names)


class TestCreditPeopleJs(unittest.TestCase):
    """词条人员选择器的 JS."""

    def setUp(self) -> None:
        self.js = _js()

    def test_registered(self) -> None:
        self.assertIn("function setupCreditPeople(", self.js)
        self.assertIn("setupCreditPeople();", self.js)

    def test_reads_chips_as_source(self) -> None:
        start = self.js.find("function setupCreditPeople(")
        end = self.js.find("\n  /*", start + 10)
        block = self.js[start:end if end > 0 else start + 6000]
        self.assertIn('[data-people="artists"]', block)
        self.assertIn("function rebuildSummary(", block)
        self.assertIn("person-chip is-static", block)
        self.assertNotIn("data-person-del", block)

    def test_writes_hidden_field(self) -> None:
        start = self.js.find("function setupCreditPeople(")
        end = self.js.find("\n  /*", start + 10)
        block = self.js[start:end if end > 0 else start + 6000]
        self.assertIn('name="credit_name"', block)

    def test_add_person_on_enter(self) -> None:
        """人员输入框回车 -> 加进该行 -> 刷新上方只读汇总。"""
        start = self.js.find("function setupCreditPeople(")
        end = self.js.find("\n  /*", start + 10)
        block = self.js[start:end if end > 0 else start + 8000]
        self.assertIn("credit-person-input", block)
        self.assertIn("addPersonToRow", block)
        self.assertIn("rebuildSummary()", block)
        # 不再往上方"生成按钮" —— 上方是只读的
        self.assertNotIn("addRoleChip", block)

    def test_delete_drops_from_base(self) -> None:
        """删掉行内人员时, 基础名单(data-artists)也要跟着删.

        infobox 填入会把 artists 写进 data-artists, rebuildSummary 每次都从
        它起头 —— 只删 chip 不删基础名单, 那个人下次重建就又冒出来了
        (用户 m12610: 删除后仍然出现在预览中)。
        """
        start = self.js.find("function setupCreditPeople(")
        end = self.js.find("\n  /*", start + 10)
        block = self.js[start:end if end > 0 else start + 9000]
        self.assertIn("function dropFromBase(", block)
        idx = block.find("[data-credit-person-del]")
        self.assertGreater(idx, 0)
        after = block[idx:idx + 900]
        self.assertIn("dropFromBase(gone)", after)
        self.assertIn("rebuildSummary()", after)


class TestSummaryIsReadOnly(unittest.TestCase):
    """上方那排 chip 只是"下方词条合并后的效果", 不再有添加/删除入口。

    用户要求(m11474): 上面只显示效果, 不是具有添加功能的一部分。
    原来的搜索框 / 候选弹层 / × 反向删除都已移除。
    """

    def setUp(self) -> None:
        self.js = _js()
        self.html = _render()

    def _credit_card(self) -> str:
        start = self.html.find('data-card="credits"')
        end = self.html.find("</section>", start)
        return self.html[start:end]

    def test_no_picker_in_html(self) -> None:
        card = self._credit_card()
        self.assertNotIn("people-picker", card)
        self.assertNotIn("people-search", card)
        self.assertIn("people-chips is-summary", card)

    def test_chips_have_no_delete_button(self) -> None:
        card = self._credit_card()
        summary = card[card.find("people-chips is-summary"):]
        summary = summary[:summary.find("</div>")]
        self.assertIn("person-chip", summary)
        self.assertNotIn("data-person-del", summary)
        self.assertIn("is-static", summary)

    def test_summary_carries_base_people(self) -> None:
        """原有 artists 不能丢 —— 有 119 张专辑的 artists 不在任何词条里。"""
        card = self._credit_card()
        self.assertIn("data-artists=", card)
        self.assertIn('name="artists"', card)

    def test_candidate_pool_is_hidden(self) -> None:
        """候选池保留了(词条行的联想要用), 但对上方不再可见。"""
        self.assertIn("people-suggest-list", self.html)
        self.assertIn("__creditArtistPool", self.js)

    def test_summary_rebuilt_from_rows(self) -> None:
        self.assertIn("function rebuildSummary(", self.js)

    def test_no_popup_anymore(self) -> None:
        """上方人员不再是弹层选择, 所以没有 pop 元素了。"""
        self.assertNotIn("credit-people-pop", self.js,
                         "点选弹层已移除")

    def test_credit_suggest_still_closes(self) -> None:
        """词条行自己的候选框仍要能收起(点选后/失焦后)。"""
        self.assertIn("function closeSuggest(", self.js)
        self.assertIn('addEventListener("mousedown"', self.js)


class TestCreditPeopleCss(unittest.TestCase):
    def test_styles(self) -> None:
        css = _css()
        for token in (".credit-row", ".credit-people-box",
                      ".credit-person-chip", ".credit-person-input"):
            self.assertIn(token, css, f"缺样式 {token}")

    def test_input_box_focus_style(self) -> None:
        css = _css()
        m = re.search(r"\.credit-people-box \{([^}]*)\}", css)
        self.assertIsNotNone(m)
        self.assertIn("flex-wrap: wrap", m.group(1),
                      "多人时要换行")


class TestSaveRoundTrip(unittest.TestCase):
    """保存时 credit_name 仍是普通字段, 解析不受影响。"""

    def test_parse_credits(self) -> None:
        """一个职责对应多人时, 后端会拆成**多条 Credit**(既有设计)."""
        from dddb.public.site import parse_album_form

        patch, errors = parse_album_form({
            "title": ["T"], "title_original": ["T"],
            "credit_role": ["compose", "Mastering"],
            "credit_name": ["Riku、Clion", "Ryo Mishima"],
        })
        self.assertEqual(errors, {})
        credits = patch["credits"]
        # compose 有两个人 -> 2 条; Mastering 一个 -> 1 条
        roles = [c["role"] for c in credits]
        self.assertEqual(roles, ["compose", "compose", "Mastering"])
        names = [c["name"] for c in credits]
        self.assertIn("Riku", names)
        self.assertIn("Clion", names)
        self.assertIn("Ryo Mishima", names)


class TestAllSetupRegistered(unittest.TestCase):
    def test_boot(self) -> None:
        js = _js()
        idx = js.find("function boot(")
        depth = 0
        boot = ""
        for i in range(idx, len(js)):
            if js[i] == "{":
                depth += 1
            elif js[i] == "}":
                depth -= 1
                if depth == 0:
                    boot = js[idx:i + 1]
                    break
        defined = set(re.findall(r"function (setup\w+)\s*\(", js))
        missing = sorted(fn for fn in defined if (fn + "()") not in boot)
        self.assertEqual(missing, [], f"未注册: {missing}")


if __name__ == "__main__":
    unittest.main()
