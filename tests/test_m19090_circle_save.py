"""用户 m19090: aslovt 提交的新建社团未能保存.

根因: 新建社团页的「活动类型」是自由文本框, 提示写「同人社团 / 企业 等」,
用户照着填 -> ``Circle.__post_init__`` 的 ``normalize_activity_type`` 抛
``ValueError: 无法识别的活动类型`` -> 整条社团没落库, 用户只看到"未能保存"
(真实原因只进了日志)。

这一组测试盯三件事:
1. 认不出来的活动类型**原样保留**, 不再让整次保存失败;
2. 新建/编辑表单把它渲染成**下拉**(只有 东方 / 原创 / 两者 / 未填);
3. 兜底页把真实原因写给用户看; 访问日志记得住 POST 与状态码。
"""

from __future__ import annotations

import re
import shutil
import time
import unittest

from dddb import models
from dddb.db import Store
from dddb.public import auth, templates as T
from dddb.public import site as site_mod
from dddb.public.users import UserStore
from tests import _tmp


def _ctx() -> T.SiteContext:
    return T.SiteContext(lang="zh", csrf_token="tk", is_editor=True)


class TestActivityTypeTolerated(unittest.TestCase):
    """``normalize_activity_type`` 对未知值降级而不是抛异常."""

    def test_known_aliases_still_normalized(self) -> None:
        for raw, want in (("东方", "touhou"), ("Touhou Project", "touhou"),
                          ("原创", "original"), ("两者", "both"), ("", "")):
            self.assertEqual(want, models.normalize_activity_type(raw), raw)

    def test_unknown_value_is_kept_as_is(self) -> None:
        self.assertEqual("同人社团", models.normalize_activity_type("同人社团"))
        self.assertEqual("企业", models.normalize_activity_type("企业"))

    def test_circle_survives_unknown_activity_type(self) -> None:
        circle = models.Circle.from_dict({"name": "某社团", "activity_type": "同人社团"})
        self.assertEqual("同人社团", circle.activity_type)
        # 显示时走 LABELS, 认不得就显示原文
        self.assertEqual("同人社团", circle.activity_label)

    def test_upsert_circle_no_longer_raises(self) -> None:
        tmp = _tmp.temp_path("m19090-upsert")
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        store = Store(tmp / "dddb.sqlite3")
        self.addCleanup(store.close)
        where = store.upsert_circle(models.Circle.from_dict(
            {"name": "某社团", "activity_type": "同人社团"}))
        self.assertEqual("created", where)
        self.assertEqual(1, len(store.list_circles()))


class TestCircleFormsUseSelect(unittest.TestCase):
    """两个入口都渲染成下拉, 选项就是那四个."""

    def _options(self, html: str) -> list[str]:
        match = re.search(r'<select name="activity_type">(.*?)</select>', html, re.S)
        self.assertIsNotNone(match, "活动类型应该是 <select>")
        return re.findall(r'<option value="([^"]*)"', match.group(1))

    def test_new_page_has_select(self) -> None:
        html = T.new_entity_page(_ctx(), "circle")
        self.assertNotIn('name="activity_type" type="text"', html)
        self.assertEqual(["", "touhou", "original", "both"], self._options(html))
        # 旧提示会引导用户填错, 必须消失
        self.assertNotIn("同人社团 / 企业", html)

    def test_edit_page_has_select(self) -> None:
        html = T.edit_profile_page(_ctx(), "circle", "某社团",
                                   values={"name": "某社团", "activity_type": "touhou"})
        self.assertEqual(["", "touhou", "original", "both"], self._options(html))
        self.assertIn('<option value="touhou" selected>', html)

    def test_edit_page_keeps_legacy_value_selectable(self) -> None:
        """库里存着旧自由文本时不能被悄悄改成「未填」."""
        html = T.edit_profile_page(_ctx(), "circle", "某社团",
                                   values={"name": "某社团", "activity_type": "同人社团"})
        self.assertIn('<option value="同人社团" selected>', html)


