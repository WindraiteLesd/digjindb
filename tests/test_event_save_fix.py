"""会次保存必须生效 + 描述字段去掉 + 必填红星 + 季节按发售日.

用户报告:
    ① "对展会会次的修改无法生效，内容不保存"
       -> 根因: ``_has_logo_field = "logo_url" in values``, 而表单早已改成
          上传(字段名 ``logo``), 该条件恒假; ``kind`` 也被移出表单但仍在读;
          ``logo_local`` 直接取 ``_prev``, 上传的图永远写不进去。
    ② "去掉每个词条后的例如描述"
    ③ "必填项全站同步用红色*表示而非文字"
    ④ "会次标签按会次顺位自动填入...单数为春偶数为秋" —— 用户最终选择
       **用发售日月份判断**(库里 M3-57 春 / M3-46 春 与奇偶规则矛盾)
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests._tmp import temp_path

ROOT = Path(__file__).resolve().parents[1]


class TestEventSaveWorks(unittest.TestCase):
    """会次编辑真的能存进去(用户报的"不保存")."""

    def setUp(self) -> None:
        from dddb.db import Store
        from dddb.public.site import build_site

        self.tmp = temp_path("event-save")
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True)
        self.db = self.data / "dddb.sqlite3"
        self.covers = self.data / "covers"
        self.covers.mkdir()
        store = Store(self.db)
        store.save_series("Music Media-Mix Market", abbr="M3",
                          full_name="Music Media-Mix Market")
        store.save_event("M3-2026秋",
                         series="Music Media-Mix Market",
                         vol=58, vol_label="M3-58")
        store.close()
        self.site = build_site(
            None, root=self.tmp, db_path=self.db,
            users_db=self.data / "u.sqlite3",
            sessions_db=self.data / "s.sqlite3", covers_dir=self.covers)

    def tearDown(self) -> None:
        import shutil

        self.site.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self):
        from dddb.public import auth

        st = auth.AuthState()
        st.session = auth.Session(sid="t", user_id=1, username="e",
                                  role="editor")
        return st

    def test_fields_are_saved(self) -> None:
        """改年份/季节/起止日期/标签/链接, 全都要落库."""
        from dddb.db import Store

        self.site.can_edit = lambda s: True          # type: ignore
        values = {
            "name": ["M3-2026秋"],
            "series": ["Music Media-Mix Market"],
            "vol_label": ["M3-58"],
            "year": ["2026"],
            "season": ["秋"],
            "start_date": ["2026-10-25"],
            "end_date": ["2026-10-26"],
            "tags": ["东方、同人"],
            "links": ["官网: https://m3.example.com/"],
        }
        self.site._do_entity_update("event", "M3-2026秋", values,
                                    self._state(), [])
        store = Store(self.db)
        got = next(e for e in store.list_events()
                   if e["name"] == "M3-2026秋")
        store.close()
        self.assertEqual(str(got["year"]), "2026", "年份没保存")
        self.assertEqual(got["season"], "秋", "季节没保存")
        self.assertEqual(got["start_date"], "2026-10-25", "开始日期没保存")
        self.assertEqual(got["end_date"], "2026-10-26", "结束日期没保存")
        self.assertIn("东方", got["tags"], "标签没保存")
        self.assertIn("官网", got["links"], "链接没保存")

    def test_season_autofilled_from_date(self) -> None:
        """没填季节时, 按发售日月份自动填."""
        from dddb.db import Store

        self.site.can_edit = lambda s: True          # type: ignore
        self.site._do_entity_update("event", "M3-2026秋", {
            "name": ["M3-2026秋"],
            "series": ["Music Media-Mix Market"],
            "vol_label": ["M3-58"],
            "start_date": ["2026-09-20"],       # 9 月 -> 秋
            "year": [""], "season": [""],
            "tags": [""], "links": [""],
        }, self._state(), [])
        store = Store(self.db)
        got = next(e for e in store.list_events()
                   if e["name"] == "M3-2026秋")
        store.close()
        self.assertEqual(got["season"], "秋", "季节应按时发售日自动填")
        self.assertEqual(str(got["year"]), "2026", "年份应自动填")

    def test_vol_label_autofilled_from_vol(self) -> None:
        """没填会次标签时, 按系列 + 会次号自动拼."""
        from dddb.db import Store

        self.site.can_edit = lambda s: True          # type: ignore
        self.site._do_entity_update("event", "M3-2026秋", {
            "name": ["M3-2026秋"],
            "series": ["Music Media-Mix Market"],
            "vol_label": [""],
            "start_date": ["2026-09-20"],
            "year": [""], "season": [""],
            "tags": [""], "links": [""],
        }, self._state(), [])
        store = Store(self.db)
        got = next(e for e in store.list_events()
                   if e["name"] == "M3-2026秋")
        store.close()
        self.assertTrue(got["vol_label"], "会次标签应自动填")
        self.assertEqual(str(got["vol_label"]), "M3-58")

    def test_kind_preserved_not_blanked(self) -> None:
        """kind 已从表单移除, 保存时不能把库里原值清空."""
        from dddb.db import Store

        store = Store(self.db)
        store.save_event("M3-2026秋", kind="m3",
                         series="Music Media-Mix Market",
                         vol=58, vol_label="M3-58")
        store.close()
        self.site.can_edit = lambda s: True          # type: ignore
        self.site._do_entity_update("event", "M3-2026秋", {
            "name": ["M3-2026秋"],
            "series": ["Music Media-Mix Market"],
            "vol_label": ["M3-58"], "year": ["2026"], "season": ["秋"],
            "tags": [""], "links": [""],
        }, self._state(), [])
        store = Store(self.db)
        got = next(e for e in store.list_events()
                   if e["name"] == "M3-2026秋")
        store.close()
        self.assertEqual(got["kind"], "m3", "kind 被清空了")


class TestNoDescriptionFields(unittest.TestCase):
    """描述类字段从表单里去掉。"""

    def test_removed(self) -> None:
        from dddb.public import templates as T

        # 社团的 # description 是例外: 用户 m19886 的简介就是被「表单里
        # 没有这个字段 + 整条替换」清空的, 所以它必须回到表单里。
        expect_gone = {"album": "description", "artist": "bio", "tag": "note"}
        for kind, field in expect_gone.items():
            with self.subTest(kind=kind):
                names = [f[0] for f in T.NEW_ENTITY_SPECS[kind][1]]
                self.assertNotIn(field, names, f"{kind} 还留着 {field}")
        circle_names = [f[0] for f in T.NEW_ENTITY_SPECS["circle"][1]]
        self.assertIn("description", circle_names,
                      "社团字段表要有简介(用户 m19886)")


class TestRequiredStar(unittest.TestCase):
    """必填用红色 * , 不用「必填」两个字。"""

    def _ctx(self):
        from dddb.public import templates as T

        return T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                             user={"id": 1, "username": "e", "role": "editor"})

    def test_no_required_text(self) -> None:
        from dddb.public import templates as T

        for kind in ("album", "artist", "circle", "event", "tag"):
            with self.subTest(kind=kind):
                html = T.new_entity_page(self._ctx(), kind)
                self.assertNotIn("必填", html, f"{kind} 还有「必填」文字")

    def test_has_star(self) -> None:
        from dddb.public import templates as T

        for kind in ("album", "artist", "circle", "event", "tag"):
            with self.subTest(kind=kind):
                html = T.new_entity_page(self._ctx(), kind)
                self.assertIn('class="req"', html, f"{kind} 没有红星")

    def test_star_not_duplicated(self) -> None:
        from dddb.public import templates as T

        html = T.new_entity_page(self._ctx(), "event")
        self.assertEqual(html.count('class="req"'), 1, "星号重复了")

    def test_edit_page_has_star(self) -> None:
        from dddb.public import templates as T

        html = T.edit_profile_page(
            self._ctx(), "event", "M3-2026秋",
            values={"name": "M3-2026秋"})
        self.assertIn('class="req"', html)
        self.assertNotIn("必填", html)

    def test_css_red(self) -> None:
        css = (ROOT / "dddb/public/static/public.css").read_text(
            encoding="utf-8")
        self.assertIn(".req {", css)
        block = css.split(".req {")[1].split("}")[0]
        self.assertIn("color", block, "必须是红色")


class TestSeasonByDate(unittest.TestCase):
    """季节按发售日月份判断(用户最终选择)."""

    def test_m3_real_data(self) -> None:
        from dddb.public.eventseries import season_from_date, year_from_date

        # 库里真实数据: M3-57 = 2026 春, M3-58 = 2026 秋
        self.assertEqual(season_from_date("2026-04-26"), "春")
        self.assertEqual(year_from_date("2026-04-26"), 2026)
        self.assertEqual(season_from_date("2026-9-20"), "秋")
        self.assertEqual(year_from_date("2026-9-20"), 2026)

    def test_other_months(self) -> None:
        from dddb.public.eventseries import season_from_date

        self.assertEqual(season_from_date("2026-3-1"), "春")
        self.assertEqual(season_from_date("2026-7-31"), "春")
        self.assertEqual(season_from_date("2026-8-1"), "秋")
        self.assertEqual(season_from_date("2026-12-31"), "秋")
        # 1-2 月 -> 冬(冬コミ常拖到 1 月)
        self.assertEqual(season_from_date("2026-1-15"), "冬")
        self.assertEqual(season_from_date("2026-2-28"), "冬")

    def test_bad_input(self) -> None:
        from dddb.public.eventseries import season_from_date, year_from_date

        self.assertEqual(season_from_date(""), "")
        self.assertEqual(season_from_date("坏数据"), "")
        self.assertEqual(year_from_date(""), 0)

    def test_label_from_vol(self) -> None:
        from dddb.public.eventseries import label_from_vol

        self.assertEqual(label_from_vol("Music Media-Mix Market", 58), "M3-58")
        self.assertEqual(label_from_vol("Comic Market", 108), "C108")
        self.assertEqual(label_from_vol("未知系列", 5), "")


if __name__ == "__main__":
    unittest.main()
