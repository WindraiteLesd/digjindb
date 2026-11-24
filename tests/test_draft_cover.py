"""草稿要能记住封面 (m12280).

file input 的值既存不进 localStorage 也恢复不回来, 所以前端把选过的图
转成 data URL 放进隐藏域; 草稿的 ``extra`` 槽负责收它, 新建表单则让它
随表单提交, 服务端 ``_store_cover_from_data`` 兜底落盘。
"""
from __future__ import annotations

import base64
import io
import pathlib
import re
import shutil
import struct
import unittest
import zlib

from dddb.db import Store
from dddb.models import Album
from dddb.public.site import build_site

ROOT = pathlib.Path(__file__).resolve().parent.parent
JS = (ROOT / "dddb/public/static/public.js").read_text(encoding="utf-8")
TPL = (ROOT / "dddb/public/templates.py").read_text(encoding="utf-8")


def _png() -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", crc))
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
            + chunk(b"IEND", b""))


def _data_url(blob: bytes = b"") -> str:
    return "data:image/png;base64," + base64.b64encode(blob or _png()).decode()


def _block(src: str, name: str) -> str:
    """按花括号配平截出 function name(...) {...} 的函数体."""
    i = src.find("function " + name)
    if i < 0:
        raise AssertionError("找不到函数 " + name)
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError("花括号不配平: " + name)


class TestDraftCollectsCover(unittest.TestCase):
    """前端: 草稿要收下封面 data URL."""

    def test_collect_extra_exists(self):
        body = _block(JS, "collectExtra")
        self.assertIn('getElementById("cover-data")', body)
        self.assertIn("extra.cover_data", body)

    def test_apply_extra_writes_back(self):
        body = _block(JS, "applyExtra")
        self.assertIn('getElementById("cover-data")', body)
        self.assertIn("extra.cover_data", body)
        # 派发 change —— setupCoverPick 靠它把图贴回 <img>
        self.assertIn('cover.dispatchEvent(new Event("change"', body)

    def test_save_stores_extra(self):
        body = _block(JS, "save")
        self.assertIn("collectExtra()", body)
        self.assertIn("extra: extra", body)

    def test_snapshot_includes_extra(self):
        body = _block(JS, "snapshotOf")
        self.assertIn("values: values", body)
        self.assertIn("extra: extra", body)

    def test_restore_applies_extra(self):
        self.assertIn("applyExtra(saved.extra)", JS)

    def test_same_draft_check_uses_extra(self):
        # 载入时判断「草稿与页面一致」也要看封面, 否则会误删带封面的草稿
        idx = JS.index("snapshotOf(collect(), collectExtra())")
        self.assertIn("snapshotOf(saved.values, saved.extra)", JS[idx:idx + 200])

    def test_cover_pick_writes_store(self):
        body = _block(JS, "setupCoverPick")
        self.assertIn('getElementById("cover-data")', body)
        self.assertIn("store.value = small", body)
        # 原图 base64 动辄 1.3M 字符, 先缩到 900px 再存
        self.assertIn("shrinkDataUrl(data, 900)", body)
        self.assertIn("1500000", body)
        self.assertIn("草稿里无法保留这张预览", body)

    def test_shrink_helper_exists(self):
        body = _block(JS, "shrinkDataUrl")
        self.assertIn("new Image()", body)
        self.assertIn("canvas", body)
        self.assertIn("toDataURL", body)
        self.assertIn("900", body)  # 默认边长上限

    def test_collect_skips_cover_data(self):
        """同一份 base64 存两遍会撑爆 localStorage(用户 m12486: 还是不行)。"""
        body = _block(JS, "collect")
        self.assertIn('if (name === "cover_data")', body)
        self.assertNotIn("values.cover_data", body)

    def test_cover_pick_listens_change(self):
        body = _block(JS, "setupCoverPick")
        self.assertIn('store.addEventListener("change"', body)
        self.assertIn("已恢复草稿里的封面预览", body)


class TestNewFormCarriesCoverData(unittest.TestCase):
    """模板: 新建页的隐藏域要随表单提交."""

    def test_creating_branch_has_cover_data(self):
        i = TPL.index("if creating:")
        j = TPL.index("else:", i)
        self.assertIn('id="cover-data"', TPL[i:j])

    def test_cover_data_has_name(self):
        # 带 name 才会被表单提交 —— 刷新后封面既看得见也提交得上
        idx = TPL.index('id="cover-data"')
        line = TPL[TPL.rindex("<input", 0, idx):idx + 40]
        self.assertIn('name="cover_data"', line)

    def test_edit_mode_has_no_cover_data(self):
        # 编辑页封面走 cover-crop.js 即时上传, 不需要草稿兜底
        k = TPL.index("_cards: dict[str, str] = {}")
        j = TPL.index("else:", TPL.index("if creating:"))
        self.assertNotIn('id="cover-data"', TPL[j:k])


class TestServerStoresCoverData(unittest.TestCase):
    """服务端: cover_data 能落成封面, 坏输入不炸."""

    def setUp(self):
        self.tmp = pathlib.Path(__file__).resolve().parent / "_tmp_covdata"
        shutil.rmtree(self.tmp, ignore_errors=True)
        (self.tmp / "covers").mkdir(parents=True)
        self.db = str(self.tmp / "db.sqlite3")
        Store(self.db).close()
        self.site = build_site(
            None, root=self.tmp, db_path=self.db,
            users_db=str(self.tmp / "users.sqlite3"),
            sessions_db=str(self.tmp / "sess.sqlite3"),
            covers_dir=str(self.tmp / "covers"),
        )
        self.key = "测试|草稿封面|2026"
        store = Store(self.db)
        store.upsert_album(Album.from_dict({
            "key": self.key, "title_original": "草稿封面测试",
            "circle": "测试", "release_date": "2026-01-01",
        }))
        store.close()
        self.site.data.invalidate()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_helper_exists(self):
        self.assertTrue(hasattr(self.site, "_store_cover_from_data"))

    def test_stores_real_png(self):
        ok = self.site._store_cover_from_data(
            self.key, {"cover_data": [_data_url()]})
        self.assertTrue(ok)
        row = self.site.data.get_album(self.key)
        self.assertTrue(row.get("cover_local"))
        self.assertTrue(pathlib.Path(row["cover_local"]).is_file())
        # 视图里的 cover 带 cache-busting 后缀
        self.assertIn("?v=", row.get("cover") or "")

    def test_rejects_bad_input_without_raising(self):
        bad = [
            {"cover_data": [""]},
            {"cover_data": ["http://x/y.jpg"]},
            {"cover_data": ["data:text/plain;base64,aGk="]},
            {"cover_data": ["data:image/png;base64,!!!!"]},
            {},
        ]
        for values in bad:
            with self.subTest(values=values):
                self.assertFalse(
                    self.site._store_cover_from_data(self.key, values))

    def test_rejects_oversize(self):
        big = _data_url(b"\x89PNG\r\n\x1a\n" + b"0" * (2 * 1024 * 1024 + 10))
        self.assertFalse(
            self.site._store_cover_from_data(self.key, {"cover_data": [big]}))

    def test_new_entity_fallback_order(self):
        src = io.open(ROOT / "dddb/public/site.py", encoding="utf-8").read()
        # 上传文件 → cover_data → cover_url_hint
        a = src.index("_save_album_cover_file(key, values)")
        b = src.index("_store_cover_from_data(key, values)", a)
        c = src.index("_store_cover_from_url(key, _hint)", b)
        self.assertLess(a, b)
        self.assertLess(b, c)


if __name__ == "__main__":
    unittest.main()
