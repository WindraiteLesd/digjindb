"""回归测试: 所有 Response 构造必须给出 str 类型的 content_type.

背景: `background_response` 曾用位置参数 `Response(200, bytes, dict)`,
把图片字节当成 content_type, 于是 `_send` 里 `content_type.startswith()`
抛 TypeError -> 请求 500 -> 站点挂掉。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class TestResponseContentType(unittest.TestCase):
    """Response 的 content_type 必须是 str, 否则 _send 会崩。"""

    def test_dataclass_field_order(self) -> None:
        """锁定字段顺序, 提醒后来者别用位置参数。"""
        import dataclasses

        from dddb.public.site import Response

        names = [field.name for field in dataclasses.fields(Response)]
        self.assertEqual(names[:3], ["status", "content_type", "body"],
                         "Response 前三个字段顺序变了, 需检查所有位置参数调用")

    def test_no_positional_response_construction(self) -> None:
        """源码里不许用 `Response(<数字>, ...)` 位置参数构造.

        这种写法极易把 body 传成 content_type(踩过一次, 站点直接挂)。
        统一用 Response.html() / Response.binary() / Response.redirect().
        """
        offenders = []
        for path in (ROOT / "dddb").rglob("*.py"):
            source = path.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_response = (
                    (isinstance(func, ast.Name) and func.id == "Response")
                    or (isinstance(func, ast.Attribute) and func.attr == "Response")
                )
                if not is_response:
                    continue
                # 有位置参数 + 第一个是数字常量 = 可疑的位置参数构造
                if node.args and isinstance(node.args[0], ast.Constant) \
                        and isinstance(node.args[0].value, int):
                    offenders.append(f"{path.name}:{node.lineno}")
        self.assertEqual(offenders, [],
                         f"发现位置参数构造 Response(status, ...): {offenders}")

    def test_background_response_uses_binary(self) -> None:
        import inspect

        from dddb.public.site import PublicSite

        source = inspect.getsource(PublicSite.background_response)
        self.assertIn("Response.binary", source,
                      "背景图响应必须用 Response.binary(data, ctype)")
        self.assertNotIn("Response(200,", source)

    def test_background_response_returns_str_content_type(self) -> None:
        """实际调用各分支, content_type 都得是 str。"""
        from dddb.config import Config
        from dddb.public.site import build_site

        from tests._tmp import temp_dir

        ctx = temp_dir("bgresp-")
        root = Path(ctx.__enter__())
        try:
            config = Config.default(root=root)
            config.ensure_dirs()
            import sqlite3

            conn = sqlite3.connect(str(config.db_path))
            conn.execute(
                "CREATE TABLE IF NOT EXISTS albums (key TEXT PRIMARY KEY, "
                "title TEXT, payload TEXT)")
            conn.commit()
            conn.close()
            site = build_site(config)
            for arg in ("abc", "", "99999", "7"):
                response = site.background_response(arg)
                self.assertIsInstance(
                    response.content_type, str,
                    f"background_response({arg!r}) 的 content_type 不是 str")
                # 模拟 _send 里的操作, 不能抛 TypeError
                response.content_type.startswith("text/html")
        finally:
            ctx.__exit__(None, None, None)

    def test_send_does_not_crash_on_all_responses(self) -> None:
        """关键响应类型过一遍 _send 的危险操作。"""
        from dddb.config import Config
        from dddb.public.site import build_site

        from tests._tmp import temp_dir

        ctx = temp_dir("sendchk-")
        root = Path(ctx.__enter__())
        try:
            config = Config.default(root=root)
            config.ensure_dirs()
            import sqlite3

            conn = sqlite3.connect(str(config.db_path))
            conn.execute(
                "CREATE TABLE IF NOT EXISTS albums (key TEXT PRIMARY KEY, "
                "title TEXT, payload TEXT)")
            conn.commit()
            conn.close()
            site = build_site(config)

            responses = [
                site.background_response("7"),
                site.background_response("abc"),
                site.avatar_response("7"),
            ]
            for response in responses:
                # _send 里真正会抛的地方
                self.assertIsInstance(response.content_type, str)
                response.content_type.startswith("text/html")
                len(response.body)  # body 必须有 len
        finally:
            ctx.__exit__(None, None, None)


class TestBackgroundRoute(unittest.TestCase):
    """背景图路由端到端: 有文件/无文件都要正常。"""

    def test_returns_file_when_present(self) -> None:
        from dddb.config import Config
        from dddb.public.site import build_site

        from tests._tmp import temp_dir

        ctx = temp_dir("bgfile-")
        root = Path(ctx.__enter__())
        try:
            config = Config.default(root=root)
            config.ensure_dirs()
            import sqlite3

            conn = sqlite3.connect(str(config.db_path))
            conn.execute(
                "CREATE TABLE IF NOT EXISTS albums (key TEXT PRIMARY KEY, "
                "title TEXT, payload TEXT)")
            conn.commit()
            conn.close()
            site = build_site(config)
            base = site._backgrounds_dir()
            self.assertIsNotNone(base)
            # 造一张背景图
            png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
            (base / "7.png").write_bytes(png)

            response = site.background_response("7")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.content_type, "image/png")
            self.assertEqual(response.body, png)
            self.assertEqual(response.headers.get("Cache-Control"),
                             "private, max-age=300")
        finally:
            ctx.__exit__(None, None, None)

    def test_404_when_missing(self) -> None:
        from dddb.config import Config
        from dddb.public.site import build_site

        from tests._tmp import temp_dir

        ctx = temp_dir("bgmiss-")
        root = Path(ctx.__enter__())
        try:
            config = Config.default(root=root)
            config.ensure_dirs()
            import sqlite3

            conn = sqlite3.connect(str(config.db_path))
            conn.execute(
                "CREATE TABLE IF NOT EXISTS albums (key TEXT PRIMARY KEY, "
                "title TEXT, payload TEXT)")
            conn.commit()
            conn.close()
            site = build_site(config)
            response = site.background_response("99999")
            self.assertEqual(response.status, 404)
            self.assertIsInstance(response.content_type, str)
        finally:
            ctx.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()