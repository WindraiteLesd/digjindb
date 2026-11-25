"""用户端「整段粘贴 -> 智能识别 -> 批量填入曲目」测试.

覆盖四层:

1. **模板**: 编辑页必须带 ``track_paste`` / ``track_mode`` 两个普通表单字段(用户端
   零脚本, 所以粘贴只能随表单 POST 上来, 不能是 AJAX); 重填时粘贴内容原样保留并
   转义; 折叠区默认收起、有内容时展开;
2. **纯函数**: :func:`dddb.public.site.merge_pasted_tracks` 的替换 / 追加语义、
   空解析结果**原样返回**(绝不清空)、非法模式回退;
3. **解析**: 四种真实粘贴写法(``01. 曲名 - 艺人 4:12`` / 全角数字 / ``Tr.1`` /
   制表符表格)经 :func:`dddb.trackparse.parse_tracklist` 后时长归一成秒;
4. **HTTP 集成**: 真起服务(端口 0)以编辑者身份登录 -> GET 编辑页 -> POST 粘贴
   -> 303 -> 用 ``Store.get_album`` 复核曲目表真的按预期变更 -> 还原;
   外加"识别不出曲目时曲目表不变、提示、**其它字段照常保存**"与越权 403。

临时目录用 :func:`tests._tmp.temp_dir`(Windows 沙箱下系统 TEMP 不可写)。
"""

from __future__ import annotations

import dataclasses
import http.cookiejar
import re
import threading
import unittest

from dddb.public import verify
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dddb.db import Store
from dddb.models import Album, Track
from dddb.public import auth
from dddb.public import templates as T
from dddb.public.data import PublicData, ReadOnlyStore
from dddb.public.site import (
    PublicSite,
    form_display_values,
    form_values,
    make_server,
    merge_pasted_tracks,
    normalize_track_mode,
    parsed_track_items,
)
from dddb.public.users import ROLE_EDITOR, UserStore
from dddb.trackparse import ParsedTrack, format_duration, parse_tracklist
from tests._tmp import temp_dir

#: 真实格式的粘贴文本: 四种写法各一条(编号+艺人+时长 / 全角 / Tr.N / 制表符表格)
PASTE_TEXT = (
    "01. 曲名 - 艺人 4:12\n"
    "０２．全角曲名　４：１２\n"
    "Tr.3 第三首 4分12秒\n"
    "04\t表格曲名\t表格艺人\t5:03\n"
)

#: 一个字都识别不出曲目的粘贴文本(全是表头 / 注释 / 单独一行的时长)
PASTE_UNRECOGNIZED = "曲目列表\n# 曲名 / アーティスト / 時間\n4:12\n"

#: 编辑页里绝对不允许出现的东西(站长端 / 数据站 / 来源字样)
FORBIDDEN_WORDS = (
    "站长端", "admin-url", "8787", "dojinmusic", "dojin-music", "bangumi", "bgm.tv",
    "vgmdb", "doujinstyle", "dizzylab", "discogs", "来源构成", "来源",
)

ORIGINAL_TRACKS = [
    {"index": 1, "title": "Opening", "artists": [], "duration": 83},
    {"index": 2, "title": "幻想郷の記憶", "artists": ["坂上なち"], "duration": 245},
]


def make_album(title: str, **over: object) -> Album:
    album = Album(
        title=title,
        title_original=str(over.get("title_original", title)),
        circle=str(over.get("circle", "")),
        artists=list(over.get("artists", []) or []),
        release_date=str(over.get("release_date", "")),
        event=str(over.get("event", "")),
        catalog_no=str(over.get("catalog_no", "")),
        genres=list(over.get("genres", []) or []),
        tags=list(over.get("tags", []) or []),
        description=str(over.get("description", "")),
        tracks=list(over.get("tracks", []) or []),
        credits=list(over.get("credits", []) or []),
        urls=dict(over.get("urls", {}) or {}),
        source_ids=dict(over.get("source_ids", {}) or {}),
    )
    album.key = album.compute_key()
    return album


