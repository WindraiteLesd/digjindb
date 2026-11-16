"""名称去重 + 实体卡片渲染测试."""

from __future__ import annotations

import unittest

from dnh.public import templates as T


class TestAlbumNames(unittest.TestCase):
    def _page(self, **extra) -> str:
        album = {
            "key": "k", "href": "/album/k", "title": "T", "tags": [], "genres": [],
            "credits": [], "urls": [], "missing": [], "has_cover": False,
            "track_count": 0, "date": "", "event": "", "circle": "", "artists": [],
            "description": "", "cover": "", "catalog_no": "", "price": "", "booth": "",
            "hall": "", "updated_text": "", "title_alt": "", "romaji": "",
            "event_kind": "", "year": "", "tracks": [],
        }
        album.update(extra)
        return T.album_detail_page(T.SiteContext(csrf_token="t"), album=album,
                                   entities=extra.pop("entities", ()))

    def test_credits_split_and_dedupe(self) -> None:
        """多来源合并产生的 ``"X / X / X"`` 只显示一次.

        注意: **社团类词条不进制作者区块**(用户要求), 所以这里只验证
        艺人/制作人员组的去重; 社团名字本身仍应出现在页面上(标题下方)。
        """
        html = self._page(circle="Login Records", credits=[
            {"label": "社团", "names": ["Login Records / Login Records / Login Records"]},
            {"label": "艺人", "names": ["A、A、B", "B"]},
        ])
        # 不再出现"X / X / X"这种合并残留
        self.assertEqual(html.count("Login Records / Login Records"), 0)
        # 社团只在标题下方出现一次, 不进制作人员
        self.assertNotIn("<dt>社团</dt>", html)
        self.assertIn("Login Records", html)
        # 艺人组: 每个人一条按钮(不再用 "A、B" 合并成一个)
        self.assertIn("<b>A</b>", html)
        self.assertIn("<b>B</b>", html)

    def test_names_not_repeated_in_info_table(self) -> None:
        html = self._page(circle="C", artists=["Alpha"])
        self.assertNotIn("<dt>社团</dt><dd>", html.split("<dl class=\"info\">")[1].split("</dl>")[0])
        self.assertNotIn("<dt>艺人</dt>", html.split("<dl class=\"info\">")[1].split("</dl>")[0])

    def test_entity_cards_have_follow_form(self) -> None:
        html = self._page(entities=[
            {"kind": "circle", "name": "C", "href": "/circle/C",
             "image": "https://example.com/x.png", "meta": [("专辑", "3 张")],
             "description": "简介", "following": False},
            {"kind": "artist", "name": "Alpha", "href": "/albums?q=Alpha",
             "meta": [("专辑", "2 张")], "following": True},
        ])
        # 艺人卡片在 people-chips 里(带悬停卡片与关注表单)。
        # 注意: circle 类型的实体不再进 chips(社团改为显示在标题后),
        # 这个用例的 album 没有 circle, 所以只剩艺人卡片。
        self.assertIn('class="entity-chip"', html)
        self.assertIn('class="entity-card"', html)
        self.assertIn('action="/follows/toggle"', html,
                      "艺人卡片要有关注表单")
        self.assertIn("已关注", html)
        self.assertNotIn("关注这个社团", html)

    def test_tracklist_title_only_once(self) -> None:
        html = self._page(tracks=[{"index": 1, "disc": 1, "title": "A",
                                   "artists": [], "duration_text": "1:00"}])
        self.assertEqual(html.count("曲目表"), 1)


if __name__ == "__main__":
    unittest.main()