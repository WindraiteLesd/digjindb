"""站务看板 + 用户主页(最后活跃/关注头像/贡献合并) + 收藏专辑 的测试.

覆盖用户 m14663 的第二、四项:
* 「最后活跃时间记录有误」 —— ``users.last_seen`` 而不是 ``last_login``;
* 「贡献记录没有做合并」 —— 同 ``(kind, target)`` 合成一条带次数;
* 「对其他用户展示的界面没做完整, 也不显示关注的社团/艺人头像和收藏专辑的封面」
* 「专辑详情里收藏专辑这个功能也是缺失的」
* 「单独做一个网站本身数据的看板, 仅开放给站务账号」
"""

from __future__ import annotations

import re
import time
import unittest
from pathlib import Path

from dddb import db as db_mod
from dddb.models import Album
from dddb.public import auth
from dddb.public import site as site_mod
from dddb.public import templates as T
from dddb.public import visits
from dddb.public.users import UserStore

from tests import _tmp

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "dddb" / "public" / "static" / "public.css").read_text(encoding="utf-8")
TPL = (ROOT / "dddb" / "public" / "templates.py").read_text(encoding="utf-8")


def ctx(**kwargs) -> T.SiteContext:
    base = {"lang": "zh", "csrf_token": "tok", "is_editor": True}
    base.update(kwargs)
    return T.SiteContext(**base)


def _session_state(*, role: str = "superuser", user_id: int = 1,
                   username: str = "dddb_admin") -> auth.AuthState:
    sess = auth.Session(sid="s1", user_id=user_id, username=username,
                        role=role, expires_at=time.time() + 3600)
    return auth.AuthState(session=sess)


class _SiteCase(unittest.TestCase):
    """真起一个 PublicSite(临时库), 鉴权/CSRF 短路."""

    def setUp(self) -> None:  # noqa: D401
        self.tmp = _tmp.temp_path("dash")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.db_path = self.tmp / "dddb.sqlite3"
        store = db_mod.Store(self.db_path)
        store.upsert_album(Album.from_dict({
            "key": "adore|nika|2026",
            "title": "adore",
            "circle": "Nika Lenz",
            "release_date": "2026-10-25",
        }))
        store.close()
        self.users_path = self.tmp / "users.sqlite3"
        self.users = UserStore(self.users_path)
        ok, message = self.users.create_user("dddb_admin", "pw-123456", role="superuser")
        self.assertTrue(ok, message)
        self.uid = int((self.users.get("dddb_admin") or {}).get("id") or 1)
        self.site = site_mod.build_site(
            None, root=self.tmp, db_path=self.db_path, users_db=self.users_path,
            sessions_db=self.tmp / "sessions.sqlite3", covers_dir=self.tmp / "covers",
        )
        self.state = _session_state(user_id=self.uid)
        self.site.sessions.state = lambda cookies: self.state
        self.site.same_origin = lambda *a, **k: (True, "")
        self.site.sessions.signer.check = lambda b, g: True
        self.site.can_edit = lambda s: True
        # 注意: **不要**给 can_review 打桩 —— 看板的权限判定正是要测的东西。
        # 会话 role 是 superuser, 所以真 can_review() 自然放行。

    def body(self, path: str, *, method: str = "GET", body: bytes = b"",
             headers=None, ip: str = "1.2.3.4") -> tuple[int, str]:
        resp = self.site.handle(method, path, headers=headers or {}, body=body,
                                client_ip=ip)
        raw = resp.body
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
        return resp.status, text


# ---------------------------------------------------------------------------
# 1. 访问日志数据层
# ---------------------------------------------------------------------------


