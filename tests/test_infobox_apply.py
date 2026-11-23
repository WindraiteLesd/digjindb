"""「填入 infobox」必须真的落到表单里, 而不只是改写隐藏域.

用户实测(m10181): 贴了 11 首曲目 + circle + artists + 词条的完整 infobox,
点「填入」后**只有标题 / 发售日 / 发行公司**生效。三个独立原因:

1. ``circle`` / ``event`` / ``tags`` / ``artists`` 在页面上是 **chip 控件**,
   真正的输入框没有 ``name``(只有 ``data-name``), 提交值在隐藏域里。
   ``infobox.js`` 直接改隐藏域, 而 ``public.js`` 的 chip 组件自己维护
   ``items`` 数组、``sync()`` 时用它覆盖隐藏域 —— 下一次交互就把值抹掉。
2. 词条 / 外链是**成对行**, 页面上只有用户手动点过「＋ 添加」才有行;
   infobox 里条目更多时, 多出来的部分无处可放。
3. 站长端「导出这张」给的地址是 ``/new/album``(新建页), 而新建分支
   ``_do_new_entity`` 完全没读 ``track_paste`` —— 曲目 100% 丢失。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INFOBOX_JS = ROOT / "dddb" / "public" / "static" / "infobox.js"
PUBLIC_JS = ROOT / "dddb" / "public" / "static" / "public.js"
SITE = ROOT / "dddb" / "public" / "site.py"
TRACKPARSE = ROOT / "dddb" / "trackparse.py"


def _code(text: str) -> str:
    """去掉注释, 免得断言命中注释里的同名字样。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _js(path: Path) -> str:
    return _code(path.read_text(encoding="utf-8"))


class TestBindFieldsGetRealChips(unittest.TestCase):
    """chip 类字段要经由它自己的同步机制写, 不能只改隐藏域。"""

    def setUp(self) -> None:
        self.js = _js(INFOBOX_JS)
        self.pub = _js(PUBLIC_JS)

    def test_set_bind_field_fires_change(self) -> None:
        i = self.js.find("function setBindField(")
        self.assertGreater(i, 0)
        block = self.js[i:i + 700]
        self.assertIn('fire(hidden, "input")', block)
        # 只派发 input 不够: chip 组件的监听挂在 change 上
        self.assertIn('fire(hidden, "change")', block)

    def test_bind_fields_use_set_bind_field(self) -> None:
        i = self.js.find("function applyInfobox(")
        end = self.js.find("\n  window.", i)
        block = self.js[i:end if end > i else i + 6000]
        self.assertIn("setBindField(k,", block)

    def test_artists_and_tags_go_through_bind(self) -> None:
        i = self.js.find('["artists", "tags", "genres"]')
        self.assertGreater(i, 0)
        block = self.js[i:i + 500]
        self.assertIn("setBindField", block)

    def test_bind_component_listens_for_change(self) -> None:
        i = self.pub.find("function setupBindFields(")
        self.assertGreater(i, 0)
        block = self.pub[i:i + 4000]
        self.assertIn("addEventListener(\"change\"", block)
        self.assertIn("readHidden", block)

    def test_people_chips_listen_for_change(self) -> None:
        i = self.pub.find("function setupPeopleChips(")
        self.assertGreater(i, 0)
        block = self.pub[i:i + 12000]
        self.assertIn("readHidden", block)
        self.assertIn("addEventListener(\"change\"", block)


class TestRowsAreCreatedOnDemand(unittest.TestCase):
    """词条 / 外链条数超过页面现有行数时要能补行。"""

    def setUp(self) -> None:
        self.js = _js(INFOBOX_JS)

    def test_ensure_rows_helper_exists(self) -> None:
        i = self.js.find("function ensureRows(")
        self.assertGreater(i, 0)
        block = self.js[i:i + 900]
        self.assertIn('data-pair-add="', block)
        self.assertIn(".click()", block)

    def test_ensure_rows_trims_extra(self) -> None:
        i = self.js.find("function ensureRows(")
        block = self.js[i:i + 900]
        # 「填入」是整份替换, 留着旧行会变成"原有 + 新增"的混合数据
        self.assertIn(".remove()", block)

    def test_links_call_ensure_rows(self) -> None:
        i = self.js.find("if (d.links.length)")
        self.assertGreater(i, 0)
        block = self.js[i:i + 700]
        self.assertIn('ensureRows("url"', block)

    def test_credits_call_ensure_rows(self) -> None:
        i = self.js.find("if (d.credits.length)")
        self.assertGreater(i, 0)
        block = self.js[i:i + 900]
        self.assertIn('ensureRows("credit"', block)

    def test_credit_people_typed_into_input(self) -> None:
        i = self.js.find("function addCreditPeople(")
        self.assertGreater(i, 0)
        block = self.js[i:i + 900]
        self.assertIn("credit-person-input", block)
        self.assertIn("KeyboardEvent", block)
        self.assertIn('key: "Enter"', block)


