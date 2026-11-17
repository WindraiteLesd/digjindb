"""测试支撑工具: fixture 加载与轻量假 HttpClient(不发真实请求)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def fixture_text(name: str, filename: str) -> str:
    """读取文本 fixture(统一 LF)."""
    path = FIXTURES / name / filename
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def fixture_json(name: str, filename: str) -> Any:
    return json.loads(fixture_text(name, filename))


class FakeResponse:
    """最小可用的 HttpClient Response 替身."""

    def __init__(self, payload: Any = None, *, text: str = "", status: int = 200, url: str = "") -> None:
        self._payload = payload
        self._text = text
        self.status = status
        self.url = url
        self.headers: dict[str, str] = {"content-type": "application/json"}
        self.body = text.encode("utf-8")
        self.from_cache = False
        self.elapsed = 0.0
        self.encoding = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        if self._text:
            return self._text
        if self._payload is None:
            return ""
        return json.dumps(self._payload, ensure_ascii=False)

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def json(self) -> Any:
        return self._payload


class FakeHttp:
    """记录请求并按 ``handler`` 返回预设响应的假客户端.

    handler 签名: ``(method, url, params, json_body, headers, kw) -> payload``;
    返回值可以是任意 JSON 可序列化对象, 也可以是 :class:`FakeResponse`。
    """

    def __init__(self, handler: Callable[..., Any] | None = None) -> None:
        self.handler = handler
        self.calls: list[dict[str, Any]] = []

    # -- 记录 ---------------------------------------------------------------

    def _record(self, method: str, url: str, params: Mapping[str, Any] | None,
                json_body: Any, headers: Mapping[str, str] | None, kw: Mapping[str, Any]) -> None:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": dict(params or {}),
                "json_body": json_body,
                "headers": dict(headers or {}),
                "kw": dict(kw),
            }
        )

    def _dispatch(self, method: str, url: str, **kw: Any) -> FakeResponse:
        params = kw.get("params")
        json_body = kw.get("json_body")
        headers = kw.get("headers")
        self._record(method, url, params, json_body, headers, {k: v for k, v in kw.items() if k not in ("params", "json_body", "headers")})
        if self.handler is None:
            return FakeResponse({}, url=url)
        result = self.handler(method, url, dict(params or {}), json_body, dict(headers or {}), kw)
        if isinstance(result, FakeResponse):
            return result
        return FakeResponse(result, url=url)

    # -- HttpClient 接口 ----------------------------------------------------

    def request(self, method: str, url: str, **kw: Any) -> FakeResponse:
        return self._dispatch(method.upper(), url, **kw)

    def get(self, url: str, **kw: Any) -> FakeResponse:
        return self._dispatch("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> FakeResponse:
        return self._dispatch("POST", url, **kw)

    def get_json(self, url: str, **kw: Any) -> Any:
        return self._dispatch("GET", url, **kw).json()

    def post_json(self, url: str, **kw: Any) -> Any:
        return self._dispatch("POST", url, **kw).json()

    @property
    def urls(self) -> list[str]:
        return [c["url"] for c in self.calls]


class BaseCase(unittest.TestCase):
    """统一断言助手."""

    def assert_in(self, needle: str, haystack: Any, msg: str = "") -> None:
        self.assertIn(needle, str(haystack), msg)

    def assert_titles(self, tracks: Sequence[Any], expected: Sequence[str]) -> None:
        self.assertEqual([t.title for t in tracks], list(expected))

    def assert_durations(self, tracks: Sequence[Any], expected: Sequence[Any]) -> None:
        self.assertEqual([t.duration for t in tracks], list(expected))
