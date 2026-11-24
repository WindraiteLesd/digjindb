"""新建专辑页必须与编辑页一致(同一套卡片界面).

历史差距: `/new/album` 用的是 `new_entity_page` —— 19 个纯文本框;
编辑页是卡片布局 + 封面槽位 + 制作人员按钮 + 词条/外链行 + 曲目表 + 草稿条。
两个界面完全不同, 用户在两边看到的东西不一致。

现在: 新建页复用 `edit_album_page(creating=True)`, 只有提交目标/面包屑/
删除按钮/封面上传方式不同。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _ctx():
    from dddb.public import templates as T

    return T.SiteContext(csrf_token="tok", is_editor=True, lang="zh",
                         user={"id": 1, "username": "ed", "role": "editor"})


def _new_page() -> str:
    from dddb.public import templates as T

    return T.edit_album_page(
        _ctx(), album={}, values=T.edit_values_from_album({}),
        creating=True, bind_options={"artist": []})


def _edit_page() -> str:
    from dddb.public import templates as T

    album = {
        "key": "k|2026", "title": "T", "title_original": "T",
        "artists": ["甲"], "credits": [{"role": "mastering", "name": "丙"}],
        "urls": {"x": "https://e.com/"}, "tracks": [
            {"disc": 1, "index": 1, "title": "曲", "artists": [],
             "duration": 100}],
    }
    return T.edit_album_page(
        _ctx(), album=album, values=T.edit_values_from_album(album),
        bind_options={"artist": []})


class TestNewPageMatchesEdit(unittest.TestCase):
    """两个界面共用的结构必须一致。"""

    def setUp(self) -> None:
        self.new = _new_page()
        self.edit = _edit_page()

    # -- 必须一致的结构 --
    SHARED = (
        'id="edit-cards"',            # 卡片网格
        'data-card="cover"',
        'data-card="info"',
        'data-card="category"',
        'data-card="description"',
        'data-card="credits"',
        'data-card="links"',
        'data-card="tracks"',         # 曲目表卡片
        "data-draft-bar",             # 草稿条
        'data-people="artists"',      # 制作人员按钮
        "credit-people-box",          # 词条行(人员输入框)
        "credit-person-input",
        'data-pair-add="credit"',     # 添加词条
        'data-pair-add="url"',        # 添加外链
        'name="url_key"',             # 外链行
        'name="track_title"',         # 曲目列
        'name="track_disc"',
        'name="track_artists"',
        'name="track_duration"',
        'name="title_original"',
        'name="description"',
        'name="csrf_token"',
        "edit-form",
    )

    def test_shared_structure(self) -> None:
        for token in self.SHARED:
            with self.subTest(token=token):
                self.assertIn(token, self.new,
                              f"新建页缺 {token}(与编辑页不一致)")
                self.assertIn(token, self.edit, f"编辑页缺 {token}")

    def test_draft_bar_outside_cards(self) -> None:
        for label, html in (("新建", self.new), ("编辑", self.edit)):
            self.assertLess(html.find("data-draft-bar"),
                            html.find('id="edit-cards"'),
                            f"{label}页草稿条应在卡片外")

    # -- 必须不同的地方 --
    def test_form_action_differs(self) -> None:
        self.assertIn('action="/new/album"', self.new)
        self.assertIn("/album/k%7C2026/edit", self.edit)

    def test_new_has_key_input(self) -> None:
        """新建时给专辑地址标识(留空自动生成), 但折在「高级」里。"""
        self.assertIn('name="album_key"', self.new)
        self.assertIn("留空自动生成", self.new)
        self.assertNotIn('name="album_key"', self.edit)
        # 默认收起来: 不打扰正常新建流程(m13013)
        self.assertIn('<details class="edit-advanced">', self.new)
        self.assertIn("高级：自定义专辑地址标识", self.new)
        self.assertNotIn('class="edit-advanced" open', self.new)

    def test_advanced_block_is_collapsed_details(self) -> None:
        """「高级」必须是 <details> 且默认关闭, 不当普通字段杵在表单顶上。"""
        self.assertIn('<details class="edit-advanced">', self.new)
        self.assertIn("<summary>", self.new)
        self.assertNotIn('<details class="edit-advanced" open', self.new)
        # 输入框在 details 里面(在 summary 之后、</details> 之前)
        start = self.new.index('<details class="edit-advanced">')
        end = self.new.index("</details>", start)
        self.assertIn('name="album_key"', self.new[start:end])

    def test_advanced_hint_explains_key(self) -> None:
        """提示要说清它是网址里那一段, 不是随便的编号。"""
        self.assertIn("专辑地址标识", self.new)
        self.assertIn("网址里的这一段就是它", self.new)
        self.assertIn("建好后不再变", self.new)

    def test_advanced_css_exists(self) -> None:
        css = (ROOT / "dddb/public/static/public.css").read_text(encoding="utf-8")
        self.assertIn(".edit-advanced {", css)
        self.assertIn(".edit-advanced summary {", css)
        self.assertIn(".edit-advanced[open] summary {", css)

    def test_new_has_no_delete(self) -> None:
        """新建时还没有专辑, 不该有删除按钮。"""
        self.assertNotIn("删除这张专辑", self.new)
        self.assertNotIn("delete-album-form", self.new)
        # 编辑页有
        self.assertIn("删除这张专辑", self.edit)

    def test_new_cover_uploads_with_form(self) -> None:
        """新建时共没有 key, 封面随主表单提交(不走异步上传)。"""
        self.assertIn('name="cover"', self.new)
        self.assertNotIn("data-cover-action", self.new)
        # 编辑页走异步上传
        self.assertIn("data-cover-action", self.edit)

    def test_breadcrumbs(self) -> None:
        self.assertIn("新建专辑", self.new)
        self.assertNotIn("正在编辑", self.new)
        self.assertIn("正在编辑", self.edit)

    def test_not_old_plain_form(self) -> None:
        """不能退回到 19 个纯文本框的旧表单。"""
        # 旧表单的特征: 有 credits/urls/tracks 三个 textarea
        self.assertNotIn('<textarea name="credits"', self.new)
        self.assertNotIn('<textarea name="urls"', self.new)
        self.assertNotIn('<textarea name="tracks"', self.new)


class TestNewPageParses(unittest.TestCase):
    """新建页提交的字段必须能被 parse_album_form 解析(否则丢数据)。"""

    def test_card_fields_round_trip(self) -> None:
        from dddb.public.site import parse_album_form

        values = {
            "album_key": ["k|2026"],
            "title_original": ["原标题"],
            "title": ["显示名"],
            "title_romaji": ["Romaji"],
            "circle": ["社团"],
            "release_date": ["2026-11-17"],
            "event": ["M3-2026秋"],
            "catalog_no": ["K-1"],
            "price": ["1000"],
            "description": ["简介"],
            "tags": ["a、b"],
            "genres": ["Pop、Rock"],
            "artists": ["甲\n乙"],
            "credit_role": ["mastering", "Illustration"],
            "credit_name": ["Ryo", "いぬすけ"],
            "url_key": ["Bandcamp", "YouTube"],
            "url_value": ["https://a.bandcamp.com/",
                          '<iframe src="https://www.youtube.com/embed/x">'
                          "</iframe>"],
            "track_disc": ["1", "1"],
            "track_index": ["1", "2"],
            "track_title": ["曲一", "曲二"],
            "track_artists": ["甲", "乙"],
            "track_lang": ["ja", ""],
            "track_genre": ["Pop", ""],
            "track_duration": ["3:42", "222"],
        }
        patch, errors = parse_album_form(values)
        self.assertFalse(errors, f"不该有错: {errors}")
        self.assertEqual(patch["title_original"], "原标题")
        self.assertEqual(patch["artists"], ["甲", "乙"])
        self.assertEqual(len(patch["credits"]), 2)
        self.assertEqual(patch["credits"][0]["role"], "mastering")
        self.assertIn("Bandcamp", patch["urls"])
        self.assertEqual(len(patch["track_list"]), 2)
        self.assertEqual(patch["track_list"][0]["duration"], 222)
        self.assertEqual(patch["tags"], ["a", "b"])


class TestNewEntityStillWorks(unittest.TestCase):
    """其它条目(社团/艺人/展会/标签)仍走原来的简单表单。"""

    def test_other_kinds_unchanged(self) -> None:
        from dddb.public import templates as T

        for kind in ("circle", "artist", "event", "tag"):
            with self.subTest(kind=kind):
                html = T.new_entity_page(_ctx(), kind)
                self.assertTrue(html.strip(), f"{kind} 页面为空")
                # 这些是简单表单, 没有卡片网格
                self.assertNotIn('id="edit-cards"', html)


if __name__ == "__main__":
    unittest.main()