class Fixture:
    """临时目录里的专辑库 + 用户库 + 站点对象."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.covers = root / "data" / "covers"
        self.covers.mkdir(parents=True, exist_ok=True)
        self.db_path = root / "data" / "dddb.sqlite3"
        self.users_path = root / "data" / "public_users.sqlite3"
        self.store = Store(self.db_path)
        self.album = make_album(
            "幻想郷の記憶",
            circle="ShibayanRecords",
            release_date="2026-10-25",
            event="M3-2026秋",
            catalog_no="SBCD-042",
            tags=["東方Project", "Vocal"],
            genres=["Vocal"],
            description="一张用来测试粘贴的专辑。",
            tracks=[Track(**item) for item in ORIGINAL_TRACKS],
        )
        self.store.upsert_album(self.album)
        self.key = self.album.key
        self.data = PublicData(ReadOnlyStore(self.db_path), covers_dir=self.covers)
        self.users = UserStore(self.users_path)
        self.sessions = auth.SessionManager()

    def site(self, **kwargs: object) -> PublicSite:
        return PublicSite(
            self.data, users=self.users, sessions=self.sessions, db_path=self.db_path, **kwargs
        )

    def close(self) -> None:
        self.store.close()
        self.users.close()


# --------------------------------------------------------------------------
# 1. 模板(纯函数)
# --------------------------------------------------------------------------


class TestPasteTemplate(unittest.TestCase):
    CSS = (Path(__file__).resolve().parent.parent / "dddb" / "public" / "static" / "public.css").read_text(
        encoding="utf-8")

    def paste_block(self, html: str) -> str:
        start = html.index('<details class="trk-paste"')
        end = html.index("</details>", start) + len("</details>")
        return html[start:end]

    def render(self, values=None, **kwargs) -> str:
        return T.edit_album_page(
            T.SiteContext(csrf_token="tok", is_editor=True),
            album={"key": "k", "title": "T"},
            values=values if values is not None else T.edit_values_from_album({}),
            **kwargs,
        )

    def test_edit_page_has_the_paste_fields(self) -> None:
        block = self.paste_block(self.render())
        self.assertIn('name="track_paste"', block)
        self.assertIn('name="track_mode"', block)
        self.assertIn('value="replace"', block)
        self.assertIn('value="append"', block)

    def test_paste_area_is_a_details_block_with_both_modes(self) -> None:
        block = self.paste_block(self.render())
        self.assertTrue(block.startswith('<details class="trk-paste">'), block[:60])
        self.assertIn("<summary>", block)
        self.assertIn("替换现有曲目", block)
        self.assertIn("追加到末尾", block)
        # 两个单选框在折叠区里, 且默认选中"替换"
        self.assertIn('value="replace" checked', block)
        self.assertNotIn('value="append" checked', block)

    def test_paste_area_stays_inside_the_edit_form(self) -> None:
        page = self.render()
        form_start = page.index('class="edit-form"')
        form_end = page.index("</form>", form_start)
        self.assertLess(form_start, page.index('name="track_paste"'))
        self.assertLess(page.index('name="track_paste"'), form_end)
        self.assertIn('name="track_paste"', self.paste_block(page))

    def test_paste_is_not_ajax_because_the_public_site_has_no_js(self) -> None:
        """用户端零脚本: 粘贴框必须是普通表单字段, 页面里不能出现 fetch/XHR."""
        page = self.render()
        self.assertIn('method="post"', page)
        self.assertNotIn("fetch(", page)
        self.assertNotIn("XMLHttpRequest", page)
        self.assertNotIn("/api/", page)

    def test_pasted_text_is_kept_expanded_and_escaped_on_rerender(self) -> None:
        page = self.render(values={
            "track_paste": "01. A <script>alert(1)</script>\n02. B",
            "track_mode": "append",
        })
        block = self.paste_block(page)
        self.assertTrue(block.startswith('<details class="trk-paste" open>'), block[:60])
        self.assertIn("01. A &lt;script&gt;alert(1)&lt;/script&gt;", block)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn('value="append" checked', block)

    def test_default_mode_is_replace_when_missing_or_forged(self) -> None:
        for values in ({}, {"track_mode": ""}, {"track_mode": "drop-everything"}):
            with self.subTest(values=values):
                block = self.paste_block(self.render(values=values))
                self.assertIn('value="replace" checked', block)

    def test_paste_block_copy_is_public_safe(self) -> None:
        page = self.render(values={"track_paste": PASTE_TEXT})
        block = self.paste_block(page)
        for word in FORBIDDEN_WORDS:
            self.assertNotIn(word, block, word)
        self.assertNotIn("脚本", block)
        self.assertNotIn("AJAX", block)

    def test_css_styles_the_paste_area_in_the_dark_theme(self) -> None:
        def rule(selector: str) -> str:
            match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", self.CSS)
            return match.group(1) if match else ""

        block = rule(".trk-paste")
        self.assertIn("var(--bg-soft)", block)
        self.assertIn("var(--border-soft)", block)
        self.assertIn("var(--radius-sm)", block)
        self.assertIn("cursor: pointer", rule(".trk-paste summary"))
        self.assertIn("display: grid", rule(".trk-paste-body"))
        self.assertIn("var(--mono)", rule(".trk-paste-body textarea"))
        self.assertIn("display: flex", rule(".trk-paste-modes"))


class TestPasteSubmitButton(TestPasteTemplate):
    """粘贴区自己的提交按钮: 用户粘完一整段要有个能点的东西.

    历史: 这个折叠区以前只有一句「保存时随表单一起提交」, 没有任何按钮
    (用户截图报「这里的提交按钮没了」)。因为公开端零脚本, 按钮只能是普通
    submit —— 整张表单一起 POST, 服务端再解析 track_paste。
    """

    def test_paste_block_has_its_own_submit_button(self) -> None:
        block = self.paste_block(self.render())
        self.assertIn('type="submit"', block)
        self.assertIn("保存并填入曲目表", block)
        self.assertIn('data-track-apply="1"', block)

    def test_button_submits_the_whole_form(self) -> None:
        """按钮不是 formaction/独立表单, 必须继承外层 edit-form 的 action."""
        page = self.render()
        block = self.paste_block(page)
        self.assertNotIn("formaction", block)
        self.assertNotIn("<form", block)
        form_start = page.index('class="edit-form"')
        self.assertLess(form_start, page.index('data-track-apply="1"'))
        self.assertLess(page.index('data-track-apply="1"'), page.index("</form>", form_start))

    def test_old_hint_only_copy_is_gone(self) -> None:
        """旧的那句纯说明(带分号后跟"没识别出曲目时…")已被按钮行取代."""
        page = self.render()
        self.assertNotIn("保存时随表单一起提交并自动填入下方曲目表", page)
        block = self.paste_block(page)
        self.assertIn("trk-paste-acts", block)
        self.assertIn("会连同其它字段一起保存", block)
        self.assertIn("没识别出曲目时曲目表保持原样", block)

    def test_button_appears_in_new_album_page_too(self) -> None:
        page = self.render(creating=True)
        block = self.paste_block(page)
        self.assertIn('data-track-apply="1"', block)

    def test_css_lays_out_the_button_row(self) -> None:
        def rule(selector: str) -> str:
            match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", self.CSS)
            return match.group(1) if match else ""

        acts = rule(".trk-paste-acts")
        self.assertIn("display: flex", acts)
        self.assertIn("align-items: center", acts)
        self.assertIn("flex-wrap: wrap", acts)
        self.assertIn("padding", rule(".trk-paste-acts .btn"))
        self.assertIn("flex: 1 1", rule(".trk-paste-acts .trk-paste-hint"))


# --------------------------------------------------------------------------
# 2. 合并纯函数
# --------------------------------------------------------------------------


class TestPasteMerge(unittest.TestCase):
    OLD = [{"index": 1, "title": "旧曲", "artists": ["旧艺人"], "duration": 60}]

    def test_replace_drops_the_submitted_list_and_renumbers(self) -> None:
        merged = merge_pasted_tracks(self.OLD, parse_tracklist(PASTE_TEXT), "replace")
        self.assertEqual([item["title"] for item in merged],
                         ["曲名", "全角曲名", "第三首", "表格曲名"])
        self.assertNotIn("旧曲", [item["title"] for item in merged])
        self.assertEqual([item["index"] for item in merged], [1, 2, 3, 4])

    def test_append_keeps_the_submitted_list_first(self) -> None:
        merged = merge_pasted_tracks(self.OLD, parse_tracklist(PASTE_TEXT), "append")
        self.assertEqual([item["title"] for item in merged],
                         ["旧曲", "曲名", "全角曲名", "第三首", "表格曲名"])
        self.assertEqual([item["index"] for item in merged], [1, 2, 3, 4, 5])

    def test_append_into_an_empty_list_is_just_the_paste(self) -> None:
        merged = merge_pasted_tracks([], parse_tracklist(PASTE_TEXT), "append")
        self.assertEqual(len(merged), 4)
        self.assertEqual(merged[0]["title"], "曲名")

    def test_empty_parse_returns_the_submitted_list_untouched(self) -> None:
        parsed = parse_tracklist(PASTE_UNRECOGNIZED)
        self.assertEqual(parsed, [], "这段文本本来就一首都不该识别出来")
        for mode in ("replace", "append"):
            with self.subTest(mode=mode):
                merged = merge_pasted_tracks(self.OLD, parsed, mode)
                self.assertEqual(merged, self.OLD)
                self.assertIsNot(merged, self.OLD, "要返回副本, 不改调用方的列表")

    def test_parsed_tracks_carry_artists_and_seconds(self) -> None:
        merged = merge_pasted_tracks([], parse_tracklist(PASTE_TEXT))
        self.assertEqual(merged[0]["artists"], ["艺人"])
        self.assertEqual(merged[0]["duration"], 252)
        self.assertEqual(merged[1]["artists"], [], "全角那条没有艺人, 不该硬拆")
        self.assertEqual(merged[3]["artists"], ["表格艺人"])
        self.assertEqual(merged[3]["duration"], 303)
        for item in merged:
            self.assertIsInstance(item["duration"], int)

    def test_parsed_items_normalize_odd_objects(self) -> None:
        items = parsed_track_items([
            ParsedTrack(index=9, title="  留白标题  ", artists=["", " 艺人 ", ""], duration=0),
            ParsedTrack(index=2, title="   ", artists=["X"], duration=125),
            dataclasses.replace(ParsedTrack(), title="只有标题"),
        ])
        self.assertEqual([item["title"] for item in items], ["留白标题", "只有标题"])
        self.assertEqual(items[0]["artists"], ["艺人"])
        self.assertIsNone(items[0]["duration"], "0 秒当没写时长")
        self.assertIsNone(items[1]["duration"])
        self.assertEqual([item["index"] for item in items], [1, 2], "序号按结果顺序重排")

    def test_merge_accepts_a_forged_mode_as_replace(self) -> None:
        merged = merge_pasted_tracks(self.OLD, parse_tracklist(PASTE_TEXT), "drop-everything")
        self.assertEqual(merged[0]["title"], "曲名")
        self.assertEqual(normalize_track_mode("drop-everything"), "replace")
        self.assertEqual(normalize_track_mode(None), "replace")
        self.assertEqual(normalize_track_mode("APPEND"), "append")
        self.assertEqual(normalize_track_mode(" append "), "append")

    def test_durations_are_formatted_by_the_shared_helper(self) -> None:
        merged = merge_pasted_tracks([], parse_tracklist(PASTE_TEXT))
        self.assertEqual(format_duration(merged[0]["duration"]), "4:12")
        self.assertEqual(format_duration(merged[3]["duration"]), "5:03")
        self.assertEqual(format_duration(None), "")
        # 与模板的 duration_text 是同一套显示规则(用户端/站长端一致)
        self.assertEqual(T.duration_text(252), "4:12")
        self.assertEqual(T.duration_text(3727), "1:02:07")

    def test_form_display_values_carries_the_paste_fields(self) -> None:
        values = form_values(
            urllib.parse.urlencode({
                "title_original": "X",
                "track_paste": PASTE_TEXT,
                "track_mode": "append",
            }).encode("utf-8")
        )
        shown = form_display_values(values)
        self.assertEqual(shown["track_paste"], PASTE_TEXT)
        self.assertEqual(shown["track_mode"], "append")
        # 回显结构必须能被模板直接渲染, 且粘贴内容不丢
        page = T.edit_album_page(
            T.SiteContext(csrf_token="tok", is_editor=True),
            album={"key": "k", "title": "X"}, values=shown,
        )
        self.assertIn("01. 曲名 - 艺人 4:12", page)
        self.assertIn('value="append" checked', page)


# --------------------------------------------------------------------------
# 3. HTTP 集成
# --------------------------------------------------------------------------


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class HttpCase(unittest.TestCase):
    """真起一个服务(端口 0), 用带 Cookie 罐的 opener 打接口."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._ctx = temp_dir("track-paste-http-")
        cls.fixture = Fixture(cls._ctx.__enter__())
        cls.site = cls.fixture.site()
        cls.httpd = make_server(cls.site, host="127.0.0.1", port=0)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        host, port = cls.httpd.server_address[:2]
        cls.base = f"http://{host}:{port}"
        cls.key = cls.fixture.key
        ok, message = cls.fixture.users.create_user("editor1", "editor-password1", role=ROLE_EDITOR)
        assert ok, message
        cls.editor_credentials = ("editor1", "editor-password1")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.site.close()
        cls.fixture.close()
        cls._ctx.__exit__(None, None, None)

    @property
    def edit_path(self) -> str:
        return "/album/" + T.quote_segment(self.key) + "/edit"

    def setUp(self) -> None:
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.raw = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar), NoRedirect
        )
        self.site.sessions = auth.SessionManager()
        self.addCleanup(self.restore_original_tracks)

    # -- 工具 ---------------------------------------------------------------

    def restore_original_tracks(self) -> None:
        ok, message = self.fixture.store.update_album_fields(
            self.key, {"track_list": [dict(item) for item in ORIGINAL_TRACKS]}
        )
        assert ok, message

    def restore_title(self) -> None:
        ok, message = self.fixture.store.update_album_fields(
            self.key, {"title_original": "幻想郷の記憶", "title": "幻想郷の記憶"}
        )
        assert ok, message

    def tracks(self) -> list[tuple]:
        album = self.fixture.store.get_album(self.key)
        assert album is not None
        return [(track.index, track.title, list(track.artists), track.duration)
                for track in album.tracks]

    def _open(self, opener, method: str, path: str, *, data: bytes | None = None):
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            req.add_header("Origin", self.base)
        return opener.open(req, timeout=15)

    def page(self, path: str) -> str:
        with self._open(self.opener, "GET", path) as resp:
            return resp.read().decode("utf-8")

    def token_of(self, html: str) -> str:
        match = re.search(r'name="csrf_token" value="([^"]*)"', html)
        assert match, "编辑页里应该有 CSRF token"
        return match.group(1)

    def form_token(self, path: str) -> str:
        return self.token_of(self.page(path))

    def register_fields(self, **overrides: object) -> dict:
        """注册表单字段(含验证码): 注册现在必须过验证码, 所以统一走这里.

        题面是算术题, 从页面文本里算出答案 —— 纯表单, 没有 JS。
        """
        html = self.page("/register")
        id_match = re.search(r'name="captcha_id" value="([^"]*)"', html)
        assert id_match, "注册页里应该有验证码字段"
        captcha_id = id_match.group(1)
        item = verify._DEFAULT_CAPTCHA_STORE.peek(captcha_id)
        assert item, "验证码不存在或已过期"
        fields: dict = {
            "csrf_token": self.token_of(html),
            "captcha_id": captcha_id,
            "captcha_answer": str(item["answer"]),
        }
        fields.update(overrides)
        return fields

    def post_raw(self, path: str, fields: dict):
        body = urllib.parse.urlencode(fields, doseq=True).encode("utf-8")
        return self._open(self.raw, "POST", path, data=body)

    def post(self, path: str, fields: dict) -> str:
        body = urllib.parse.urlencode(fields, doseq=True).encode("utf-8")
        with self._open(self.opener, "POST", path, data=body) as resp:
            return resp.read().decode("utf-8")

    def login(self, username: str, password: str) -> None:
        token = self.form_token("/login")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post_raw("/login", {"csrf_token": token, "username": username,
                                     "password": password})
        self.assertEqual(ctx.exception.code, 303, ctx.exception.headers.get("Location"))

    def base_fields(self, **over: object) -> dict:
        fields = {"csrf_token": self.form_token(self.edit_path), "title_original": "幻想郷の記憶"}
        fields.update(over)
        return fields


