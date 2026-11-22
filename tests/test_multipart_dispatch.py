"""真实 HTTP 提交必须能保存.

真实事故:
    用户在 ``/event/M3-2026秋/edit`` 提交, 页面显示成功但数据全丢。

根因:
    ``_handle_write`` 用 ``form_values(body)`` 解析请求体 —— 那只认
    ``application/x-www-form-urlencoded``。而 ``edit_profile_page`` 生成的表单是
    ``enctype="multipart/form-data"``(会次要传图), 浏览器一定走 multipart,
    于是 ``values`` **全空**, 保存时每个字段都读成空串。

    之前我只测了直接调 ``_do_entity_update(values={...})`` —— 绕过了
    HTTP 层, 所以测试全绿而线上仍然丢数据。

这个测试走**完整 HTTP**, 和浏览器一样发 multipart。
"""

from __future__ import annotations

import base64
import re
import unittest
from pathlib import Path

from tests._tmp import temp_path

ROOT = Path(__file__).resolve().parents[1]

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAE"
    "hQGAhKmMIQAAAABJRU5ErkJggg==")

B = "----multiparttest"


def _multipart(fields: dict[str, str],
               files: dict[str, tuple[str, str, bytes]] | None = None
               ) -> tuple[dict, bytes]:
    parts = []
    for k, v in fields.items():
        parts.append(
            ('--{}\r\nContent-Disposition: form-data; name="{}"\r\n\r\n{}'
             .format(B, k, v)).encode())
    for k, (fn, ct, data) in (files or {}).items():
        parts.append(
            ('--{}\r\nContent-Disposition: form-data; name="{}";'
             ' filename="{}"\r\nContent-Type: {}\r\n\r\n'
             .format(B, k, fn, ct)).encode() + data)
    body = b"\r\n".join(parts) + b"\r\n--" + B.encode() + b"--\r\n"
    return ({"content-type": "multipart/form-data; boundary={}".format(B)},
            body)


class TestMultipartParsedAtDispatch(unittest.TestCase):
    """分发前就要把 multipart 解析进 values。"""

    def setUp(self) -> None:
        from dddb.db import Store
        from dddb.public.site import build_site

        self.tmp = temp_path("mp-dispatch")
        self.data = self.tmp / "data"
        self.data.mkdir(parents=True)
        self.db = self.data / "dddb.sqlite3"
        self.covers = self.data / "covers"
        self.covers.mkdir()
        store = Store(self.db)
        store.save_series("Music Media-Mix Market", abbr="M3",
                          full_name="Music Media-Mix Market")
        store.save_event("M3-2026秋", series="Music Media-Mix Market",
                         vol=58, vol_label="M3-58")
        store.close()
        self.site = build_site(
            None, root=self.tmp, db_path=self.db,
            users_db=self.data / "u.sqlite3",
            sessions_db=self.data / "s.sqlite3", covers_dir=self.covers)
        self.site.can_edit = lambda s: True          # type: ignore

    def tearDown(self) -> None:
        import shutil

        if getattr(self, "_orig_check", None) is not None:
            self.site.sessions.signer.check = self._orig_check  # type: ignore
        self.site.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _state(self):
        from dddb.public import auth

        st = auth.AuthState()
        st.session = auth.Session(sid="t", user_id=1, username="e",
                                  role="editor")
        return st

    def test_multipart_reaches_handler(self) -> None:
        """关键: 走 handle() 而不是直接调 _do_entity_update。"""
        from dddb.db import Store

        headers, body = _multipart({
            "name": "M3-2026秋",
            "series": "Music Media-Mix Market",
            "vol_label": "M3-58",
            "year": "2026",
            "season": "秋",
            "start_date": "2026-09-20",
            "end_date": "2026-09-21",
            "tags": "东方、同人",
            "links": "官网: https://m3.example.com/",
            "csrf_token": "testtoken",
        }, {"logo": ("logo.png", "image/png", PNG)})

        # 绕过鉴权与 CSRF(只测"表单内容能否到达保存逻辑")
        st = self._state()
        st.csrf_token = "testtoken"
        self.site.sessions.state = lambda cookies: st            # type: ignore
        self.site.same_origin = lambda *a, **k: (True, "")       # type: ignore
        # CSRF 校验直接放行(该逻辑有独立测试覆盖)
        signer = self.site.sessions.signer
        self._orig_check = signer.check
        signer.check = lambda binding, given: True                # type: ignore

        resp = self.site.handle(
            "POST", "/event/M3-2026%E7%A7%8B/edit",
            headers={**headers, "host": "127.0.0.1:9",
                     "origin": "http://127.0.0.1:9"},
            body=body)
        self.assertLess(resp.status, 400, "提交被拒: {}".format(
            (resp.body or b"")[:300]))

        store = Store(self.db)
        got = next(e for e in store.list_events()
                   if e["name"] == "M3-2026秋")
        store.close()
        self.assertEqual(str(got["year"]), "2026",
                         "年份丢了 —— multipart 没解析到 values")
        self.assertEqual(got["season"], "秋", "季节丢了")
        self.assertEqual(got["start_date"], "2026-09-20", "开始日期丢了")
        self.assertEqual(got["end_date"], "2026-09-21", "结束日期丢了")
        self.assertIn("东方", got["tags"], "标签丢了")
        self.assertIn("官网", got["links"], "链接丢了")
        self.assertTrue(got["logo_local"], "图片没落盘")


