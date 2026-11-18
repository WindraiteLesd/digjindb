"""展会排序: 时间 / 专辑数量, 正倒序, 默认最新.

用户要求: "展会展示排序：时间顺序和专辑数量的正倒序，默认按最新排序"
"""

from __future__ import annotations

import unittest
from pathlib import Path

from tests._tmp import temp_path

ROOT = Path(__file__).resolve().parents[1]

#: 构造数据: 时间与数量刻意不同序, 才能区分两个维度
SAMPLE = [
    {"name": "M3", "latest": "2026-10-25", "count": 2161},
    {"name": "Comic Market", "latest": "2024-08-11", "count": 40},
    {"name": "例大祭", "latest": "2023-05-07", "count": 2},
]


def _names(items) -> list[str]:
    return [x["name"] for x in items]


class TestSortLogic(unittest.TestCase):
    def _sort(self, sort: str, order: str, extra=None):
        from dddb.public.data import _sort_series

        data = [dict(x) for x in SAMPLE] + [dict(x) for x in (extra or [])]
        return _sort_series(data, sort=sort, order=order)

    def test_time_desc_default(self) -> None:
        self.assertEqual(_names(self._sort("time", "desc")),
                         ["M3", "Comic Market", "例大祭"])

    def test_time_asc(self) -> None:
        self.assertEqual(_names(self._sort("time", "asc")),
                         ["例大祭", "Comic Market", "M3"])

    def test_count_desc(self) -> None:
        self.assertEqual(_names(self._sort("count", "desc")),
                         ["M3", "Comic Market", "例大祭"])

    def test_count_asc(self) -> None:
        self.assertEqual(_names(self._sort("count", "asc")),
                         ["例大祭", "Comic Market", "M3"])

    def test_bad_values_fall_back(self) -> None:
        """乱传参数不能崩, 落到默认(time/desc)。"""
        self.assertEqual(_names(self._sort("bogus", "bogus")),
                         ["M3", "Comic Market", "例大祭"])
        self.assertEqual(_names(self._sort("", "")),
                         ["M3", "Comic Market", "例大祭"])

    def test_undated_last_in_time_sort(self) -> None:
        """时间排序: 没日期的排最后(正序倒序都一样)。"""
        extra = [{"name": "无日期", "latest": "", "count": 5}]
        for order in ("asc", "desc"):
            with self.subTest(order=order):
                got = _names(self._sort("time", order, extra))
                self.assertEqual(got[-1], "无日期",
                                 "时间排序时无日期必须压最后")

    def test_undated_keeps_position_in_count_sort(self) -> None:
        """数量排序: 有没有日期**不影响**位置(5 张要排在 2 张前面)。"""
        extra = [{"name": "无日期", "latest": "", "count": 5}]
        asc = _names(self._sort("count", "asc", extra))
        self.assertEqual(asc.index("无日期"), 1,
                         "5 张应排在 2 张之后、40 张之前")
        desc = _names(self._sort("count", "desc", extra))
        self.assertEqual(desc.index("无日期"), 2)

    def test_stable_for_equal_values(self) -> None:
        """值相同时按名字兜底, 保证刷新不乱跳。"""
        from dddb.public.data import _sort_series

        data = [
            {"name": "B", "latest": "2026-01-01", "count": 10},
            {"name": "A", "latest": "2026-01-01", "count": 10},
        ]
        got = _names(_sort_series(data, sort="time", order="desc"))
        self.assertEqual(got, ["A", "B"])


class TestDateNormalization(unittest.TestCase):
    """日期必须同宽, 否则字符串比较会把 9 月排到 10 月后面。"""

    def test_zero_pad(self) -> None:
        from dddb.public.data import _norm_date

        self.assertEqual(_norm_date("2026-9-20"), "2026-09-20")
        self.assertEqual(_norm_date("2026-10-25"), "2026-10-25")
        self.assertEqual(_norm_date("2026/4/6"), "2026-04-06")
        self.assertEqual(_norm_date("2026"), "2026-01-01")
        self.assertEqual(_norm_date(""), "")

    def test_sort_correct_across_months(self) -> None:
        """归一化后 9 月必须排在 10 月之前。"""
        from dddb.public.data import _norm_date

        dates = ["2026-9-20", "2026-10-25", "2026-4-26"]
        self.assertEqual(sorted(dates, key=_norm_date),
                         ["2026-04-06".replace("04-06", "4-26"), "2026-9-20",
                          "2026-10-25"])

    def test_bad_date_untouched(self) -> None:
        from dddb.public.data import _norm_date

        self.assertEqual(_norm_date("坏数据"), "坏数据")


