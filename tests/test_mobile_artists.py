"""窄屏曲目表必须显示艺人(以前被 display:none 藏掉)."""

from __future__ import annotations

import re

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "dnh/public/static/public.css"


def _css() -> str:
    return CSS.read_text(encoding="utf-8")



class TestMobileArtistsVisible(unittest.TestCase):
    """历史 bug: 窄屏 .track-artists { display: none } 直接把艺人藏了。"""

    def test_no_hide_rule(self) -> None:
        css = _css()
        # 去掉 /* */ 注释后再查(注释里会提到旧规则)
        code = re.sub(r"/\*[\s\S]*?\*/", "", css)
        self.assertNotIn(".tracklist .track-artists { display: none; }", code,
                         "窄屏不能把艺人整列藏掉")
        for m in re.finditer(r"\.track-artists[^{]*\{([^}]*)\}", code):
            body = m.group(1)
            # :empty 时隐藏是允许的(没艺人本来就不该占位)
            if ":empty" in m.group(0):
                continue
            self.assertNotIn("display: none", body,
                             "艺人不能 display:none: {}".format(
                                 m.group(0)[:80]))

    def test_mobile_block_shows_artists(self) -> None:
        """820px 内的曲目表规则要显式让艺人可见。"""
        css = _css()
        # 窄屏规则可能写在多个 @media (max-width: 820px) 块里, 全局找
        code = re.sub(r"/\*[\s\S]*?\*/", "", css)
        # 有多条 .tracklist td.track-artists 规则, 取**带 display 的那条**
        rules = [m.group(1) for m in re.finditer(
            r"\.tracklist td\.track-artists \{([^}]*)\}", code)]
        self.assertTrue(rules, "缺窄屏艺人规则")
        self.assertTrue(any("display: block" in b for b in rules),
                        "艺人规则要显式 display: block, 实际: {}".format(rules))
        # 并且确实放在窄屏媒体查询里
        idx = code.find(".tracklist td.track-artists {")
        before = code[:idx]
        self.assertIn("@media (max-width: 820px)", before,
                      "艺人规则要在窄屏媒体查询内")

    def test_grid_layout_has_artist_area(self) -> None:
        """卡片式布局必须给艺人一个独立区域。"""
        css = _css()
        self.assertIn("grid-template-areas", css)
        m = re.search(r"\.tracklist tr\.track-item \{([^}]*)\}", css)
        self.assertIsNotNone(m)
        self.assertIn("artist", m.group(1), "grid 模板里要有 artist 区")

    def test_template_renders_artists_cell(self) -> None:
        """详情页模板要输出艺人单元格。"""
        tpl = (ROOT / "dnh/public/templates.py").read_text(encoding="utf-8")
        self.assertIn('class="track-artists"', tpl)
        self.assertIn('esc("、".join(str(a) for a in artists))', tpl)

    def test_artists_have_data(self) -> None:
        """有艺人数据时单元格非空(不是渲染成空串)."""
        from dnh.public import templates as T

        ctx = T.SiteContext(csrf_token="t", lang="zh")
        html = T.album_detail_page(ctx, album={
            "key": "d", "title": "T", "title_original": "T",
            "tracks": [
                {"disc": 1, "index": 1, "title": "Cur", "artists": ["甲", "乙"],
                 "duration": 100},
                {"disc": 1, "index": 2, "title": "NoArtist", "artists": [],
                 "duration": 120},
            ],
            "credits": [], "urls": [], "tags": [], "genres": [], "missing": [],
            "track_count": 2,
        })
        m = re.findall(r'<td class="track-artists">([^<]*)</td>', html)
        self.assertTrue(m, "模板里找不到艺人单元格")
        self.assertIn("甲、乙", m[0], "多人要用顿号连接")

    def test_empty_artists_dont_take_space(self) -> None:
        """没艺人时那行不留空白高度。"""
        css = _css()
        self.assertIn(".tracklist td.track-artists:empty", css)


if __name__ == "__main__":
    unittest.main()