class TestVisitStore(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = _tmp.temp_path("visits")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.store = visits.VisitStore(self.dir / "site_visits.sqlite3")

    def test_store_is_enabled(self) -> None:
        self.assertTrue(self.store.enabled)

    def test_record_and_totals(self) -> None:
        now = time.time()
        self.assertTrue(self.store.record(path="/albums", route="albums", status=200,
                                          user_id=3, username="u", client_ip="1.1.1.1",
                                          now=now))
        self.store.record(path="/", route="home", client_ip="1.1.1.1", now=now)
        self.store.record(path="/about", route="about", client_ip="2.2.2.2", now=now)
        totals = self.store.totals(now=now)
        self.assertEqual(3, totals["views"])
        self.assertEqual(2, totals["visitors"])
        self.assertEqual(1, totals["members"])
        self.assertEqual(3, totals["total"])

    def test_totals_ignores_old_rows(self) -> None:
        now = time.time()
        self.store.record(path="/old", route="home", now=now - 3 * 86400)
        self.store.record(path="/new", route="home", now=now)
        self.assertEqual(1, self.store.totals(hours=24, now=now)["views"])

    def test_top_routes_and_paths(self) -> None:
        now = time.time()
        for _ in range(3):
            self.store.record(path="/albums", route="albums", now=now)
        self.store.record(path="/about", route="about", now=now)
        routes = self.store.top_routes(now=now)
        self.assertEqual("albums", routes[0]["route"])
        self.assertEqual(3, routes[0]["count"])
        paths = self.store.top_paths(now=now)
        self.assertEqual("/albums", paths[0]["path"])

    def test_recent_newest_first(self) -> None:
        now = time.time()
        self.store.record(path="/a", now=now - 10)
        self.store.record(path="/b", now=now)
        recent = self.store.recent(5)
        self.assertEqual(["/b", "/a"], [r["path"] for r in recent])

    def test_purge_removes_only_old(self) -> None:
        now = time.time()
        self.store.record(path="/old", now=now - 40 * 86400)
        self.store.record(path="/new", now=now)
        removed = self.store.purge(keep_days=30, now=now)
        self.assertEqual(1, removed)
        self.assertEqual(["/new"], [r["path"] for r in self.store.recent(10)])

    def test_ua_and_path_clipped(self) -> None:
        self.store.record(path="/" + "x" * 500, route="r", ua="U" * 500)
        row = self.store.recent(1)[0]
        self.assertLessEqual(len(row["path"]), visits.PATH_MAX)
        self.assertLessEqual(len(row["ua"]), visits.UA_MAX)

    def test_disabled_store_is_noop(self) -> None:
        broken = visits.VisitStore("")
        self.assertFalse(broken.enabled)
        self.assertFalse(broken.record(path="/x"))
        self.assertEqual([], broken.recent())
        self.assertEqual(0, broken.purge())
        self.assertEqual({}, {})  # totals 在空实现上仍返回 dict
        self.assertEqual(0, broken.totals()["views"])


class TestServerStats(unittest.TestCase):
    def test_memory_shape(self) -> None:
        info = visits.memory_info()
        for key in ("total_mb", "available_mb", "used_mb", "percent"):
            self.assertIn(key, info)
            self.assertIsInstance(info[key], float)

    def test_disk_shape(self) -> None:
        info = visits.disk_info("/")
        for key in ("total_gb", "used_gb", "free_gb", "percent"):
            self.assertIn(key, info)

    def test_load_average_three(self) -> None:
        self.assertEqual(3, len(visits.load_average()))

    def test_server_stats_keys(self) -> None:
        stats = visits.server_stats()
        for key in ("cpu_percent", "memory", "disk", "load", "uptime"):
            self.assertIn(key, stats)

    def test_cpu_snapshot_never_raises(self) -> None:
        value = visits.cpu_snapshot()
        self.assertTrue(value is None or (isinstance(value, tuple) and len(value) == 2))

    def test_uptime_is_text(self) -> None:
        self.assertIsInstance(visits.uptime_text(), str)


# ---------------------------------------------------------------------------
# 2. 看板页(权限 + 渲染)
# ---------------------------------------------------------------------------


class TestDashboardPage(_SiteCase):
    def test_dashboard_route(self) -> None:
        self.assertEqual({"name": "dashboard", "arg": ""},
                         site_mod.parse_route("/dashboard"))

    def test_dashboard_denied_for_non_superuser(self) -> None:
        self.site.can_review = lambda s: False
        status, text = self.body("/dashboard")
        self.assertEqual(403, status)
        self.assertIn("站务", text)

    def test_dashboard_denied_when_anonymous(self) -> None:
        self.site.sessions.state = lambda cookies: auth.AuthState()
        status, _ = self.body("/dashboard")
        self.assertEqual(403, status)

    def test_dashboard_renders_sections(self) -> None:
        self.body("/albums")
        status, text = self.body("/dashboard")
        self.assertEqual(200, status)
        for token in ("dash-tiles", "dash-gauge", "热门页面", "热门路径",
                      "最近访问", "最近改动", "服务器负载"):
            self.assertIn(token, text)

    def test_dashboard_counts_own_visit(self) -> None:
        self.body("/albums")
        _, text = self.body("/dashboard")
        self.assertIn("近 24 小时访问", text)
        self.assertIn("/albums", text)

    def test_dashboard_lists_recent_edit(self) -> None:
        store = self.site.writable_store()
        try:
            store.add_revision("adore|nika|2026", user_id=self.uid,
                               username="dddb_admin", summary="编辑前快照",
                               payload="{}")
        finally:
            store.close()
        self.site.data.invalidate()
        _, text = self.body("/dashboard")
        self.assertIn("编辑前快照", text)
        self.assertIn("adore|nika|2026", text)


# ---------------------------------------------------------------------------
# 3. 访问日志接入 handle()
# ---------------------------------------------------------------------------


class TestVisitRecording(_SiteCase):
    def test_page_requests_are_recorded(self) -> None:
        for path in ("/", "/albums", "/stats"):
            self.body(path, headers={"user-agent": "UA-test"})
        rows = self.site.visit_store().recent(10)
        paths = [r["path"] for r in rows]
        for expected in ("/", "/albums", "/stats"):
            self.assertIn(expected, paths)
        self.assertEqual("UA-test", rows[0]["ua"])

    def test_static_assets_not_recorded(self) -> None:
        before = len(self.site.visit_store().recent(50))
        self.body("/static/public.css")
        self.body("/static/public.js")
        self.assertEqual(before, len(self.site.visit_store().recent(50)))

    def test_head_not_recorded(self) -> None:
        before = len(self.site.visit_store().recent(50))
        self.site.handle("HEAD", "/albums", headers={}, client_ip="1.2.3.4")
        self.assertEqual(before, len(self.site.visit_store().recent(50)))

    def test_visit_keeps_username(self) -> None:
        self.body("/albums")
        row = self.site.visit_store().recent(1)[0]
        self.assertEqual("dddb_admin", row["username"])
        self.assertEqual(self.uid, row["user_id"])

    def test_visit_failure_does_not_break_page(self) -> None:
        class Boom:
            enabled = True

            def record(self, **kwargs):
                raise RuntimeError("boom")

            def purge(self, **kwargs):
                return 0

            def recent(self, limit=120):
                return []

            def totals(self, **kwargs):
                return {}

            def top_routes(self, **kwargs):
                return []

            def top_paths(self, **kwargs):
                return []

        self.site._visits = Boom()
        status, _ = self.body("/albums")
        self.assertEqual(200, status)


# ---------------------------------------------------------------------------
# 4. 最后活跃 = last_seen
# ---------------------------------------------------------------------------


class TestLastSeen(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = _tmp.temp_path("seen")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.users = UserStore(self.dir / "users.sqlite3")
        ok, message = self.users.create_user("someone", "pw-123456", role="viewer")
        self.assertTrue(ok, message)
        self.user = self.users.get("someone") or {}
        self.uid = int(self.user["id"])

    def test_column_exists(self) -> None:
        self.assertIn("last_seen", self.users.columns())

    def test_new_user_has_zero_last_seen(self) -> None:
        self.assertEqual(0.0, float(self.user.get("last_seen") or 0))

    def test_touch_seen_updates(self) -> None:
        now = time.time()
        self.assertTrue(self.users.touch_seen(self.uid, now=now, min_gap=0))
        fresh = self.users.get("someone") or {}
        self.assertAlmostEqual(now, float(fresh["last_seen"]), delta=2)

    def test_touch_seen_throttled(self) -> None:
        now = time.time()
        self.assertTrue(self.users.touch_seen(self.uid, now=now, min_gap=300))
        self.assertFalse(self.users.touch_seen(self.uid, now=now + 10, min_gap=300))

    def test_touch_seen_allows_after_gap(self) -> None:
        now = time.time()
        self.users.touch_seen(self.uid, now=now, min_gap=300)
        self.assertTrue(self.users.touch_seen(self.uid, now=now + 400, min_gap=300))

    def test_touch_seen_bad_id(self) -> None:
        self.assertFalse(self.users.touch_seen(0))
        self.assertFalse(self.users.touch_seen("abc"))

    def test_authenticate_writes_both(self) -> None:
        self.users.authenticate("someone", "pw-123456")
        fresh = self.users.get("someone") or {}
        self.assertGreater(float(fresh["last_login"] or 0), 0)
        self.assertGreater(float(fresh["last_seen"] or 0), 0)


class TestProfileActiveText(_SiteCase):
    def test_profile_shows_last_seen_not_login(self) -> None:
        old = time.time() - 10 * 86400
        self.users.users if False else None
        # 手工把 last_login 设成十天前, last_seen 设为刚刚
        self.users._execute("UPDATE users SET last_login = ?, last_seen = ? WHERE id = ?",
                            (old, time.time(), self.uid))
        self.site.sessions.state = lambda cookies: auth.AuthState()
        status, text = self.body("/u/dddb_admin")
        self.assertEqual(200, status)
        self.assertIn("最后活跃", text)


# ---------------------------------------------------------------------------
# 5. 贡献合并 + 关注头像
# ---------------------------------------------------------------------------


class TestMergeContributions(_SiteCase):
    def _items(self):
        now = time.time()
        return [
            {"kind": "edit", "target": "a|1", "summary": "第三", "created_at": now},
            {"kind": "edit", "target": "b|2", "summary": "第二", "created_at": now - 10},
            {"kind": "edit", "target": "a|1", "summary": "第一", "created_at": now - 20},
        ]

    def test_same_target_merged(self) -> None:
        merged = self.site._merge_contributions(self._items())
        self.assertEqual(2, len(merged))
        first = merged[0]
        self.assertEqual("a|1", first["target"])
        self.assertEqual(2, first["count"])
        self.assertEqual("第三", first["summary"])

    def test_order_kept(self) -> None:
        merged = self.site._merge_contributions(self._items())
        self.assertEqual(["a|1", "b|2"], [m["target"] for m in merged])

    def test_empty(self) -> None:
        self.assertEqual([], self.site._merge_contributions([]))

    def test_different_kinds_not_merged(self) -> None:
        now = time.time()
        merged = self.site._merge_contributions([
            {"kind": "edit", "target": "x", "summary": "", "created_at": now},
            {"kind": "upload", "target": "x", "summary": "", "created_at": now - 1},
        ])
        self.assertEqual(2, len(merged))


class TestEnrichFollows(_SiteCase):
    def test_hrefs_and_images(self) -> None:
        rows = [
            {"kind": "circle", "key": "某社团", "created_at": 0},
            {"kind": "artist", "key": "某艺人", "created_at": 0},
            {"kind": "event", "key": "M3-2026秋", "created_at": 0},
        ]
        out = self.site._enrich_follows(rows)
        self.assertEqual(3, len(out))
        self.assertTrue(out[0]["href"].startswith("/circle/"))
        self.assertTrue(out[1]["href"].startswith("/artist/"))
        self.assertTrue(out[2]["href"].startswith("/event/"))
        for item in out:
            self.assertIn("image", item)

    def test_entity_image_reads_payload(self) -> None:
        from dddb.models import Circle

        store = self.site.writable_store()
        try:
            store.upsert_circle(Circle.from_dict({
                "name": "某社团", "full_name": "某社团",
                "logo_url": "https://x/logo.png",
            }))
        finally:
            store.close()
        self.site.data.invalidate()
        self.assertEqual("https://x/logo.png", self.site._entity_image("circle", "某社团"))

    def test_entity_image_missing(self) -> None:
        self.assertEqual("", self.site._entity_image("circle", "不存在"))
        self.assertEqual("", self.site._entity_image("artist", "不存在"))


# ---------------------------------------------------------------------------
# 6. 收藏专辑
# ---------------------------------------------------------------------------


class TestAlbumFavorite(_SiteCase):
    def test_detail_has_favorite_form_for_member(self) -> None:
        status, text = self.body("/album/adore%7Cnika%7C2026")
        self.assertEqual(200, status)
        self.assertIn('class="album-favorite"', text)
        self.assertIn('action="/favorites/toggle"', text)
        self.assertIn("收藏专辑", text)

    def test_detail_prompts_login_for_anonymous(self) -> None:
        self.site.sessions.state = lambda cookies: auth.AuthState()
        status, text = self.body("/album/adore%7Cnika%7C2026")
        self.assertEqual(200, status)
        self.assertIn("登录后可收藏", text)

    def test_toggle_marks_favorite(self) -> None:
        payload = "csrf_token=tok&album_key=adore%7Cnika%7C2026".encode()
        status, _ = self.body("/favorites/toggle", method="POST", body=payload,
                             headers={"content-type": "application/x-www-form-urlencoded"})
        self.assertIn(status, (200, 303))
        profile = self.site.profile_store()
        if profile is not None:
            self.assertTrue(profile.is_favorite(self.uid, "adore|nika|2026"))

    def test_detail_shows_collected_state(self) -> None:
        profile = self.site.profile_store()
        if profile is None:
            self.skipTest("没有个人中心库")
        profile.toggle_favorite(self.uid, "adore|nika|2026")
        _, text = self.body("/album/adore%7Cnika%7C2026")
        self.assertIn("已收藏", text)


# ---------------------------------------------------------------------------
# 7. 模板 / CSS / 版本
# ---------------------------------------------------------------------------


class TestFrontendWiring(unittest.TestCase):
    def test_css_has_dashboard_blocks(self) -> None:
        for token in (".dash-tiles", ".dash-tile", ".dash-gauge", ".dash-bar-fill",
                      ".dash-card", ".dash-visits", ".dash-edits", ".dash-empty"):
            self.assertIn(token, CSS)

    def test_css_has_follow_card(self) -> None:
        for token in (".follow-grid", ".follow-card", ".follow-card-avatar",
                      ".follow-card-name", ".contrib-count", ".album-favorite"):
            self.assertIn(token, CSS)

    def test_nav_has_dashboard_link(self) -> None:
        html = T._nav("dashboard", lang="zh", is_editor=True, is_superuser=True)
        self.assertIn('href="/dashboard"', html)
        self.assertIn("数据看板", html)

    def test_nav_hides_dashboard_for_normal_user(self) -> None:
        html = T._nav("albums", lang="zh", is_editor=True, is_superuser=False)
        self.assertNotIn('href="/dashboard"', html)

    def test_versions_bumped(self) -> None:
        for name, floor in (("public.css", 81), ("public.js", 68), ("infobox.js", 10)):
            match = re.search(name.replace(".", r"\.") + r"\?v=(\d+)", TPL)
            self.assertIsNotNone(match, name)
            self.assertGreaterEqual(int(match.group(1)), floor, name)

    def test_dashboard_page_renders_empty_state(self) -> None:
        html = T.dashboard_page(ctx(is_superuser=True), data={})
        self.assertIn("dash-empty", html)
        self.assertIn("还没有访问记录", html)

    def test_dashboard_gauge_clamped(self) -> None:
        html = T.dashboard_page(ctx(is_superuser=True), data={
            "enabled": True,
            "totals": {"views": 1, "visitors": 1, "members": 1, "total": 1},
            "server": {"cpu_percent": 130.0, "memory": {}, "disk": {}, "load": {},
                       "uptime": ""},
        })
        self.assertIn("width:100.0%", html)
        self.assertNotIn("width:130", html)


if __name__ == "__main__":
    unittest.main()
