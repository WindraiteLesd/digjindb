"""品牌文案 / logo / 「工作队列」与看板的两处隐藏 / 密码措辞 的测试.

分层:

1. 纯模板断言(站点名、副标题、页脚、logo 引用、导航过滤);
2. 页面级隐藏(首页工作队列入口、看板两块编辑指标、关于页的死链接);
3. 站点级鉴权(``/edit`` 对访客/普通用户 403, 对编辑者/超级用户 200);
4. 源码级守卫(``口令`` 不再出现在用户端文案里、``STATIC_FILES`` 含 logo).

身份用**真实**的 :class:`UserStore` + :class:`SessionManager` 造会话, 经
``PublicSite.handle`` 走完整鉴权链路(而不是伪造一个 is_editor 的上下文)。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest import mock as umock

from dddb.db import Store
from dddb.models import Album
from dddb.public import auth
from dddb.public import templates as T
from dddb.public import site as site_module
from dddb.public.data import PublicData, ReadOnlyStore
from dddb.public.site import STATIC_FILES, PublicSite
from dddb.public.users import ROLE_EDITOR, ROLE_SUPERUSER, ROLE_VIEWER, UserStore
from tests._tmp import temp_dir

#: 旧品牌 —— 改版后任何页面都不许再出现
OLD_BRAND = ("同人专辑档案", "同人音乐专辑索引")

#: 假的统计数字(模板只做展示, 不校验来源)
FAKE_STATS = {
    "albums": 3, "circles": 2, "events": 2, "tags": 5, "tracks": 9, "edited": 1,
    "has_profile": 2,
    "coverage": {"local": 1, "remote": 1, "none": 1, "rate": 66.7},
    "completeness": {"no_tracks": 1, "no_desc": 1, "no_cover": 1, "no_circle": 0, "no_date": 0},
    "years": [{"value": "2026", "count": 3}],
    "event_top": [{"value": "M3-2026秋", "label": "M3-2026秋", "count": 2}],
    "kinds": [{"value": "m3", "label": "M3", "count": 2}],
    "sources": [],
    "genres": [{"value": "Vocal", "count": 1}],
    "tags_top": [{"value": "女性ボーカル", "count": 2}],
    "circles_top": [{"value": "C", "count": 2}],
}


def make_album(title: str, **over: object) -> Album:
    album = Album(
        title=title,
        title_original=title,
        circle=str(over.get("circle", "")),
        release_date=str(over.get("release_date", "")),
        event=str(over.get("event", "")),
        tags=list(over.get("tags", []) or []),
        genres=list(over.get("genres", []) or []),
        source_ids={"test": title},
    )
    album.key = album.compute_key()
    return album


def nav_of(html: str) -> str:
    match = re.search(r'<nav class="nav"[^>]*>(.*?)</nav>', html, re.S)
    return match.group(1) if match else ""


class BrandFixture:
    """小库 + 三个身份的站点对象(不依赖 socket)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.db_path = root / "data" / "dddb.sqlite3"
        self.covers = root / "data" / "covers"
        self.covers.mkdir(parents=True, exist_ok=True)
        store = Store(self.db_path)
        store.upsert_album(make_album(
            "幻想郷の記憶", circle="ShibayanRecords", release_date="2026-10-25",
            event="M3-2026秋", tags=["女性ボーカル", "Rock"],
        ))
        store.close()
        self.data = PublicData(ReadOnlyStore(self.db_path), root=root, covers_dir=self.covers)
        self.users = UserStore(root / "data" / "public_users.sqlite3")
        self.sessions = auth.SessionManager()
        for name, role in (("viewer1", ROLE_VIEWER), ("editor1", ROLE_EDITOR),
                           ("super1", ROLE_SUPERUSER)):
            ok, message = self.users.create_user(name, "password-123", role=role)
            assert ok, message
        self.site = PublicSite(self.data, users=self.users, sessions=self.sessions,
                               db_path=self.db_path)

    def state(self, username: str) -> auth.AuthState:
        user = self.users.get(username) or {}
        session = self.sessions.create(user)
        return auth.AuthState(
            session=session,
            csrf_binding=session.sid,
            csrf_token=self.sessions.signer.token(session.sid),
        )

    @staticmethod
    def headers(state: auth.AuthState | None) -> dict[str, str]:
        if state is None or state.session is None:
            return {}
        return {"cookie": f"{auth.SESSION_COOKIE}={state.session.sid}"}

    def get(self, state: auth.AuthState | None, path: str, query: dict | None = None):
        return self.site.handle("GET", path, query or {}, headers=self.headers(state),
                                client_ip="127.0.0.1")

    def close(self) -> None:
        self.site.close()
        self.users.close()


class BrandCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._ctx = temp_dir("brand-")
        cls.fixture = BrandFixture(cls._ctx.__enter__())
        cls.guest = None
        cls.viewer = cls.fixture.state("viewer1")
        cls.editor = cls.fixture.state("editor1")
        cls.superuser = cls.fixture.state("super1")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture.close()
        cls._ctx.__exit__(None, None, None)

    def page(self, state: auth.AuthState | None, path: str, query: dict | None = None) -> str:
        resp = self.fixture.get(state, path, query)
        return resp.body.decode("utf-8")


# --------------------------------------------------------------------------
# 1. 品牌常量与外壳
# --------------------------------------------------------------------------


class TestBrandCopy(BrandCase):
    def test_constants_are_the_new_brand(self) -> None:
        self.assertEqual(T.SITE_NAME, "DoujinDigDatabase")
        self.assertEqual(T.SITE_TAGLINE, "同人音乐专辑挖掘数据库")

    def test_layout_header_uses_new_name_and_tagline(self) -> None:
        html = T.layout(T.SiteContext(), title="首页", body="<p>b</p>")
        brand = re.search(r'<a class="brand" href="/">(.*?)</a>', html, re.S).group(1)
        self.assertIn("DoujinDigDatabase", brand)
        self.assertIn("同人音乐专辑挖掘数据库", brand)
        self.assertIn("<title>首页 · DoujinDigDatabase</title>", html)

    def test_header_references_logo_svg_not_a_text_mark(self) -> None:
        html = T.layout(T.SiteContext(), title="首页", body="")
        # src 带 `?v=2`: 图标内容变过, 必须破掉 CF/浏览器对静态资源 1 天的缓存
        self.assertRegex(html, r'<img class="brand-logo" src="/static/logo\.svg(\?v=\d+)?" alt="DoujinDigDatabase"')
        # 保持原来的 34×34 尺寸(布局不变)
        self.assertIn('width="34" height="34"', html)
        self.assertNotIn('<span class="brand-mark"', html)
        self.assertNotIn('aria-hidden="true">同</span>', html)

    def test_footer_default_is_the_new_tagline(self) -> None:
        html = T.layout(T.SiteContext(), title="x", body="")
        footer = html.split('<footer', 1)[1]
        self.assertIn("同人音乐专辑挖掘数据库", footer)
        custom = T.layout(T.SiteContext(footer_note="自定义页脚"), title="x", body="")
        self.assertIn("自定义页脚", custom.split('<footer', 1)[1])

    def test_no_old_brand_anywhere_in_rendered_pages(self) -> None:
        for path in ("/", "/albums", "/stats", "/about", "/login", "/register"):
            html = self.page(self.guest, path)
            for word in OLD_BRAND:
                self.assertNotIn(word, html, f"{path} 仍出现旧品牌: {word}")

    def test_no_kouling_wording_in_rendered_pages(self) -> None:
        """用户明确要求「口令」统一叫「密码」."""
        for path in ("/", "/login", "/register", "/about"):
            html = self.page(self.guest, path)
            self.assertNotIn("口令", html, path)
        login = self.page(self.guest, "/login")
        self.assertIn("密码", login)
        register = self.page(self.guest, "/register")
        self.assertIn("密码", register)

    def test_source_copy_has_no_kouling_left(self) -> None:
        """源码级守卫: 以后新写的文案也别把「口令」加回来."""
        root = Path(__file__).resolve().parent.parent / "dddb" / "public"
        for name in ("templates.py", "site.py"):
            text = (root / name).read_text(encoding="utf-8")
            self.assertNotIn("口令", text, name)

    def test_static_whitelist_includes_logo(self) -> None:
        self.assertIn("logo.svg", STATIC_FILES)
        self.assertIn("public.css", STATIC_FILES)
        self.assertIn("favicon.svg", STATIC_FILES)

    def test_logo_is_served_with_svg_content_type(self) -> None:
        """白名单放行 + 正确 MIME(真文件由站方放入 static/, 这里用临时目录验证)."""
        with temp_dir("brand-static-") as static_dir:
            (static_dir / "logo.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg"></svg>', encoding="utf-8")
            with umock.patch.object(site_module, "STATIC_DIR", static_dir):
                resp = self.fixture.site.serve_static("logo.svg")
                self.assertEqual(resp.status, 200)
                self.assertIn("image/svg+xml", resp.content_type)
                self.assertIn(b"<svg", resp.body)
                # 同目录下不在白名单里的文件依旧 404
                (static_dir / "secret.txt").write_text("no", encoding="utf-8")
                self.assertEqual(self.fixture.site.serve_static("secret.txt").status, 404)

    def test_favicon_route_serves_the_new_icon(self) -> None:
        resp = self.fixture.get(self.guest, "/favicon.ico")
        self.assertEqual(resp.status, 200)
        self.assertIn("image/svg+xml", resp.content_type)


# --------------------------------------------------------------------------
# 2. 普通用户看不到的东西
# --------------------------------------------------------------------------


class TestHiddenForNonEditors(BrandCase):
    def test_nav_hides_work_queue_for_guest_and_viewer(self) -> None:
        for state in (self.guest, self.viewer):
            html = T.layout(T.SiteContext(user=state.user if state else None,
                                          is_editor=False), title="x", body="")
            nav = nav_of(html)
            self.assertNotIn('href="/edit"', nav)
            self.assertNotIn("工作队列", nav)
        editor_html = T.layout(T.SiteContext(is_editor=True), title="x", body="")
        self.assertIn('href="/edit"', nav_of(editor_html))
        self.assertIn("工作队列", nav_of(editor_html))

    def test_home_hides_queue_button_and_inbox_block(self) -> None:
        inbox = {"total": 7, "kinds": [{"value": "no_cover", "label": "缺封面", "count": 7}]}
        guest = T.home_page(T.SiteContext(), latest=[], events=[], circles=[], tags=[],
                            stats=FAKE_STATS, inbox=inbox)
        self.assertNotIn('href="/edit"', guest)
        self.assertNotIn("inbox-rows", guest)
        self.assertNotIn("工作队列", guest)
        editor = T.home_page(T.SiteContext(is_editor=True), latest=[], events=[], circles=[],
                             tags=[], stats=FAKE_STATS, inbox=inbox)
        self.assertIn('href="/edit"', editor)
        self.assertIn("inbox-rows", editor)
        self.assertIn("工作队列", editor)

    def test_stats_hides_editor_metric_blocks(self) -> None:
        guest = T.stats_page(T.SiteContext(), stats=FAKE_STATS)
        for word in ("封面覆盖率", "信息完整度", "打开工作队列"):
            self.assertNotIn(word, guest)
        # 普通用户仍然看得到内容统计
        self.assertIn("年份趋势", guest)
        self.assertIn("展会分布", guest)
        self.assertEqual(guest.count("stat-card"), 6)
        editor = T.stats_page(T.SiteContext(is_editor=True), stats=FAKE_STATS)
        self.assertIn("封面覆盖率", editor)
        self.assertIn("信息完整度", editor)
        self.assertIn("打开工作队列", editor)

    def test_about_page_has_no_dead_queue_link_for_viewers(self) -> None:
        guest = T.about_page(T.SiteContext(), stats=FAKE_STATS)
        self.assertNotIn('href="/edit"', guest)
        editor = T.about_page(T.SiteContext(is_editor=True), stats=FAKE_STATS)
        self.assertIn('href="/edit"', editor)


class TestEditRouteAccess(BrandCase):
    def test_guest_and_viewer_get_403(self) -> None:
        for state in (self.guest, self.viewer):
            for path in ("/edit", "/edit/no_cover"):
                resp = self.fixture.get(state, path)
                self.assertEqual(resp.status, 403, path)
                body = resp.body.decode("utf-8")
                self.assertIn("工作队列只对编辑者开放", body)
                # 拒绝页里**不渲染**队列内容
                self.assertNotIn("queue-row", body)

    def test_editor_and_superuser_get_200(self) -> None:
        for state in (self.editor, self.superuser):
            overview = self.fixture.get(state, "/edit")
            self.assertEqual(overview.status, 200)
            body = overview.body.decode("utf-8")
            self.assertIn("工作队列", body)
            self.assertIn("tabs", body)                 # 缺口分类标签页

            detail = self.fixture.get(state, "/edit/no_cover")
            self.assertEqual(detail.status, 200)
            detail_body = detail.body.decode("utf-8")
            self.assertIn("工作队列 · 缺封面", detail_body)
            self.assertIn("queue-row", detail_body)     # 具体条目(不是空壳)
            self.assertIn("命中", detail_body)

    def test_superuser_is_treated_as_editor_in_context(self) -> None:
        ctx = self.fixture.site.context(self.superuser)
        self.assertTrue(ctx.is_editor)
        self.assertTrue(self.fixture.site.can_edit(self.superuser))
        viewer_ctx = self.fixture.site.context(self.viewer)
        self.assertFalse(viewer_ctx.is_editor)
        self.assertFalse(self.fixture.site.can_edit(self.viewer))
        self.assertFalse(self.fixture.site.can_edit(None))

    def test_editor_sees_edit_buttons_on_album_page(self) -> None:
        key = self.fixture.data.query_albums(page=1, per=1)[0][0]["key"]
        path = "/album/" + T.quote_segment(key)
        guest = self.page(self.guest, path)
        self.assertNotIn("编辑这张专辑", guest)
        editor = self.page(self.editor, path)
        self.assertIn("编辑这张专辑", editor)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()