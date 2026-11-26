"""测试: 编辑界面重构(卡片布局/出品信息/品番/展会自动匹配/布局偏好)."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _album(**overrides):
    base = {
        "key": "d", "title": "T", "title_original": "T", "title_alt": "",
        "romaji": "", "circle": "某社团", "artists": ["甲", "乙"],
        "credits": [{"role": "作曲", "name": "丙"}],
        "urls": [{"key": "x", "url": "https://a.com/"}],
        "tags": ["tag1"], "genres": ["Trance"], "tracks": [],
        "date": "2026-10-25", "event": "M3-2026秋",
        "catalog_no": "ABC-001", "price": "1000", "company": "某公司",
        "hall": "", "booth": "A-01", "description": "", "missing": [],
        "track_count": 0,
    }
    base.update(overrides)
    return base


def _render(layout=None, **album_overrides):
    from dddb.public import templates as T

    ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                        user={"id": 1, "username": "ed", "role": "editor"},
                        edit_layout=layout)
    album = _album(**album_overrides)
    return T.edit_album_page(ctx, album=album,
                             values=T.edit_values_from_album(album))


class TestCardLayout(unittest.TestCase):
    """卡片结构: 左上封面&标题 / 右上出品信息 / 其余可排。"""

    def setUp(self) -> None:
        self.html = _render()

    def test_cards_present(self) -> None:
        for name in ("cover", "info", "category", "description",
                     "credits", "links"):
            self.assertIn('data-card="{}"'.format(name), self.html,
                          f"缺卡片 {name}")

    def test_default_order(self) -> None:
        order = re.findall(r'data-card="(\w+)"', self.html)
        self.assertEqual(order[:2], ["cover", "info"],
                         "默认第一张是封面&标题, 第二张是出品信息")

    def test_cover_card_has_title(self) -> None:
        m = re.search(r'data-card="cover"([\s\S]*?)</section>', self.html)
        self.assertIsNotNone(m)
        inner = m.group(1)
        self.assertIn("cover-input", inner, "封面在该卡片")
        self.assertIn('name="title_original"', inner, "标题也在该卡片")

    def test_tracks_card_present(self) -> None:
        """曲目表是独立卡片(在网格之外, 整行)。"""
        self.assertIn('data-card="tracks"', self.html)
        self.assertIn("edit-tracks-card", self.html)

    def test_no_drag_or_size_switch(self) -> None:
        """布局固定: 不再有拖动把手 / 长卡半卡切换按钮。"""
        self.assertNotIn("edit-card-grip", self.html)
        self.assertNotIn("data-card-size=", self.html)
        self.assertNotIn("draggable=", self.html.replace(
            'draggable="true" title="按住拖动排序"', ""), "卡片不该可拖动")


class TestInfoCard(unittest.TestCase):
    """出品信息: 艺人移除 / 社团+发行公司 / 品番 / 发售日. """

    def setUp(self) -> None:
        self.html = _render()
        m = re.search(r'data-card="info"([\s\S]*?)</section>', self.html)
        self.info = m.group(1) if m else ""

    def test_artist_removed(self) -> None:
        """艺人已移到制作人员, 出品信息里不该有。"""
        self.assertNotIn("艺人", self.info)
        self.assertNotIn('data-name="artists"', self.info)

    def test_circle_and_company_together(self) -> None:
        # 用户 m16568: 发行公司输入框去掉了(同人展侧把社团和发行公司合并), 换成制作人
        self.assertIn("社团 / 出品方", self.info)
        self.assertIn("制作人", self.info)
        self.assertNotIn("发行公司", self.info)

    def test_catalog_label_is_binhang(self) -> None:
        """编号 -> 品番。"""
        self.assertIn("品番", self.info)
        self.assertNotIn("编号", self.info)

    def test_release_date_present(self) -> None:
        self.assertIn('name="release_date"', self.info)

    def test_artist_in_credits_card(self) -> None:
        """艺人在制作人员卡片里, 以 chip 按钮 + 隐藏字段形式存在。"""
        m = re.search(r'data-card="credits"([\s\S]*?)</section>', self.html)
        self.assertIsNotNone(m)
        inner = m.group(1)
        self.assertIn("person-chip", inner, "艺人要显示为按钮")
        self.assertIn('name="artists"', inner, "要有隐藏字段提交")
        self.assertIn("people-suggest-list", inner, "要有艺人候选")
        # 上方只读, 没有搜索框(用户要求 m11474)
        self.assertNotIn("people-picker", inner)


class TestCustomLayout(unittest.TestCase):
    """编辑者可自定义卡片顺序与大小."""

    def test_order_is_fixed(self) -> None:
        """顺序固定, 传 layout 参数也不影响(布局已写死)。"""
        html = _render(layout={"order": ["info", "cover"], "sizes": {}})
        order = re.findall(r'data-card="(\w+)"', html)
        self.assertEqual(order[:2], ["cover", "info"],
                         "顺序固定: 封面&标题在前, 出品信息在后")

    def test_no_size_attribute(self) -> None:
        html = _render(layout={"sizes": {"info": "wide"}})
        self.assertNotIn("data-size=", html)

    def test_no_layout_payload(self) -> None:
        """页面不再携带布局 JSON / 保存端点。"""
        html = _render()
        self.assertNotIn("data-layout", html)


class TestCoverRequiredBug(unittest.TestCase):
    """曲风按回车不该提示"请上传封面".

    根因: 封面的 file input 带 required, 但上传已改成独立 JS 流程,
    主表单原生校验会跳到它 -> 在任意输入框回车都提示要传封面。
    """

    def setUp(self) -> None:
        self.html = _render()

    def test_no_required_on_file_inputs(self) -> None:
        files = re.findall(r'<input[^>]*type="file"[^>]*>', self.html)
        self.assertGreater(len(files), 0, "找不到 file 输入框")
        for item in files:
            self.assertNotIn("required", item,
                             f"file 输入框不该有 required: {item[:90]}")

    def test_only_title_is_required(self) -> None:
        req = re.findall(r'<input[^>]*required[^>]*>', self.html)
        names = []
        for item in req:
            m = re.search(r'name="([^"]*)"', item)
            names.append(m.group(1) if m else "?")
        self.assertEqual(names, ["title_original"],
                         "只有标题(原题)是必填")


class TestEventAutoDate(unittest.TestCase):
    """选展会后自动匹配发售日."""

    def setUp(self) -> None:
        self.js = (ROOT / "dddb/public/static/public.js").read_text(
            encoding="utf-8")

    def test_js_registered(self) -> None:
        self.assertIn("function setupEventAutoDate(", self.js)
        self.assertIn("setupEventAutoDate();", self.js)

    def test_fetches_event_date(self) -> None:
        start = self.js.find("function setupEventAutoDate(")
        end = self.js.find("\n  /*", start + 10)
        block = self.js[start:end if end > 0 else start + 3000]
        self.assertIn("/event-date", block)

    def test_uses_visible_bind_input(self) -> None:
        """_bind 的可见输入框是 .bind-input[data-name=xxx]。"""
        start = self.js.find("function setupEventAutoDate(")
        end = self.js.find("\n  /*", start + 10)
        block = self.js[start:end if end > 0 else start + 3000]
        self.assertIn(".bind-input", block)
        self.assertIn('data-name="event"', block)

    def test_does_not_overwrite_manual_date(self) -> None:
        """手填的日期不能被自动填覆盖。"""
        start = self.js.find("function setupEventAutoDate(")
        end = self.js.find("\n  /*", start + 10)
        block = self.js[start:end if end > 0 else start + 3000]
        self.assertIn("lastAuto", block)

    def test_backend_endpoint(self) -> None:
        from dddb.public import site as SITE

        self.assertTrue(hasattr(SITE.PublicSite, "event_date_json"))
        data = json.loads(SITE.PublicSite.event_date_json(
            object.__new__(SITE.PublicSite), "name=M3-2026%E7%A7%8B"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["date"], "2026-10-25")

    def test_unknown_event_returns_not_ok(self) -> None:
        from dddb.public import site as SITE

        data = json.loads(SITE.PublicSite.event_date_json(
            object.__new__(SITE.PublicSite), "name=不存在的展会"))
        self.assertFalse(data["ok"])
        self.assertEqual(data["date"], "")


class TestLayoutPreferenceStorage(unittest.TestCase):
    """布局偏好存到 user_prefs 表."""

    def test_roundtrip(self) -> None:
        from tests._tmp import temp_dir

        ctx = temp_dir("layout-")
        root = Path(ctx.__enter__())
        try:
            from dddb.public.profile import ProfileStore

            store = ProfileStore(str(root / "p.sqlite3"))
            self.assertIn("user_prefs", store.table_names())
            payload = json.dumps({"order": ["info", "cover"], "sizes": {}})
            self.assertTrue(store.set_pref(35, "edit.layout", payload))
            self.assertEqual(store.get_pref(35, "edit.layout"), payload)
            # upsert
            store.set_pref(35, "edit.layout", '{"order":[]}')
            self.assertEqual(store.get_pref(35, "edit.layout"), '{"order":[]}')
            # 用户隔离
            self.assertEqual(store.get_pref(1, "edit.layout", "none"), "none")
            store.close()
        finally:
            ctx.__exit__(None, None, None)

    def test_rejects_bad_input(self) -> None:
        from tests._tmp import temp_dir

        ctx = temp_dir("layout2-")
        root = Path(ctx.__enter__())
        try:
            from dddb.public.profile import ProfileStore

            store = ProfileStore(str(root / "p.sqlite3"))
            self.assertFalse(store.set_pref(0, "k", "v"))
            self.assertFalse(store.set_pref(35, "", "v"))
            store.close()
        finally:
            ctx.__exit__(None, None, None)


class TestJsRegistered(unittest.TestCase):
    def test_all_setup_functions_in_boot(self) -> None:
        js = (ROOT / "dddb/public/static/public.js").read_text(encoding="utf-8")
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
