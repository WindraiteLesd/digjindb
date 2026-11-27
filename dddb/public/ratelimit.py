"""按 IP 的令牌桶限速 —— 纯 stdlib, 单进程并发安全.

公开端上线后会直接暴露给互联网, 没限速就等于把 ``http.server`` 的线程池送人
(LibreSSL 之后的 **Slowloris / 大量并发请求**都能把一个免费 VPS 打满)。
这里实现三档令牌桶:

* ``write``  —— 登录/注册/编辑: 正常用户一分钟也提交不了几次, 收紧;
* ``heavy``  —— 封面/静态资源/搜索: 一次页面浏览要抓几十张图, 桶必须够大,
  否则正常浏览就会被 429;
* ``browse`` —— 普通 HTML 页面。

超限时返回 **429 + ``Retry-After``**, 让人和爬虫都知道等多久。

放在公开端(而不是反代)的理由: Caddy/Nginx 的限速规则要额外配置且看不到本站
路由; 站内限速能精确到"只有 /cover 和写接口才重点保护", 而且单测可以直接验证。
"""

from __future__ import annotations

import ipaddress
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping

#: 默认分档: (每秒补充令牌数, 桶容量)
DEFAULT_CLASSES: dict[str, tuple[float, float]] = {
    "browse": (10.0, 30.0),
    "heavy": (10.0, 120.0),
    "search": (2.0, 20.0),
    "write": (0.25, 5.0),
}

#: 走"重活"限额的路由(封面 / 静态资源 / 外链跳转) —— 一次页面浏览要抓几十张图, 桶要大
HEAVY_ROUTES = frozenset({"cover", "static", "link"})

#: 走"检索"限额的路由 —— 每次都是一次全库 LIKE 查询, 人一分钟也搜不了几次
SEARCH_ROUTES = frozenset({"search"})

#: 完全不计入限速的路由(robots.txt / sitemap.xml 要让爬虫随便取)
EXEMPT_ROUTES = frozenset({"robots", "sitemap"})


# --------------------------------------------------------------------------
# 客户端 IP
# --------------------------------------------------------------------------


