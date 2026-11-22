"""封面上传的加固点(第二轮).

用户第二轮反馈: 点了「上传封面」, 提示"已上传 1 张", 但左侧仍显示"无封面"。

代码走查发现三个真风险:
    ① ``uploadOne`` 从 ``data-cover-next`` 反推封面 URL —— 格式一变就
       得到 ``/cover/?v=...``, 图裂, 看起来像"没上传"
    ② ``addSlot`` 的 asPrimary 判定里直接 ``thumbImg.getAttribute(...)``,
       thumbImg 为 null 时抛异常 —— 而异常发生在**加完槽位之后**,
       正是"槽位有了、提示有了、主图没变"
    ③ ``setPrimary`` 开头判 ``!thumbImg`` 直接 return, 引用失效后永不更新

加固:
    * 服务端给 ``data-cover-src``, 前端不再反推
    * addSlot 前判 thumbImg
    * setPrimary 取不到就重查 DOM
    * **确定性兜底**: 上传成功后 400ms 检查主图, 还是占位就刷新页面
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "dddb/public/static/public.js"
TPL = ROOT / "dddb/public/templates.py"


def _js() -> str:
    return JS.read_text(encoding="utf-8")


def _gallery() -> str:
    js = _js()
    i = js.find("function setupCoverGallery(")
    if i < 0:
        return ""
    m = re.search(r"\n  function \w+\(", js[i + 10:])
    return js[i:i + 10 + (m.start() if m else len(js))]


class TestServerProvidesCoverSrc(unittest.TestCase):
    def test_data_cover_src_rendered(self) -> None:
        from dddb.public import templates as T

        ctx = T.SiteContext(csrf_token="t", is_editor=True, lang="zh",
                            user={"id": 1, "username": "e", "role": "editor"})
        album = {"key": "A|B|2026", "title_original": "B",
                 "has_cover": False, "cover_local": "", "cover": ""}
        html = T.edit_album_page(ctx, album=album,
                                 values=T.edit_values_from_album({}),
                                 creating=False)
        m = re.search(r'data-cover-src="([^"]*)"', html)
        self.assertIsNotNone(m, "模板没给 data-cover-src")
        self.assertTrue(m.group(1).startswith("/cover/"))

    def test_template_has_attribute(self) -> None:
        src = TPL.read_text(encoding="utf-8")
        self.assertIn("data-cover-src=", src)


class TestUploadUsesCoverSrc(unittest.TestCase):
    def test_not_reverse_engineered(self) -> None:
        """uploadOne 不该再从 data-cover-next 反推路径。"""
        block = _gallery()
        self.assertIn("data-cover-src", block,
                      "uploadOne 没用服务端给的封面地址")

    def test_has_fallback_match(self) -> None:
        """真拿不到时用 /album/<key>/edit 的 key 兜底。"""
        block = _gallery()
        self.assertIn("/album/", block)

    def test_prefers_cover_src(self) -> None:
        block = _gallery()
        idx = block.find("data-cover-src")
        self.assertGreater(idx, 0)


class TestThumbImgSafety(unittest.TestCase):
    def test_addslot_checks_thumbimg(self) -> None:
        """addSlot 调用前必须先判 thumbImg(否则抛异常断掉 setPrimary)。"""
        block = _gallery()
        idx = block.find("addSlot(src, file.name")
        self.assertGreater(idx, 0, "找不到 addSlot 调用")
        window = block[max(0, idx - 400):idx]
        self.assertIn("thumbImg", window,
                      "addSlot 前没判 thumbImg")

    def test_setprimary_requeries_dom(self) -> None:
        """setPrimary 取不到 thumbImg 要重查 DOM。"""
        block = _gallery()
        i = block.find("function setPrimary(")
        self.assertGreater(i, 0)
        body = block[i:i + 700]
        self.assertIn("querySelector", body,
                      "setPrimary 没有重查 DOM 的兜底")


class TestDeterministicFallback(unittest.TestCase):
    """最关键的一条: 上传后主图没更新就刷新。"""

    def test_refresh_when_still_empty(self) -> None:
        block = _gallery()
        self.assertIn("is-empty", block)
        # 要有"检查 + 跳转"的逻辑
        self.assertIn("window.location.href", block)
        idx = block.find("is-empty", block.find("chain.then"))
        self.assertGreater(idx, 0, "没有在 chain.then 里检查 is-empty")

    def test_uses_timeout(self) -> None:
        block = _gallery()
        self.assertIn("setTimeout", block,
                      "没延时等 setPrimary 生效")

    def test_refresh_has_cache_bust(self) -> None:
        block = _gallery()
        seg = block[block.find("setTimeout"):]
        self.assertIn("Date.now()", seg)


class TestStillNoDuplicateUpload(unittest.TestCase):
    """上一轮的修复不能被这次改动回退。"""

    def test_upload_btn_delegates(self) -> None:
        js = _js()
        i = js.find("function setupCoverUpload(")
        m = re.search(r"\n  function \w+\(", js[i + 10:])
        block = js[i:i + 10 + (m.start() if m else len(js))]
        self.assertIn("hasGallery", block)
        self.assertIn("input.click()", block)

    def test_credit_extra_fields(self) -> None:
        """词条的原名/别名不能被顺手删掉。"""
        src = TPL.read_text(encoding="utf-8")
        for name in ("credit_original", "credit_alias"):
            self.assertIn(name, src, f"缺 {name}")


if __name__ == "__main__":
    unittest.main()
