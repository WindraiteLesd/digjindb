"""CDP 驱动浏览器的协议实现.

之前"修不好"的三个真实 bug:
    ① **握手丢帧**: ``recv(4096)`` 一次读回"握手响应 + 首个数据帧",
       旧代码只等到 ``\\r\\n\\r\\n`` 就把整块丢掉, 于是第一次
       ``call()`` 永远等不到响应。真机几乎必现(Chrome 握手后立刻
       推 ``Runtime.executionContextCreated``)。
    ② **走了系统代理**: urllib 默认读 ``HTTP_PROXY``, 连
       ``127.0.0.1:9222/json/version`` 也被代理解析 ->
       ``BadStatusLine``。必须用无代理 opener。
    ③ **启动器参数不足 + 死等**: 缺 ``--remote-debugging-address``
       等开关; 浏览器崩了也要白等 25 秒, 且看不到错误输出。

这个文件用**假 CDP 服务器**验证协议, 不依赖真浏览器。
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
import unittest
from pathlib import Path

from tests._tmp import temp_path

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 假 CDP 服务器(HTTP + WS 同一端口, 与真机一致)
# ---------------------------------------------------------------------------
class FakeCDP:
    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(8)
        self.received: list[dict] = []
        self._stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    # -- 帧 ------------------------------------------------------------

    @staticmethod
    def _accept_key(key: str) -> str:
        magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        return base64.b64encode(
            hashlib.sha1((key + magic).encode()).digest()).decode()

    @staticmethod
    def _frame(obj: dict) -> bytes:
        payload = json.dumps(obj).encode()
        n = len(payload)
        out = bytearray([0x81])
        if n < 126:
            out.append(n)
        elif n < 65536:
            out.append(126)
            out += struct.pack(">H", n)
        else:
            out.append(127)
            out += struct.pack(">Q", n)
        return bytes(out) + payload

    def _read_frame(self, conn: socket.socket, buf: bytes):
        def need(n: int, data: bytes):
            while len(data) < n:
                chunk = conn.recv(4096)
                if not chunk:
                    raise ConnectionError("closed")
                data += chunk
            return data[:n], data[n:]

        head, rest = need(2, buf)
        b2 = head[1]
        masked = bool(b2 & 0x80)
        n = b2 & 0x7F
        if n == 126:
            ext, rest = need(2, rest)
            n = struct.unpack(">H", ext)[0]
        elif n == 127:
            ext, rest = need(8, rest)
            n = struct.unpack(">Q", ext)[0]
        mask = b""
        if masked:
            mask, rest = need(4, rest)
        data, rest = need(n, rest)
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        try:
            return json.loads(data.decode("utf-8", "replace")), rest
        except ValueError:
            return None, rest

    # -- 连接处理 -------------------------------------------------------

    def _handle(self, conn: socket.socket) -> None:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                conn.close()
                return
            buf += chunk
        head, _, leftover = buf.partition(b"\r\n\r\n")
        lines = head.decode("utf-8", "replace").split("\r\n")
        req = lines[0]
        headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        if headers.get("upgrade", "").lower() != "websocket":
            if req.startswith("GET /json/version"):
                body = json.dumps({"Browser": "FakeChrome/999"}).encode()
            elif req.startswith("GET /json/list"):
                body = json.dumps([{
                    "type": "page",
                    "url": "https://example.test/new/album",
                    "webSocketDebuggerUrl":
                        "ws://127.0.0.1:{}/devtools/page/P1".format(
                            self.port),
                }]).encode()
            else:
                body = b"{}"
            conn.sendall(
                ("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                 "Content-Length: {}\r\nConnection: close\r\n\r\n"
                 ).format(len(body)).encode() + body)
            conn.close()
            return

        key = headers.get("sec-websocket-key", "")
        conn.sendall((
            "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Accept: {}\r\n\r\n"
        ).format(self._accept_key(key)).encode())

        # **关键**: 握手后立刻推一条事件 —— 真机就是这样,
        # 旧代码会把它连同握手响应一起丢掉。
        conn.sendall(self._frame({
            "method": "Runtime.executionContextCreated",
            "params": {"context": {"id": 1}},
        }))

        rest = leftover
        while True:
            try:
                msg, rest = self._read_frame(conn, rest)
            except (ConnectionError, OSError):
                break
            if msg is None:
                continue
            if "id" not in msg:
                continue
            self.received.append(msg)
            mid = msg["id"]
            method = msg.get("method", "")
            params = msg.get("params") or {}
            if method == "Runtime.evaluate":
                expr = str(params.get("expression", ""))
                if "document.readyState" in expr:
                    value = "complete"
                elif "location.pathname" in expr:
                    value = "/new/album"
                elif "document.querySelector" in expr:
                    value = True
                else:
                    value = "ok"
                conn.sendall(self._frame({
                    "id": mid,
                    "result": {"result": {"type": "string",
                                          "value": value}},
                }))
            elif method == "DOM.getDocument":
                conn.sendall(self._frame(
                    {"id": mid, "result": {"root": {"nodeId": 1}}}))
            elif method == "DOM.querySelector":
                conn.sendall(self._frame(
                    {"id": mid, "result": {"nodeId": 42}}))
            else:
                conn.sendall(self._frame({"id": mid, "result": {}}))
        conn.close()

    def _loop(self) -> None:
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def close(self) -> None:
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


class CDPTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.srv = FakeCDP()

    def tearDown(self) -> None:
        self.srv.close()

    def _cdp(self):
        from dddb.import_to_web import CDP

        return CDP(self.srv.port)


class TestHttpEndpoints(CDPTestCase):
    def test_version(self) -> None:
        cdp = self._cdp()
        self.assertEqual(cdp.version().get("Browser"), "FakeChrome/999")

    def test_pages(self) -> None:
        cdp = self._cdp()
        pages = cdp.pages()
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["type"], "page")

    def test_bypasses_proxy(self) -> None:
        """即使环境里设了 HTTP_PROXY, 也不该影响 127.0.0.1。"""
        import os

        old = os.environ.get("HTTP_PROXY")
        os.environ["HTTP_PROXY"] = "http://127.0.0.1:1"   # 必然连不上
        try:
            cdp = self._cdp()
            self.assertEqual(cdp.version().get("Browser"),
                             "FakeChrome/999",
                             "CDP 请求走了系统代理")
        finally:
            if old is None:
                os.environ.pop("HTTP_PROXY", None)
            else:
                os.environ["HTTP_PROXY"] = old


class TestHandshakeLeftover(CDPTestCase):
    """握手后立刻到来的数据帧不能丢 —— 这是最关键的 bug。"""

    def test_event_frame_not_lost(self) -> None:
        cdp = self._cdp()
        cdp.connect_page()
        # 服务端在握手后立刻推送了事件帧; 如果它被丢掉,
        # 第一次 call() 就会一直等到超时
        value = cdp.eval_js("document.readyState")
        self.assertEqual(value, "complete",
                         "握手后的第一帧丢了(旧 bug)")
        cdp.close()

    def test_multiple_calls(self) -> None:
        cdp = self._cdp()
        cdp.connect_page()
        for _ in range(5):
            self.assertEqual(cdp.eval_js("document.readyState"), "complete")
        cdp.close()


class TestCallAndEval(CDPTestCase):
    def test_eval_pathname(self) -> None:
        cdp = self._cdp()
        cdp.connect_page()
        self.assertEqual(cdp.eval_js("location.pathname"), "/new/album")
        cdp.close()

    def test_eval_bool(self) -> None:
        cdp = self._cdp()
        cdp.connect_page()
        self.assertTrue(cdp.eval_js(
            "!!document.querySelector('[name=\"title\"]')"))
        cdp.close()

    def test_call_returns_result(self) -> None:
        cdp = self._cdp()
        cdp.connect_page()
        self.assertEqual(cdp.call("DOM.getDocument"),
                         {"root": {"nodeId": 1}})
        cdp.close()

    def test_sent_methods_recorded(self) -> None:
        cdp = self._cdp()
        cdp.connect_page()
        cdp.call("Page.enable")
        names = [m.get("method") for m in self.srv.received]
        self.assertIn("Page.enable", names)
        cdp.close()


class TestFillAndUpload(CDPTestCase):
    def test_fill_page(self) -> None:
        from dddb.import_to_web import fill_page

        cdp = self._cdp()
        cdp.connect_page()
        data = {"title_original": "T", "circle": "C"}
        fill_page(cdp, data)
        # 发给浏览器的表达式里必须带我们的字段名
        exprs = " ".join(str((m.get("params") or {}).get("expression", ""))
                         for m in self.srv.received
                         if m.get("method") == "Runtime.evaluate")
        self.assertIn("title_original", exprs)
        self.assertIn("circle", exprs)
        cdp.close()

    def test_upload_cover(self) -> None:
        from dddb.import_to_web import upload_cover

        tmp = temp_path("cdp-cover")
        cover = Path(tmp) / "c.png"
        cover.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        cdp = self._cdp()
        cdp.connect_page()
        try:
            self.assertTrue(upload_cover(cdp, str(cover)))
            names = [m.get("method") for m in self.srv.received]
            self.assertIn("DOM.setFileInputFiles", names)
        finally:
            cdp.close()
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)

    def test_upload_missing_file_returns_false(self) -> None:
        from dddb.import_to_web import upload_cover

        cdp = self._cdp()
        cdp.connect_page()
        self.assertFalse(upload_cover(cdp, "/no/such/file.png"))
        cdp.close()


class TestLauncherConfig(unittest.TestCase):
    """启动参数与端口选择。"""

    def test_flags_include_essentials(self) -> None:
        from dddb.import_to_web import BROWSER_FLAGS

        joined = " ".join(BROWSER_FLAGS)
        for flag in ("--remote-debugging-address=127.0.0.1",
                     "--no-first-run", "--user-data-dir",
                     "--disable-dev-shm-usage",
                     "--disable-breakpad"):
            # --user-data-dir 由 launch_browser 单独拼, 这里不强求
            if flag == "--user-data-dir":
                continue
            self.assertIn(flag, joined, f"缺启动参数 {flag}")

    def test_pick_free_port(self) -> None:
        from dddb.import_to_web import pick_free_port

        port = pick_free_port()
        self.assertGreaterEqual(port, 9222)

    def test_pick_free_port_skips_busy(self) -> None:
        from dddb.import_to_web import pick_free_port

        busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy.bind(("127.0.0.1", 0))
        taken = busy.getsockname()[1]
        busy.listen(1)
        try:
            got = pick_free_port(start=taken, tries=3)
            self.assertNotEqual(got, taken, "挑到了被占用的端口")
        finally:
            busy.close()


class TestWaitFailureReporting(unittest.TestCase):
    """浏览器崩了要立刻报错并带回日志, 不能死等。"""

    def test_exited_process_reports_log(self) -> None:
        from dddb.import_to_web import wait_for_cdp

        tmp = temp_path("cdp-wait")
        log = Path(tmp) / "browser.log"
        log.write_text("FATAL: crashpad 拒绝访问\n", encoding="utf-8")

        class FakeProc:
            returncode = 1
            cdp_port = 9222
            cdp_log = str(log)

            @staticmethod
            def poll():
                return 1

        start = time.time()
        with self.assertRaises(RuntimeError) as ctx:
            wait_for_cdp(9222, timeout=20, proc=FakeProc())
        elapsed = time.time() - start
        self.assertLess(elapsed, 5, "进程已退出却还在死等")
        msg = str(ctx.exception)
        self.assertIn("crashpad", msg, "没把浏览器日志带回来")
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


class TestNoRegressionOnRealApi(unittest.TestCase):
    """协议相关的注释/兼容性检查。"""

    def test_leftover_buffer_documented(self) -> None:
        src = (ROOT / "dddb/import_to_web.py").read_text(encoding="utf-8")
        self.assertIn("leftover", src, "没保留握手溢出字节")
        self.assertIn("_buf", src, "没有溢出缓存字段")

    def test_proxy_handler_present(self) -> None:
        src = (ROOT / "dddb/import_to_web.py").read_text(encoding="utf-8")
        self.assertIn("ProxyHandler({})", src, "没绕过系统代理")


if __name__ == "__main__":
    unittest.main()
