"""统一异常体系."""

from __future__ import annotations

from typing import Any


class DnhError(Exception):
    """所有本项目异常的基类."""


class Cancelled(DnhError):
    """任务被主动中止(面板上的「停止」按钮).

    单独一个类型是为了让 :meth:`Pipeline._emit` **原样向上抛** ——
    进度回调里的异常通常要吞掉(不能让日志回调搞崩采集), 但取消必须穿透。
    """


class ConfigError(DnhError):
    """配置文件缺失 / 非法."""


class SourceError(DnhError):
    """数据源适配器失败."""

    def __init__(self, message: str, *, source: str | None = None) -> None:
        super().__init__(message)
        self.source = source


class ParseError(SourceError):
    """页面 / 响应解析失败."""


class HttpError(DnhError):
    """HTTP 层错误."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        url: str | None = None,
        body: bytes | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.url = url
        self.body = body

    def __str__(self) -> str:  # pragma: no cover - 展示用
        base = super().__str__()
        bits = []
        if self.status is not None:
            bits.append(f"status={self.status}")
        if self.url:
            bits.append(f"url={self.url}")
        return f"{base} ({', '.join(bits)})" if bits else base


class RateLimited(HttpError):
    """被目标站点限流."""

    def __init__(self, message: str = "rate limited", *, retry_after: float | None = None, **kw: Any) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class NotAuthenticated(DnhError):
    """需要登录态但未登录 / 登录已过期."""


class NeteaseApiError(DnhError):
    """网易云接口返回业务错误码."""

    def __init__(self, message: str, *, code: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload


class ProxyError(DnhError):
    """代理不可用."""


class BackendUnavailable(SourceError):
    """某个 X 后端不可用(无 token / 无可用实例)."""