class TestSeriesData(unittest.TestCase):
    """event_series() 要给出 latest 并按参数排序。"""

    def setUp(self) -> None:
        from dddb.db import Store
        from dddb.models import Album
        from dddb.public.data import PublicData, ReadOnlyStore

        self.tmp = temp_path("series-sort")
        self.db = self.tmp / "t.sqlite3"
        store = Store(self.db)
        for series, abbr, event, date, n in (
            ("Music Media-Mix Market", "M3", "M3-2026秋", "2026-10-25", 3),
            ("Comic Market", "CM", "C108", "2024-08-11", 1),
        ):
            store.save_series(series, abbr=abbr, full_name=series)
            store.save_event(event, series=series, vol=1, vol_label="x")
            for i in range(n):
                store.upsert_album(Album.from_dict({
                    "key": "{}-{}".format(event, i),
                    "title": "T{}".format(i),
                    "title_original": "T{}".format(i),
                    "event": event, "release_date": date,
                    "source_ids": {}}))
        store.close()
        self.store = ReadOnlyStore(self.db)
        self.data = PublicData(self.store, root=self.tmp)

    def tearDown(self) -> None:
        import shutil

        self.store.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_latest_from_release_date(self) -> None:
        items = self.data.event_series()
        by = {x["name"]: x for x in items}
        self.assertEqual(by["Music Media-Mix Market"]["latest"], "2026-10-25")
        self.assertEqual(by["Comic Market"]["latest"], "2024-08-11")

    def test_default_is_newest_first(self) -> None:
        items = self.data.event_series()
        self.assertEqual(items[0]["name"], "Music Media-Mix Market",
                         "默认应按最新排序")

    def test_count_order(self) -> None:
        items = self.data.event_series(sort="count", order="desc")
        self.assertEqual(items[0]["count"], 3)
        asc = self.data.event_series(sort="count", order="asc")
        self.assertEqual(asc[0]["count"], 1)

    def test_editions_have_date(self) -> None:
        items = self.data.event_series()
        ed = items[0]["editions"][0]
        self.assertEqual(ed["date"], "2026-10-25")


class TestSortUi(unittest.TestCase):
    def _ctx(self):
        from dddb.public import templates as T

        return T.SiteContext(csrf_token="t", lang="zh")

    def _html(self, sort="time", order="desc"):
        from dddb.public import templates as T

        items = [{"name": "M3", "abbr": "M3", "full_name": "M3",
                  "logo": "", "has_logo": False, "count": 10,
                  "latest": "2026-10-25", "editions": []}]
        return T.events_index_page(self._ctx(), events=[], kinds=[],
                                   series=items, sort=sort, order=order)

    def test_sort_bar_present(self) -> None:
        html = self._html()
        self.assertIn("sort-bar", html)
        self.assertIn("时间", html)
        self.assertIn("专辑数量", html)

    def test_active_dimension_marked(self) -> None:
        html = self._html("time", "desc")
        self.assertIn("sort-pill is-active", html)

    def test_click_active_flips_order(self) -> None:
        """点当前维度应翻转方向。"""
        html = self._html("time", "desc")
        self.assertIn("/events?sort=time&order=asc", html)

    def test_click_other_switches_dimension(self) -> None:
        """点另一维度应切过去(默认倒序)。"""
        html = self._html("time", "desc")
        self.assertIn("/events?sort=count&order=desc", html)

    def test_arrow_indicator(self) -> None:
        self.assertIn("↓", self._html("time", "desc"))
        self.assertIn("↑", self._html("time", "asc"))

    def test_css_present(self) -> None:
        css = (ROOT / "dddb/public/static/public.css").read_text(
            encoding="utf-8")
        for token in (".sort-bar", ".sort-pill", ".sort-pill.is-active"):
            self.assertIn(token, css, f"缺样式 {token}")


class TestRouteParams(unittest.TestCase):
    """page_events 要解析 ?sort / ?order。"""

    def test_signature_accepts_params(self) -> None:
        from dddb.public import site as SITE

        import inspect

        sig = inspect.signature(SITE.PublicSite.page_events)
        self.assertIn("params", sig.parameters)

    def test_calls_data_with_sort(self) -> None:
        src = (ROOT / "dddb/public/site.py").read_text(encoding="utf-8")
        self.assertIn("sort=sort", src)
        self.assertIn('sort not in ("time", "count")', src)


if __name__ == "__main__":
    unittest.main()
