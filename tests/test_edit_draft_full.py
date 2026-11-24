"""草稿保存要"全": 行增删 / chip 变更 / radio 都要进草稿, 恢复要能还原。

用户 m11689: 「草稿保存的字段并不全」。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = ROOT / "dddb" / "public" / "static" / "public.js"
TPL = ROOT / "dddb" / "public" / "templates.py"


def _block(src: str, name: str) -> str:
    """截出 `function name(...) {...}` 的完整函数体。"""
    i = src.index("function " + name + "(")
    depth = 0
    started = False
    for j in range(i, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
            started = True
        elif c == "}":
            depth -= 1
            if started and depth == 0:
                return src[i:j + 1]
    raise AssertionError("括号不配对: " + name)


class TestDraftTriggers(unittest.TestCase):
    """保存触发要覆盖 click 与 DOM 变更, 不能只看 input/change。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.src = JS.read_text(encoding="utf-8")
        cls.body = _block(cls.src, "setupEditDraft")

    def test_listens_click(self) -> None:
        self.assertIn('form.addEventListener("click", markDirty)', self.body)

    def test_observes_dom_changes(self) -> None:
        self.assertIn("MutationObserver", self.body)
        self.assertIn("childList: true", self.body)
        self.assertIn("subtree: true", self.body)

    def test_observes_guarded(self) -> None:
        """老浏览器没有 MutationObserver, 不能直接 new。"""
        self.assertIn('typeof window.MutationObserver === "function"', self.body)

    def test_still_listens_input_and_change(self) -> None:
        self.assertIn('form.addEventListener("input", markDirty)', self.body)
        self.assertIn('form.addEventListener("change", markDirty)', self.body)

    def test_debounce_shared(self) -> None:
        """所有入口走同一个 scheduleSave, 各自 setTimeout 会互相覆盖。"""
        self.assertIn("function scheduleSave()", self.body)
        self.assertIn("window.clearTimeout(timer)", self.body)


class TestNoSelfTriggerLoop(unittest.TestCase):
    """状态条文字在 form 内, 写它又触发 MutationObserver -> 必须判等。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.body = _block(JS.read_text(encoding="utf-8"), "setupEditDraft")

    def test_compares_stamp_before_writing(self) -> None:
        self.assertIn("lastSaved", self.body)
        self.assertIn("if (!force && (!stamp || stamp === lastSaved)) { return; }",
                      self.body)

    def test_stamp_is_values_only(self) -> None:
        """指纹只由表单值算出来。

        曾经的写法是 `payload.slice(0, payload.indexOf(',"at":'))`, 但 JSON
        键序是 action / at / values, 切出来只剩 action —— 第一次保存后指纹
        永远相同, 后面改的全被跳过(用户 m12045: 刷新后草稿丢失)。
        """
        self.assertNotIn('payload.indexOf(\',"at":\')', self.body)
        self.assertIn("function snapshotOf(values)", self.body)
        self.assertIn("JSON.stringify({ action: action, values: values })",
                      self.body)


class TestDirtyGate(unittest.TestCase):
    """载入时 chip 重建会触发 MutationObserver, 不能因此用页面原值覆盖草稿。

    这是「刷新后草稿丢失」的直接成因: setupBindFields / setupPeopleChips
    在 setupEditDraft 之后跑, 它们重建 chip 是 DOM 变更, 若"变更就存",
    1.2 秒后草稿就被页面原值顶掉了。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.body = _block(JS.read_text(encoding="utf-8"), "setupEditDraft")

    def test_save_is_gated_by_dirty(self) -> None:
        self.assertIn("var dirty = false;", self.body)
        self.assertIn("if (!force && !dirty) { return; }", self.body)

    def test_mutations_do_not_mark_dirty(self) -> None:
        self.assertIn("function onMutation() {", self.body)
        self.assertIn("if (dirty) { scheduleSave(); }", self.body)
        # MutationObserver 走 onMutation, 不能直接挂 scheduleSave
        self.assertIn("new window.MutationObserver(onMutation)", self.body)

    def test_user_events_mark_dirty(self) -> None:
        for ev in ("input", "change", "click", "keydown", "drop"):
            self.assertIn('form.addEventListener("%s", markDirty)' % ev,
                          self.body)

    def test_flush_on_unload(self) -> None:
        """防抖 1.2 秒, 打完字立刻刷新就来不及写 —— 离开前补一次。"""
        self.assertIn('window.addEventListener("beforeunload"', self.body)
        self.assertIn("visibilitychange", self.body)

    def test_submit_clears_draft(self) -> None:
        self.assertIn('form.addEventListener("submit"', self.body)
        self.assertIn("window.localStorage.removeItem(KEY)", self.body)

    def test_identical_draft_is_dropped(self) -> None:
        """草稿和页面当前值一样时不必提示恢复。"""
        self.assertIn("JSON.stringify(saved.values) === JSON.stringify(collect())",
                      self.body)


