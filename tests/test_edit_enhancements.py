"""测试: 编辑界面增强(人员按钮化/封 face 多图/草稿/无回车提交/时长)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _render(**album_overrides):
    from dnh.public import templates as T

    album = {
        "key": "d", "title": "T", "title_original": "T", "title_alt": "",
        "romaji": "", "circle": "社团A", "artists": ["Riku", "Clion"],
        "credits": [{"role": "compose", "name": "Riku"}], "urls": {},
        "tags": [], "genres": [], "tracks": [
            {"disc": 1, "index": 1, "title": "曲", "artists": [],
             "duration": "3:42"},
        ], "date": "", "event": "M3-2026秋", "catalog_no": "",
        "price": "", "company": "", "hall": "", "booth": "",
        "description": "", "missing": [], "track_count": 1,
        "has_cover": True,
    }
    album.update(album_overrides)
    ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                        user={"id": 1, "username": "ed", "role": "editor"})
    return T.edit_album_page(
        ctx, album=album, values=T.edit_values_from_album(album),
        bind_options={
            "artist": [{"id": 12, "name": "Riku"},
                       {"id": 34, "name": "Clion"}],
            "event": [{"id": 1, "name": "M3-2026秋"}],
        })


def _js() -> str:
    return (ROOT / "dnh/public/static/public.js").read_text(encoding="utf-8")


def _css() -> str:
    return (ROOT / "dnh/public/static/public.css").read_text(encoding="utf-8")


class TestPeopleChips(unittest.TestCase):
    """制作人员全部按钮化 + 取消艺人栏位 + ArtistId 匹配."""

    def setUp(self) -> None:
        self.html = _render()
        m = re.search(r'data-card="credits"([\s\S]*?)</section>', self.html)
        self.credits = m.group(1) if m else ""

    def test_no_artist_bind_field(self) -> None:
        """艺人不再是"输入框 + 候选"的绑定栏位。"""
        self.assertNotIn('data-bind="artist"', self.html)

    def test_artists_are_chips(self) -> None:
        self.assertGreater(self.credits.count("person-chip"), 0)
        for name in ("Riku", "Clion"):
            self.assertIn(name, self.credits)

    def test_chips_carry_entity_id(self) -> None:
        """chip 要带 ArtistId(重名/马甲靠它区分)。"""
        ids = re.findall(r'data-entity-id="(\d+)"', self.credits)
        self.assertIn("12", ids)
        self.assertIn("34", ids)

    def test_hidden_artists_field(self) -> None:
        self.assertIn('name="artists"', self.credits)

    def test_artist_picker_present(self) -> None:
        self.assertIn("people-picker", self.credits)
        self.assertIn("people-search", self.credits)
        self.assertIn("person-suggest", self.credits)

    def test_credits_rows_still_there(self) -> None:
        self.assertIn('name="credit_role"', self.credits)
        self.assertIn('name="credit_name"', self.credits)

    def test_js_handles_chips(self) -> None:
        js = _js()
        self.assertIn("function setupPeopleChips(", js)
        self.assertIn("setupPeopleChips();", js)
        self.assertIn("data-entity-id", js)


class TestEventSuggestAll(unittest.TestCase):
    """展会备选无需输入即显示."""

    def test_event_has_suggest_all(self) -> None:
        html = _render()
        m = re.search(r'data-bind="event"[^>]*>', html)
        self.assertIsNotNone(m, "找不到展会字段")
        self.assertIn("data-suggest-all", m.group(0))

    def test_js_shows_all_on_empty(self) -> None:
        js = _js()
        self.assertIn("showAllOnFocus", js)
        self.assertIn("data-suggest-all", js)

    def test_other_bind_fields_unchanged(self) -> None:
        """社团/标签仍然需要输入才出候选。"""
        html = _render()
        m = re.search(r'data-bind="circle"[^>]*>', html)
        self.assertIsNotNone(m)
        self.assertNotIn("data-suggest-all", m.group(0))


class TestNoEnterSubmit(unittest.TestCase):
    """不要把回车当提交。"""

    def test_js_registered(self) -> None:
        js = _js()
        self.assertIn("function setupNoEnterSubmit(", js)
        self.assertIn("setupNoEnterSubmit();", js)

    def test_prevents_default_on_input(self) -> None:
        js = _js()
        start = js.find("function setupNoEnterSubmit(")
        end = js.find("\n  /*", start + 10)
        block = js[start:end if end > 0 else start + 2500]
        self.assertIn("preventDefault", block)
        self.assertIn("TEXTAREA", block, "textarea 里回车要保留换行")

    def test_ctrl_enter_submits(self) -> None:
        js = _js()
        start = js.find("function setupNoEnterSubmit(")
        end = js.find("\n  /*", start + 10)
        block = js[start:end if end > 0 else start + 2500]
        self.assertIn("ctrlKey", block)
        self.assertIn("metaKey", block)


class TestDraftAutosave(unittest.TestCase):
    """自动保存草稿。"""

    def test_draft_bar_in_html(self) -> None:
        html = _render()
        self.assertIn("data-draft-bar", html)
        self.assertIn("data-draft-clear", html)

    def test_js_registered(self) -> None:
        js = _js()
        self.assertIn("function setupEditDraft(", js)
        self.assertIn("setupEditDraft();", js)

    def test_uses_localstorage(self) -> None:
        js = _js()
        start = js.find("function setupEditDraft(")
        end = js.find("\n  /*", start + 10)
        block = js[start:end if end > 0 else start + 4000]
        self.assertIn("localStorage", block)
        self.assertIn("dddb:edit-draft", block)

    def test_does_not_auto_overwrite(self) -> None:
        """只提示恢复, 不自动套用(避免冲掉别人的修改)。"""
        js = _js()
        start = js.find("function setupEditDraft(")
        end = js.find("\n  /*", start + 10)
        block = js[start:end if end > 0 else start + 4000]
        self.assertIn("恢复草稿", block)

    def test_debounced(self) -> None:
        js = _js()
        start = js.find("function setupEditDraft(")
        end = js.find("\n  /*", start + 10)
        block = js[start:end if end > 0 else start + 4000]
        self.assertIn("setTimeout", block, "要防抖, 不要每次输入都写")


class TestCoverGallery(unittest.TestCase):
    """封面点击放大 + 多图。"""

    def test_zoom_button(self) -> None:
        html = _render()
        self.assertIn("data-cover-zoom", html)
        self.assertIn("data-zoom-src", html)

    def test_slots_present(self) -> None:
        """多图改成槽位: 当前封面 + 加号。"""
        html = _render()
        self.assertIn("data-cover-slots", html)
        self.assertIn("data-cover-add", html, "要有加号格")

    def test_js_registered(self) -> None:
        js = _js()
        self.assertIn("function setupCoverGallery(", js)
        self.assertIn("setupCoverGallery();", js)

    def test_multi_upload(self) -> None:
        js = _js()
        start = js.find("function setupCoverGallery(")
        end = js.find("\n  /*", start + 10)
        block = js[start:end if end > 0 else start + 5000]
        self.assertIn("files.length", block, "要支持多张")
        self.assertIn("FormData", block)

    def test_lightbox_css(self) -> None:
        css = _css()
        self.assertIn(".cover-lightbox", css)
        self.assertIn(".cover-slots", css)
        self.assertIn(".cover-slot-add", css)


class TestTrackRowOrder(unittest.TestCase):
    """曲目表: 排序把手最前 + 时长列回来。"""

    def setUp(self) -> None:
        self.html = _render()

    def test_headers_order(self) -> None:
        m = re.search(r"<thead><tr>([\s\S]*?)</tr></thead>", self.html)
        self.assertIsNotNone(m)
        heads = re.findall(r"<th>([^<]*)</th>", m.group(1))
        self.assertEqual(heads[0], "排序", "拖动把手在最前")
        self.assertIn("时长", heads)

    def test_duration_input(self) -> None:
        self.assertIn('name="track_duration"', self.html)

    def test_row_cells_order(self) -> None:
        m = re.search(r'<tr class="track-row"([\s\S]*?)</tr>', self.html)
        self.assertIsNotNone(m)
        labels = re.findall(r'data-label="([^"]*)"', m.group(1))
        self.assertEqual(labels[0], "排序")


class TestEqualHeight(unittest.TestCase):
    """制作人员与外链同一行等高。"""

    def test_css_two_columns(self) -> None:
        """卡片左右两列, 同一行等高(align-items: stretch)。"""
        css = _css()
        m = re.search(r"\.edit-cards \{([^}]*)\}", css)
        self.assertIsNotNone(m)
        body = m.group(1)
        self.assertIn("grid-template-columns: repeat(2", body)
        self.assertIn("align-items: stretch", body)

    def test_body_flex(self) -> None:
        css = _css()
        self.assertIn(".edit-card-credits .edit-card-body", css)
        self.assertIn(".edit-card-links .edit-card-body", css)

    def test_rows_flex_grow(self) -> None:
        css = _css()
        self.assertRegex(
            css,
            r"\.edit-card-credits \.edit-card-body \.rows,\s*"
            r"\.edit-card-links \.edit-card-body \.rows \{ flex: 1 1 auto; \}")


class TestAllSetupRegistered(unittest.TestCase):
    def test_boot_registers_everything(self) -> None:
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
        self.assertEqual(missing, [], f"未在 boot() 注册: {missing}")


if __name__ == "__main__":
    unittest.main()
