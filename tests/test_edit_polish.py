"""测试: 制作人员职责按钮 / 草稿条位置 / 多图槽位 / Disc 表格."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _render(**overrides):
    from dddb.public import templates as T

    album = {
        "key": "d", "title": "T", "title_original": "T", "title_alt": "",
        "romaji": "", "circle": "", "artists": ["Riku"],
        "credits": [
            {"role": "mastering", "name": "Ryo Mishima"},
            {"role": "Illustration", "name": "いぬすけ"},
        ],
        "urls": {}, "tags": [], "genres": [],
        "tracks": [
            {"disc": 1, "index": 1, "title": "A", "artists": [],
             "duration": "3:00"},
            {"disc": 2, "index": 1, "title": "B", "artists": [],
             "duration": "4:00"},
        ],
        "date": "", "event": "", "catalog_no": "", "price": "",
        "company": "", "hall": "", "booth": "", "description": "",
        "missing": [], "track_count": 2,
    }
    album.update(overrides)
    ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                        user={"id": 1, "username": "ed", "role": "editor"})
    return T.edit_album_page(
        ctx, album=album, values=T.edit_values_from_album(album),
        bind_options={"artist": [{"id": 1, "name": "Riku"}]})


def _js() -> str:
    return (ROOT / "dddb/public/static/public.js").read_text(encoding="utf-8")


def _css() -> str:
    return (ROOT / "dddb/public/static/public.css").read_text(encoding="utf-8")


class TestPersonChipWithRole(unittest.TestCase):
    """上方按钮 = 「职责 · 人名」, 与详情页一致."""

    def setUp(self) -> None:
        self.html = _render()
        m = re.search(r'data-people="artists">([\s\S]*?)</div>', self.html)
        self.chips = m.group(1) if m else ""

    def test_chip_has_role_attr(self) -> None:
        """`data-role` 保留**库内原始代码**(JS 与测试按代码比对)."""
        self.assertIn('data-role="mastering"', self.chips)
        self.assertIn('data-role="Illustration"', self.chips)

    def test_chip_shows_role_and_name(self) -> None:
        """按钮内容应是 职责 + 人名.

        职责走 ``role_label`` 显示 (库里的 ``mastering`` 要显示成「母带」),
        与详情页一致 —— 否则同一张碟在编辑页是 ``mastering``、详情页是
        「母带」, 两副样子。
        """
        self.assertIn(">母带<", self.chips)
        self.assertIn("<b>Ryo Mishima</b>", self.chips)
        self.assertIn(">插画<", self.chips)
        self.assertIn("<b>いぬすけ</b>", self.chips)

    def test_artist_without_role(self) -> None:
        """纯艺人没有职责, 标「艺人」。"""
        self.assertIn('data-role=""', self.chips)
        self.assertIn(">艺人<", self.chips)

    def test_role_change_refreshes_summary(self) -> None:
        """职责改了 -> 上方只读汇总重建(不再往上方"生成按钮")."""
        js = _js()
        self.assertIn("function rebuildSummary(", js)
        self.assertIn('t.name !== "credit_role"', js,
                      "职责框改动要触发汇总刷新")
        # 生成的 chip 带 role(原始代码), 显示名走 data-role-labels 查表
        self.assertIn('chip.setAttribute("data-role", roleText)', js)
        self.assertIn('getAttribute("data-role-labels")', js)
        self.assertIn("chips.map(roleLabelOf)", js)
        self.assertNotIn("function addRoleChip(", js,
                         "上方已无添加入口(m11474)")

    def test_js_person_input_enter(self) -> None:
        """人员输入框回车也要生成按钮。"""
        js = _js()
        self.assertIn("credit-person-input", js)
        self.assertIn("function addPersonToRow(", js)


class TestDraftBarOutsideCards(unittest.TestCase):
    """草稿提示在卡片外、表单最上方。"""

    def setUp(self) -> None:
        self.html = _render()

    def test_draft_before_cards(self) -> None:
        draft = self.html.find("data-draft-bar")
        cards = self.html.find('id="edit-cards"')
        self.assertGreater(draft, 0, "找不到草稿条")
        self.assertGreater(cards, 0, "找不到卡片网格")
        self.assertLess(draft, cards, "草稿条要在卡片网格之前")

    def test_draft_not_inside_cover_card(self) -> None:
        """不能出现在封面卡片内部。"""
        m = re.search(r'data-card="cover"([\s\S]*?)</section>', self.html)
        self.assertIsNotNone(m)
        self.assertNotIn("data-draft-bar", m.group(1))

    def test_css_border_style(self) -> None:
        css = _css()
        m = re.search(r"\.edit-draft-bar \{([^}]*)\}", css)
        self.assertIsNotNone(m)


class TestCoverSlots(unittest.TestCase):
    """多图: 左封面 + 右侧加号, 全部可删。"""

    def setUp(self) -> None:
        self.html = _render()
        self.js = _js()

    def test_slots_container(self) -> None:
        self.assertIn("data-cover-slots", self.html)

    def test_add_button_is_last(self) -> None:
        """加号格在槽位容器的最后(JS 插入时用 insertBefore)。"""
        js = self.js
        start = js.find("function setupCoverGallery(")
        end = js.find("\n  /*", start + 10)
        block = js[start:end if end > 0 else start + 8000]
        self.assertIn('querySelector("[data-cover-add]")', block)
        self.assertIn("insertBefore(slot, addBtn)", block,
                      "新图要插在加号之前, 加号永远最后")

    def test_every_slot_deletable(self) -> None:
        js = self.js
        start = js.find("function setupCoverGallery(")
        end = js.find("\n  /*", start + 10)
        block = js[start:end if end > 0 else start + 8000]
        self.assertIn('data-cover-del', block)
        self.assertIn("slot.remove()", block)

    def test_primary_highlight(self) -> None:
        css = _css()
        self.assertIn(".cover-slot.is-primary", css)

    def test_delete_primary_promotes_next(self) -> None:
        js = self.js
        start = js.find("function setupCoverGallery(")
        end = js.find("\n  /*", start + 10)
        block = js[start:end if end > 0 else start + 8000]
        self.assertIn("setPrimary(rest[0].src)", block,
                      "删掉封面后要把下一张提为封面")

    def test_add_slot_opens_file_input(self) -> None:
        js = self.js
        start = js.find("function setupCoverGallery(")
        end = js.find("\n  /*", start + 10)
        block = js[start:end if end > 0 else start + 8000]
        self.assertIn("fileInput.click()", block)

    def test_css_slots(self) -> None:
        css = _css()
        for token in (".cover-slots", ".cover-slot", ".cover-slot-add",
                      ".cover-slot-x"):
            self.assertIn(token, css, f"缺样式 {token}")

    def test_old_strip_removed(self) -> None:
        self.assertNotIn("data-cover-strip", self.html)
        self.assertNotIn(".cover-strip", _css())


class TestDiscTableConsistent(unittest.TestCase):
    """新建 Disc 的表格必须与模板一致(10 列, 无批量按钮)。"""

    def setUp(self) -> None:
        self.js = _js()

    def test_add_disc_headers_match_template(self) -> None:
        start = self.js.find("function addDiscBlock(")
        end = self.js.find("\n  function ", start + 10)
        block = self.js[start:end if end > 0 else start + 6000]
        # 找表头数组
        m = re.search(r'\["排序", "音轨"[\s\S]{0,200}?\]', block)
        self.assertIsNotNone(m, "addDiscBlock 的表头不是 10 列新顺序")
        labels = re.findall(r'"([^"]*)"', m.group(0))
        self.assertEqual(labels, ["排序", "音轨", "歌曲名", "副标题",
                                  "翻译歌名", "艺人", "语种", "曲风",
                                  "时长", "操作"])

    def test_no_bulk_button(self) -> None:
        start = self.js.find("function addDiscBlock(")
        end = self.js.find("\n  function ", start + 10)
        block = self.js[start:end if end > 0 else start + 6000]
        self.assertNotIn("data-disc-bulk", block)

    def test_new_disc_gets_a_row(self) -> None:
        start = self.js.find("function addDiscBlock(")
        end = self.js.find("\n  function ", start + 10)
        block = self.js[start:end if end > 0 else start + 6000]
        self.assertIn("buildTrackRow(discNo, 1)", block)

    def test_template_disc2_has_10_columns(self) -> None:
        html = _render()
        blocks = re.findall(
            r'data-disc="2"([\s\S]{0,4000}?)</table>', html)
        self.assertTrue(blocks, "找不到 Disc 02")
        m = re.search(r"<thead><tr>([\s\S]*?)</tr></thead>", blocks[0])
        self.assertIsNotNone(m)
        heads = re.findall(r"<th>([^<]*)</th>", m.group(1))
        self.assertEqual(len(heads), 10, f"Disc 02 表头应有 10 列: {heads}")

    def test_disc2_cells_match(self) -> None:
        html = _render()
        m = re.search(r'data-disc="2"([\s\S]{0,4000}?)</table>', html)
        self.assertIsNotNone(m)
        row = re.search(r'<tr class="track-row"[^>]*>([\s\S]*?)</tr>', m.group(1))
        self.assertIsNotNone(row, "Disc 02 没有曲目行")
        self.assertEqual(row.group(1).count("<td"), 10,
                         "数据行应有 10 格")


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