class TestRestoreFitsRows(unittest.TestCase):
    """恢复要先把重复行补齐/裁掉, 否则草稿里的第 3 条词条无处可放。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.body = _block(JS.read_text(encoding="utf-8"), "setupEditDraft")

    def test_has_fit_pair_rows(self) -> None:
        self.assertIn("function (kind, need)", self.body)
        self.assertIn('[data-pair-add="', self.body)

    def test_fits_credit_and_url(self) -> None:
        self.assertIn('fitPairRows("credit"', self.body)
        self.assertIn('fitPairRows("url"', self.body)

    def test_fits_discs_from_track_disc(self) -> None:
        self.assertIn('listOf("track_disc")', self.body)
        self.assertIn("fitDiscs(", self.body)
        self.assertIn("fitTrackRows(", self.body)

    def test_fit_track_rows_uses_add_button(self) -> None:
        self.assertIn('[data-track-add="1"]', self.body)

    def test_guards_against_infinite_loops(self) -> None:
        self.assertIn("guard < 80", self.body)
        self.assertIn("guard < 20", self.body)
        self.assertIn("guard < 200", self.body)


class TestRestoreDispatchesChange(unittest.TestCase):
    """chip 类控件只监听 change, 恢复必须派发 change 才会重建。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.body = _block(JS.read_text(encoding="utf-8"), "setupEditDraft")

    def test_dispatches_both_events(self) -> None:
        self.assertIn('new Event("input", { bubbles: true })', self.body)
        self.assertIn('new Event("change", { bubbles: true })', self.body)

    def test_sets_checked_for_radio(self) -> None:
        self.assertIn('el.type === "radio" || el.type === "checkbox"', self.body)
        self.assertIn("el.checked = list.indexOf(el.value) !== -1", self.body)

    def test_skips_file_inputs(self) -> None:
        self.assertIn('if (el.type === "file") { return; }', self.body)

    def test_removes_restore_button_after_use(self) -> None:
        self.assertIn("restore.parentNode.removeChild(restore)", self.body)


class TestCreditNameChangeRebuildsChips(unittest.TestCase):
    """草稿恢复写 credit_name 后, 行内 chip 要真的出现。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.src = JS.read_text(encoding="utf-8")
        cls.body = _block(cls.src, "setupCreditPeople")

    def test_has_add_chip_only(self) -> None:
        self.assertIn("function addChipOnly(row, name)", self.body)

    def test_listens_change_on_credit_name(self) -> None:
        self.assertIn('addEventListener("change"', self.body)
        self.assertIn('t.name !== "credit_name"', self.body)

    def test_rebuilds_after_external_write(self) -> None:
        self.assertIn("rebuildSummary()", self.body)
        self.assertIn("split(/[\\n、]+/)", self.body)


class TestVersionBumped(unittest.TestCase):
    def test_public_js_version(self) -> None:
        text = TPL.read_text(encoding="utf-8")
        m = re.search(r"public\.js\?v=(\d+)", text)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(int(m.group(1)), 58)


if __name__ == "__main__":
    unittest.main()


class TestManualSaveButton(unittest.TestCase):
    """「保存草稿」按钮: 不必等防抖, 也不必先改过东西(用户 m12181)。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.src = JS.read_text(encoding="utf-8")
        cls.body = _block(cls.src, "setupEditDraft")
        cls.tpl = TPL.read_text(encoding="utf-8")

    def test_button_in_markup(self) -> None:
        self.assertIn('data-draft-save="1"', self.tpl)
        self.assertIn(">保存草稿</button>", self.tpl)

    def test_button_next_to_clear(self) -> None:
        """窄条里只该有一行两个按钮, 保存在前、清除在后。"""
        save_at = self.tpl.index('data-draft-save="1"')
        clear_at = self.tpl.index('data-draft-clear="1"')
        self.assertLess(save_at, clear_at)

    def test_save_takes_force(self) -> None:
        self.assertIn("function save(force)", self.body)
        self.assertIn("if (!force && !dirty) { return; }", self.body)
        self.assertIn("if (!force && (!stamp || stamp === lastSaved)) { return; }",
                      self.body)

    def test_button_handler_forces_save(self) -> None:
        self.assertIn('document.querySelector("[data-draft-save]")', self.body)
        self.assertIn("save(true)", self.body)

    def test_button_clears_pending_timer(self) -> None:
        """手动存完不该再被 1.2 秒后的那次自动保存重复写一遍。"""
        self.assertIn("window.clearTimeout(timer)", self.body)
        self.assertIn("timer = null", self.body)

    def test_failure_is_reported(self) -> None:
        self.assertIn("保存草稿失败", self.body)

    def test_status_text_says_saved(self) -> None:
        self.assertIn('"草稿已保存 "', self.body)

    def test_unload_does_not_force(self) -> None:
        """beforeunload 直接把 save 交出去会把 Event 当 force, 要包一层。"""
        self.assertIn("window.addEventListener(\"beforeunload\", function () { save(false); })",
                      self.body)
        self.assertNotIn('window.addEventListener("beforeunload", save)', self.body)