def _clean_ip(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("[") and "]" in text:            # [::1]:1234
        text = text[1 : text.index("]")]
    elif text.count(":") == 1 and "." in text:          # 127.0.0.1:54321
        text = text.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return ""


def client_ip_from_headers(
    headers: Mapping[str, Any] | None,
    remote_addr: Any = "",
    *,
    trust_proxy: bool = False,
) -> str:
    """取这次请求的真实来源 IP.

    * 默认只信 TCP 层的 ``remote_addr`` —— 否则任何人都能塞一个
      ``X-Forwarded-For: 1.2.3.4`` 把自己的请求算到别人头上, 限速直接失效;
    * ``trust_proxy=True``(确实在 Caddy/Nginx 后面)时才看
      ``X-Forwarded-For``(取最左边一跳)与 ``X-Real-IP``;
    * **TCP 对端是回环地址时自动信任**: 用户 m17937 —— 线上服务只监听
      ``127.0.0.1:8940``, 前面必定是本机 Caddy。能从回环连进来就说明
      "这是本机反代转发给我的", 而外网客户端根本无法造出回环对端
      (socket 的对端地址由内核填, 伪造不了), 所以这条自动判定不比
      显式开关宽松, 却省掉了"部署时忘了开 behind_proxy"这类坑。
    * 解析不出合法 IP 一律退化成 ``"unknown"``(共用一个桶, 宁可收紧不可放空)。
    """
    lowered = {str(k).lower(): v for k, v in (headers or {}).items()}
    peer = _clean_ip(remote_addr)
    if trust_proxy or _is_loopback(peer):
        # Cloudflare 的 ``CF-Connecting-IP`` 由边缘节点**覆盖写**, 客户端塞不了;
        # ``X-Forwarded-For`` 却是"追加", 客户端自己发一个假值会留在链头,
        # 取最左一跳就被人绕开限速了。所以优先认前者。
        for name in ("cf-connecting-ip", "x-forwarded-for", "x-real-ip"):
            raw = lowered.get(name)
            if not raw:
                continue
            for candidate in str(raw).split(","):
                ip = _clean_ip(candidate)
                if ip:
                    return ip
    return peer or "unknown"


def _is_loopback(ip: str) -> bool:
    """``"127.0.0.1"`` / ``"::1"`` 这类回环地址."""
    if not ip:
        return False
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


# --------------------------------------------------------------------------
# 令牌桶
# --------------------------------------------------------------------------


class TokenBucket:
    """经典令牌桶: 桶里最多 ``burst`` 个令牌, 每秒补 ``rate`` 个."""

    __slots__ = ("rate", "burst", "tokens", "updated")

    def __init__(self, rate: float, burst: float, *, tokens: float | None = None,
                 updated: float = 0.0) -> None:
        self.rate = max(0.0, float(rate))
        self.burst = max(1.0, float(burst))
        self.tokens = float(self.burst if tokens is None else min(tokens, self.burst))
        self.updated = float(updated)

    def refill(self, now: float) -> None:
        elapsed = max(0.0, float(now) - self.updated)
        if elapsed > 0:
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.updated = float(now)

    def take(self, now: float, cost: float = 1.0) -> tuple[bool, float, float]:
        """``(放行?, 还要等几秒, 剩余令牌)``."""
        cost = max(0.0, float(cost))
        self.refill(now)
        if self.tokens >= cost:
            self.tokens -= cost
            return True, 0.0, self.tokens
        if self.rate <= 0:
            return False, float("inf"), self.tokens
        return False, (cost - self.tokens) / self.rate, self.tokens


@dataclass(slots=True)
class RateLimitDecision:
    """一次限速判定(直接喂给 429 响应)."""

    allowed: bool
    retry_after: float = 0.0
    remaining: float = 0.0
    klass: str = ""
    key: str = ""

    @property
    def retry_after_seconds(self) -> int:
        """``Retry-After`` 用的整数秒(至少 1)."""
        if self.allowed:
            return 0
        wait = self.retry_after
        if wait != wait or wait == float("inf"):  # NaN / inf
            return 60
        return max(1, int(wait + 0.999))


class RateLimiter:
    """按 ``(档位, 来源)`` 分桶的令牌桶集合.

    线程安全: http.server 每请求一线程, 所有操作都在同一把 ``RLock`` 下。
    空闲桶会被回收, 同时记住的来源数有上限 —— 否则一个伪造 IP 的洪水能让
    字典无限增长(那本身就是一种内存攻击)。
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        classes: Mapping[str, tuple[float, float]] | None = None,
        idle_ttl: float = 600.0,
        max_keys: int = 20000,
    ) -> None:
        self.enabled = bool(enabled)
        table = dict(DEFAULT_CLASSES)
        for name, pair in (classes or {}).items():
            try:
                rate, burst = pair
                table[str(name)] = (max(0.0, float(rate)), max(1.0, float(burst)))
            except (TypeError, ValueError):
                continue
        self.classes = table
        self.idle_ttl = max(1.0, float(idle_ttl))
        self.max_keys = max(64, int(max_keys))
        self._buckets: dict[tuple[str, str], TokenBucket] = {}
        self._lock = threading.RLock()
        self._last_prune = 0.0

    # -- 构造 ---------------------------------------------------------------

    @classmethod
    def from_config(cls, cfg: Any) -> "RateLimiter":
        """从 ``config.public`` 建一个限速器(缺字段就用默认值)."""
        if cfg is None:
            return cls()
        get = lambda name, default: getattr(cfg, name, default)  # noqa: E731
        return cls(
            enabled=bool(get("rate_limit_enabled", True)),
            classes={
                "browse": (float(get("rate_limit_rate", 10.0)), float(get("rate_limit_burst", 30.0))),
                "heavy": (
                    float(get("rate_limit_heavy_rate", 10.0)),
                    float(get("rate_limit_heavy_burst", 120.0)),
                ),
                "search": (
                    float(get("rate_limit_search_rate", 2.0)),
                    float(get("rate_limit_search_burst", 20.0)),
                ),
                "write": (
                    float(get("rate_limit_write_rate", 0.25)),
                    float(get("rate_limit_write_burst", 5.0)),
                ),
            },
            idle_ttl=float(get("rate_limit_idle_ttl", 600.0)),
            max_keys=int(get("rate_limit_max_keys", 20000)),
        )

    # -- 判定 ---------------------------------------------------------------

    def check(
        self,
        key: Any,
        klass: str = "browse",
        *,
        cost: float = 1.0,
        now: float | None = None,
    ) -> RateLimitDecision:
        name = str(klass or "browse")
        identity = str(key or "unknown")
        if not self.enabled:
            rate, burst = self.classes.get(name, DEFAULT_CLASSES["browse"])
            return RateLimitDecision(True, 0.0, float(burst), name, identity)
        rules = self.classes.get(name) or self.classes.get("browse") or DEFAULT_CLASSES["browse"]
        rate, burst = rules
        stamp = time.time() if now is None else float(now)
        with self._lock:
            self._maybe_prune_locked(stamp)
            bucket = self._buckets.get((name, identity))
            if bucket is None:
                if len(self._buckets) >= self.max_keys:
                    self._prune_locked(stamp, force=True)
                bucket = TokenBucket(rate, burst, tokens=burst, updated=stamp)
                self._buckets[(name, identity)] = bucket
            bucket.rate, bucket.burst = rate, burst
            allowed, wait, remaining = bucket.take(stamp, cost)
        return RateLimitDecision(allowed, wait, remaining, name, identity)

    # -- 维护 ---------------------------------------------------------------

    def _maybe_prune_locked(self, now: float) -> None:
        if (now - self._last_prune) >= min(60.0, self.idle_ttl):
            self._prune_locked(now)

    def _prune_locked(self, now: float, *, force: bool = False) -> int:
        self._last_prune = now
        doomed = [
            bucket_key for bucket_key, bucket in self._buckets.items()
            if (now - bucket.updated) > self.idle_ttl
        ]
        for bucket_key in doomed:
            self._buckets.pop(bucket_key, None)
        if force and len(self._buckets) >= self.max_keys:
            oldest = sorted(self._buckets.items(), key=lambda item: item[1].updated)[
                : len(self._buckets) - self.max_keys + 1
            ]
            for bucket_key, _bucket in oldest:
                self._buckets.pop(bucket_key, None)
            doomed.extend(bucket_key for bucket_key, _ in oldest)
        return len(doomed)

    def prune(self, now: float | None = None) -> int:
        stamp = time.time() if now is None else float(now)
        with self._lock:
            return self._prune_locked(stamp)

    def reset(self, key: Any = None) -> int:
        """清掉某个来源(或全部)的桶 —— 测试与运维用."""
        with self._lock:
            if key is None:
                removed = len(self._buckets)
                self._buckets.clear()
                return removed
            identity = str(key)
            doomed = [bk for bk in self._buckets if bk[1] == identity]
            for bucket_key in doomed:
                self._buckets.pop(bucket_key, None)
            return len(doomed)

    def state_size(self) -> int:
        with self._lock:
            return len(self._buckets)

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "classes": {name: {"rate": rate, "burst": burst} for name, (rate, burst) in self.classes.items()},
            "idle_ttl": self.idle_ttl,
            "max_keys": self.max_keys,
            "active_keys": self.state_size(),
        }


def classify_request(method: Any, route_name: Any, query: Mapping[str, Any] | None = None) -> str:
    """方法 + 路由 -> ``"write"`` / ``"search"`` / ``"heavy"`` / ``"browse"`` / ``"exempt"``."""
    verb = str(method or "GET").upper()
    if verb not in ("GET", "HEAD"):
        return "write"
    name = str(route_name or "")
    if name in EXEMPT_ROUTES:
        return "exempt"
    if name in SEARCH_ROUTES:
        return "search"
    if name in HEAVY_ROUTES:
        return "heavy"
    if name == "albums" and isinstance(query, Mapping) and query.get("q"):
        # ``/albums?q=...`` 才是真正的检索(``/search`` 只是跳转)
        return "search"
    return "browse"


__all__ = [
    "DEFAULT_CLASSES",
    "EXEMPT_ROUTES",
    "HEAVY_ROUTES",
    "SEARCH_ROUTES",
    "RateLimitDecision",
    "RateLimiter",
    "TokenBucket",
    "classify_request",
    "client_ip_from_headers",
]