class TestFormValuesParsesMultipart(unittest.TestCase):
    """form_values 只认 urlencoded —— 这就是需要统一补解析的原因。"""

    def test_form_values_alone_is_garbage(self) -> None:
        """form_values 对 multipart 会产出**垃圾键**(不是正确的字段)。

        这正是数据丢失的形态: 保存逻辑按 ``values["year"]`` 取,
        取不到 -> 读成空串 -> 把库里的值覆盖成空。
        """
        from dddb.public.site import form_values

        _headers, body = _multipart({"year": "2026", "season": "秋"})
        got = form_values(body)
        self.assertNotIn("year", got, "form_values 不该能正确解析 multipart")
        self.assertNotIn("season", got)
        # 它产出的是一堆畸形键(整段 boundary 被当成 key)
        self.assertTrue(got, "form_values 会产出垃圾键")

    def test_multipart_helper_works(self) -> None:
        from dddb.public.site import build_site

        tmp = temp_path("mp-helper")
        (tmp / "covers").mkdir(parents=True, exist_ok=True)
        from dddb.db import Store

        Store(tmp / "t.sqlite3").close()
        site = build_site(None, root=tmp, db_path=tmp / "t.sqlite3",
                          users_db=tmp / "u.sqlite3",
                          sessions_db=tmp / "s.sqlite3",
                          covers_dir=tmp / "covers")
        try:
            headers, body = _multipart({"year": "2026", "season": "秋"})
            got = site._values_from_multipart(headers, body)
            self.assertEqual(got["year"], ["2026"])
            self.assertEqual(got["season"], ["秋"])
        finally:
            site.close()
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)


class TestAllEditRoutesCovered(unittest.TestCase):
    """四个实体编辑路由都靠统一解析, 不需要各自补。"""

    def test_source_has_unified_parse(self) -> None:
        src = (ROOT / "dddb/public/site.py").read_text(encoding="utf-8")
        # 统一解析应在 _handle_write 里(分发前)
        idx = src.find("values = form_values(body)")
        self.assertGreater(idx, 0)
        window = src[idx:idx + 1200]
        self.assertIn("_values_from_multipart", window,
                      "分发前没有统一补 multipart 解析")

    def test_edit_routes_listed(self) -> None:
        src = (ROOT / "dddb/public/site.py").read_text(encoding="utf-8")
        self.assertIn(
            'if name in ("artist_edit", "circle_edit", "event_edit", '
            '"tag_edit"):', src)


if __name__ == "__main__":
    unittest.main()