class TestPasteHttp(HttpCase):
    def test_editor_edit_page_exposes_the_paste_fields(self) -> None:
        self.login(*self.editor_credentials)
        html = self.page(self.edit_path)
        self.assertIn('name="track_paste"', html)
        self.assertIn('name="track_mode"', html)
        self.assertIn('<details class="trk-paste">', html)
        self.assertIn('value="replace" checked', html)
        for word in FORBIDDEN_WORDS:
            self.assertNotIn(word, html, word)

    def test_viewer_paste_is_403(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post_raw("/register", self.register_fields(
                username="viewer1", password="password-1", password2="password-1"))
        self.assertEqual(ctx.exception.code, 303)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post(self.edit_path, self.base_fields(track_paste=PASTE_TEXT))
        self.assertEqual(ctx.exception.code, 403)
        self.assertEqual([item[1] for item in self.tracks()], ["Opening", "幻想郷の記憶"])

    def test_anonymous_paste_is_403(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post(self.edit_path, {"csrf_token": "whatever", "title_original": "X",
                                       "track_paste": PASTE_TEXT})
        self.assertEqual(ctx.exception.code, 403)
        self.assertEqual([item[1] for item in self.tracks()], ["Opening", "幻想郷の記憶"])

    def test_paste_replace_rewrites_the_stored_tracklist(self) -> None:
        self.login(*self.editor_credentials)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post_raw(self.edit_path, self.base_fields(
                track_paste=PASTE_TEXT, track_mode="replace"))
        self.assertEqual(ctx.exception.code, 303)
        self.assertEqual(ctx.exception.headers["Location"], "/album/" + T.quote_segment(self.key))
        self.assertEqual(self.tracks(), [
            (1, "曲名", ["艺人"], 252),
            (2, "全角曲名", [], 252),
            (3, "第三首", [], 252),
            (4, "表格曲名", ["表格艺人"], 303),
        ])

    def test_paste_append_keeps_existing_tracks_at_the_front(self) -> None:
        self.login(*self.editor_credentials)
        fields = self.base_fields(
            track_paste=PASTE_TEXT,
            track_mode="append",
            # 浏览器会把预填的两行一起提交上来
            track_index=["1", "2"],
            track_title=["Opening", "幻想郷の記憶"],
            track_artists=["", "坂上なち"],
            track_duration=["1:23", "4:05"],
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post_raw(self.edit_path, fields)
        self.assertEqual(ctx.exception.code, 303)
        self.assertEqual(self.tracks(), [
            (1, "Opening", [], 83),
            (2, "幻想郷の記憶", ["坂上なち"], 245),
            (3, "曲名", ["艺人"], 252),
            (4, "全角曲名", [], 252),
            (5, "第三首", [], 252),
            (6, "表格曲名", ["表格艺人"], 303),
        ])

    def test_unrecognized_paste_keeps_the_stored_tracks_and_hints(self) -> None:
        """粘贴一首都没识别出: 曲目表原样保留, 但**整次提交不作废**, 其它字段照常落库."""
        self.login(*self.editor_credentials)
        self.addCleanup(self.restore_title)
        html = self.post(self.edit_path, self.base_fields(
            title_original="幻想郷の記憶(粘贴时改的标题)",
            track_paste=PASTE_UNRECOGNIZED,
            track_mode="replace",
            track_index=["1", "2"],
            track_title=["Opening", "幻想郷の記憶"],
            track_artists=["", "坂上なち"],
            track_duration=["1:23", "4:05"],
        ))
        self.assertIn("粘贴内容没识别出曲目", html)
        self.assertIn("其它修改已保存", html)
        # 曲目表一字未动(没有被粘贴内容覆盖, 也没有被清空)
        self.assertEqual(self.tracks(), [
            (1, "Opening", [], 83),
            (2, "幻想郷の記憶", ["坂上なち"], 245),
        ])
        # **但标题真的改掉了** —— 粘贴失败不否决整次提交, 这正是用户预期的行为
        album = self.fixture.store.get_album(self.key)
        assert album is not None
        self.assertEqual(album.title_original, "幻想郷の記憶(粘贴时改的标题)")
        self.assertEqual(album.title, "幻想郷の記憶(粘贴时改的标题)")
        # 粘贴内容留在框里(折叠区展开), 用户改完能再存一次
        self.assertIn('<details class="trk-paste" open>', html)
        self.assertIn("# 曲名 / アーティスト / 時間", html)

    def test_unrecognized_paste_without_rows_keeps_existing_tracklist(self) -> None:
        """粘贴 0 首 + 表单没提交曲目行 -> **保留原有曲目**, 只提示粘贴没识别出来.

        数据保护优先: 表单里没有曲目行时无法区分"用户想清空"和"页面没渲染出来",
        所以一律保留原值(实测发生过一次保存清空 52 首曲目的事故)。
        """
        self.login(*self.editor_credentials)
        before = self.tracks()
        html = self.post(self.edit_path, self.base_fields(
            track_paste=PASTE_UNRECOGNIZED, track_mode="replace"))
        self.assertIn("粘贴内容没识别出曲目", html)
        album = self.fixture.store.get_album(self.key)
        assert album is not None
        self.assertEqual(self.tracks(), before, "曲目表应保持原样")
        self.assertEqual(album.title, "幻想郷の記憶")

    def test_empty_paste_never_clears_the_submitted_tracks(self) -> None:
        self.login(*self.editor_credentials)
        for paste in ("", "   \n  "):
            with self.subTest(paste=paste):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    self.post_raw(self.edit_path, self.base_fields(
                        track_paste=paste,
                        track_index=["1", "2"],
                        track_title=["Opening", "幻想郷の記憶"],
                        track_artists=["", "坂上なち"],
                        track_duration=["1:23", "4:05"],
                    ))
                self.assertEqual(ctx.exception.code, 303)
                self.assertEqual(self.tracks(), [
                    (1, "Opening", [], 83),
                    (2, "幻想郷の記憶", ["坂上なち"], 245),
                ])

    def test_paste_survives_a_validation_error_rerender(self) -> None:
        self.login(*self.editor_credentials)
        html = self.post(self.edit_path, self.base_fields(
            track_paste=PASTE_TEXT,
            track_mode="append",
            track_title=["坏行"],
            track_duration=["nope"],
        ))
        self.assertIn("曲目时长格式", html)
        self.assertIn("01. 曲名 - 艺人 4:12", html)
        self.assertIn('<details class="trk-paste" open>', html)
        self.assertIn('value="append" checked', html)
        self.assertEqual([item[1] for item in self.tracks()], ["Opening", "幻想郷の記憶"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()