class TestCoverIsReported(unittest.TestCase):
    """封面已经没有任何输入框, 填不进去必须说出来。"""

    def test_cover_mentions_manual_upload(self) -> None:
        js = _js(INFOBOX_JS)
        i = js.find("function applyInfobox(")
        block = js[i:]
        self.assertIn("封面需手动上传", block)


class TestNewAlbumKeepsTracks(unittest.TestCase):
    """新建页也要解析 track_paste —— 站长端导出的地址就是 /new/album。"""

    def setUp(self) -> None:
        self.src = SITE.read_text(encoding="utf-8")

    def test_new_album_reads_track_paste(self) -> None:
        i = self.src.find("def _do_new_entity(")
        self.assertGreater(i, 0)
        block = self.src[i:i + 6000]
        self.assertIn("track_paste", block)
        self.assertIn("parse_tracklist", block)
        self.assertIn("merge_pasted_tracks", block)

    def test_new_album_tracks_use_merged_list(self) -> None:
        i = self.src.find("def _do_new_entity(")
        block = self.src[i:i + 6000]
        self.assertIn('"tracks": list(patch.get("track_list") or [])', block)


class TestTrackArtistSplitting(unittest.TestCase):
    """网易云/infobox 里的 ``曲名 / A、B`` 要拆成两个艺人。"""

    def setUp(self) -> None:
        from dddb.trackparse import parse_tracklist

        self.parse = parse_tracklist

    def test_ideographic_comma(self) -> None:
        got = self.parse("1. What I thought (feat. Letsu) / N_dog、Letsu")
        self.assertEqual(got[0].artists, ["N_dog", "Letsu"])

    def test_three_artists(self) -> None:
        got = self.parse("2. WOW'N'FLUTTER / N_dog、icesawder、Yutori")
        self.assertEqual(got[0].artists, ["N_dog", "icesawder", "Yutori"])

    def test_single_artist(self) -> None:
        got = self.parse("11. OUTRO (DAC) / N_dog")
        self.assertEqual(got[0].artists, ["N_dog"])

    def test_slash_still_works(self) -> None:
        got = self.parse("3. Song / A / B")
        self.assertEqual(got[0].artists, ["A", "B"])


class TestInfoboxParseSplitsArtists(unittest.TestCase):
    """infobox 文本里的曲目艺人同样要按「、」拆开。"""

    def setUp(self) -> None:
        from dddb import infobox

        self.ib = infobox

    def test_ideographic_comma_in_track(self) -> None:
        d = self.ib.parse(
            "[dddb]\n"
            "title_original || T\n"
            "tracks || 1. Song / N_dog、Letsu\n")
        self.assertEqual(d["tracks"][0]["artists"], ["N_dog", "Letsu"])

    def test_user_sample_roundtrip(self) -> None:
        text = (
            "[dddb]\n"
            "key || netease:404613799\n"
            "title_original || AD/DA CONVERTER\n"
            "circle || N_dog\n"
            "company || N_dog\n"
            "release_date || 2026-10-30\n"
            "artists || N_dog / Letsu\n"
            "tracks || 1. What I thought (feat. Letsu) / N_dog、Letsu\n"
        )
        d = self.ib.parse(text)
        self.assertEqual(d["meta"]["title_original"], "AD/DA CONVERTER")
        self.assertEqual(d["meta"]["artists"], ["N_dog", "Letsu"])
        self.assertEqual(d["meta"]["release_date"], "2026-10-30")
        self.assertEqual(d["tracks"][0]["artists"], ["N_dog", "Letsu"])


if __name__ == "__main__":
    unittest.main()
