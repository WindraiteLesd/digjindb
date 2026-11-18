"""测试: 多值换行拆分 + 新表单结构约束."""

from __future__ import annotations

import unittest

from dddb.public import site as S
from dddb.public import templates as T


class TestSplitNames(unittest.TestCase):
    def test_newline_is_primary(self) -> None:
        self.assertEqual(S.PublicSite._split_names("甲\n乙"), ["甲", "乙"])
        self.assertEqual(S.PublicSite._split_names("甲\r\n乙"), ["甲", "乙"])

    def test_dunhao_kept_when_multiline(self) -> None:
        """多行时行内"、"应保留(名字本身可能含顿号)."""
        self.assertEqual(S.PublicSite._split_names("A、B 组合\nC"), ["A、B 组合", "C"])

    def test_single_line_still_splits_dunhao(self) -> None:
        self.assertEqual(S.PublicSite._split_names("甲、乙"), ["甲", "乙"])

    def test_dedupe_and_trim(self) -> None:
        self.assertEqual(S.PublicSite._split_names(" 甲 \n甲\n乙 "), ["甲", "乙"])
        self.assertEqual(S.PublicSite._split_names(""), [])


class TestNewFormLayout(unittest.TestCase):
    def _html(self, kind: str = "album", options=None) -> str:
        ctx = T.SiteContext(csrf_token="t", is_editor=True,
                            user={"username": "ed", "role": "editor"})
        return T.new_entity_page(ctx, kind, bind_options=options or {})

    def test_album_layout_rules(self) -> None:
        html = self._html()
        body = html.split('data-entity-form="album"')[1]

        def pos(token: str) -> int:
            return body.index(token)

        # 封面最上, 标题随后
        self.assertLess(pos('name="cover"'), pos('name="title"'))
        # 弃用 key / cover_url, 新建不编辑罗马音
        self.assertNotIn('name="key"', body)
        self.assertNotIn('name="cover_url"', body)
        self.assertNotIn('name="title_romaji"', body)
        # 同组字段: 位置相邻且落在同一 <div class="form-grid"> 段内
        for first, second in (('name="circle"', 'name="artists"'),
                              ('name="release_date"', 'name="event"'),
                              ('name="hall"', 'name="booth"'),
                              ('name="catalog_no"', 'name="price"')):
            a, b = pos(first), pos(second)
            self.assertLess(a, b, f"{first} 应在 {second} 之前")
            segment = body[a:b]
            self.assertNotIn("</section>", segment[:segment.index("<label")] if "<label" in segment else segment,
                             f"{first} 与 {second} 应在同一分组")
            self.assertLess(b - a, 1400, f"{first} 与 {second} 应相邻(同一行)")
        # 日期控件
        self.assertIn('type="date"', body)
        # 曲目/词条/外链是文本域
        for field in ("tracks", "credits", "urls"):
            self.assertIn(f'<textarea name="{field}"', body)

    def test_suggestions_hidden_by_default(self) -> None:
        html = self._html(options={"circle": [{"id": 1, "name": "サークル"}]})
        self.assertIn('class="suggest-item"', html)
        self.assertIn("hidden>", html)
        self.assertIn('class="suggest-avatar"', html)

    def test_artist_event_tag_forms(self) -> None:
        # tag 的「说明」(note) 已从表单移除(描述类字段全去掉)
        for kind, field in (("artist", "nationality"),
                            ("event", "start_date"),
                            ("tag", "kind")):
            html = self._html(kind)
            self.assertIn(f'name="{field}"', html)
        artist = self._html("artist")
        self.assertLess(artist.index('name="cover"'),
                        artist.index('name="name"'),
                        "头像上传在最顶部")


if __name__ == "__main__":
    unittest.main()