class _SiteCase(unittest.TestCase):
    """真起一个 PublicSite(临时库), 用编辑者身份提交表单."""

    def setUp(self) -> None:
        self.tmp = _tmp.temp_path("m19090-site")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.db_path = self.tmp / "dddb.sqlite3"
        Store(self.db_path).close()
        self.users_path = self.tmp / "users.sqlite3"
        users = UserStore(self.users_path)
        ok, message = users.create_user("aslovt", "pw-123456", role="editor")
        self.assertTrue(ok, message)
        self.uid = int((users.get("aslovt") or {}).get("id") or 1)
        self.site = site_mod.build_site(
            None, root=self.tmp, db_path=self.db_path, users_db=self.users_path,
            sessions_db=self.tmp / "sessions.sqlite3",
            covers_dir=self.tmp / "covers",
        )
        self.addCleanup(self.site.close)
        sess = auth.Session(sid="s1", user_id=self.uid, username="aslovt",
                            role="editor", expires_at=time.time() + 3600)
        state = auth.AuthState(session=sess)
        self.site.sessions.state = lambda cookies: state
        self.site.same_origin = lambda *a, **k: (True, "")
        self.site.sessions.signer.check = lambda b, g: True

    def post(self, path: str, fields: dict[str, str]):
        body = "&".join(f"{k}={v}" for k, v in
                        [("csrf_token", "tk"), *fields.items()]).encode()
        return self.site.handle(
            "POST", path, body=body,
            headers={"content-type": "application/x-www-form-urlencoded",
                     "origin": "http://x", "host": "x", "referer": "http://x/"})


class TestNewCircleSaves(_SiteCase):
    def test_free_text_activity_type_still_saves(self) -> None:
        """复现 aslovt 的情形: 填「同人社团」也必须能存下来."""
        resp = self.post("/new/circle", {"name": "TestCircle",
                                         "activity_type": "同人社团"})
        self.assertEqual(303, resp.status, "新建社团应该 303 跳详情页")
        store = Store(self.db_path)
        try:
            circle = store.get_circle("TestCircle")
        finally:
            store.close()
        self.assertIsNotNone(circle, "社团没落库")
        self.assertEqual("同人社团", circle.activity_type)

    def test_select_value_saves_as_key(self) -> None:
        resp = self.post("/new/circle", {"name": "TouhouCircle",
                                         "activity_type": "touhou"})
        self.assertEqual(303, resp.status)
        store = Store(self.db_path)
        try:
            circle = store.get_circle("TouhouCircle")
        finally:
            store.close()
        self.assertIsNotNone(circle)
        self.assertEqual("touhou", circle.activity_type)
        self.assertEqual("东方", circle.activity_label)

    def test_circle_without_activity_type_saves(self) -> None:
        resp = self.post("/new/circle", {"name": "BareCircle", "activity_type": ""})
        self.assertEqual(303, resp.status)
        store = Store(self.db_path)
        try:
            self.assertIsNotNone(store.get_circle("BareCircle"))
        finally:
            store.close()


class TestVisitLogKeepsMethodAndStatus(_SiteCase):
    """用户 m19090: 保存失败在看板上一点痕迹都没有(一律记成 GET)."""

    def test_post_is_recorded_with_status(self) -> None:
        self.post("/new/circle", {"name": "LoggedCircle", "activity_type": "touhou"})
        import sqlite3
        con = sqlite3.connect(self.tmp / "site_visits.sqlite3")
        try:
            rows = con.execute(
                "SELECT method, status, path, username FROM site_visits"
            ).fetchall()
        finally:
            con.close()
        self.assertTrue(rows, "没有访问记录")
        self.assertIn(("POST", 303, "/new/circle", "aslovt"), rows)


class TestFailurePageShowsReason(_SiteCase):
    def test_reason_surfaces_on_fallback_page(self) -> None:
        """兜底页要把真实原因写给用户, 而不是只说"没有保存成功"."""
        resp = self.site._new_entity_failed(
            "circle", {"name": "X"}, self.site.sessions.state([]),
            [], ValueError("某某字段不合法"))
        html = resp.body.decode("utf-8")
        self.assertIn("某某字段不合法", html)
        self.assertIn("没有保存成功", html)


if __name__ == "__main__":
    unittest.main()
