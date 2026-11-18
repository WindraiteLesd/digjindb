"""基于标准库的 HTTP 客户端.

特性:

* 按域名自动选择直连 / 代理(见 :class:`~dddb.config.ProxyConfig`)
* **遵守 robots.txt**(per-host 缓存 + 显式豁免名单 + Crawl-delay 转最小间隔)
* 可识别的 User-Agent(``dddb/<version> (+<联系地址>)``), 只在必须浏览器
  UA 的站点(VGMdb 的 cf_clearance)上伪装
* 指数退避重试, 尊重 ``Retry-After``
* 同域名最小请求间隔限速
* SQLite 响应缓存(便于离线重放)
* gzip / deflate 解压与多编码文本解码

对外只暴露 :class:`HttpClient` 与 :class:`Response`(robots 的结构与判定函数也一并
导出, 供 ``dddb robots`` 命令与单测使用)。
"""

from __future__ import annotations

import gzip
import io
import json as jsonlib
import os
import random
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import __version__
from .cache import HttpCache
from .config import Config
from .errors import HttpError, ProxyError, RateLimited

_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 524}
_TEXT_DECODERS = ("utf-8", "cp932", "shift_jis", "gb18030", "big5", "euc-kr", "latin-1")

_MIB = 1024 * 1024

#: robots.txt 最多读这么多字节(更大的当解析不了处理)
ROBOTS_MAX_BYTES = 512 * 1024

#: 取 robots.txt 的超时(秒) —— 它不该拖慢一次采集
ROBOTS_FETCH_TIMEOUT = 10.0

#: robots.txt 文件名里的 host 白名单(挡掉 ``../`` 这类路径穿越)
_SAFE_FILE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _scheme_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(str(url)).scheme or "").lower()
    except ValueError:
        return ""


def _netloc_of(url: str) -> str:
    """``host[:port]``(robots.txt 要按真实 netloc 取, 端口不能丢)."""
    try:
        return (urllib.parse.urlsplit(str(url)).netloc or "").lower()
    except ValueError:
        return ""


def host_matches(host: str, hosts: Iterable[str]) -> bool:
    """host 是否落在某个后缀名单里(``example.com`` 匹配 ``example.com`` 与 ``www.example.com``)."""
    target = str(host or "").strip().lower()
    if not target:
        return False
    for entry in hosts or []:
        suffix = str(entry or "").strip().lower().lstrip(".")
        if suffix and (target == suffix or target.endswith("." + suffix)):
            return True
    return False


# --------------------------------------------------------------------------
# robots.txt
# --------------------------------------------------------------------------


class RobotsDisallowed(HttpError):
    """robots.txt 明确禁止抓取这个 URL.

    单独一个异常类型, 好让上层能把它和"网络不通 / 403"区分开:
    前者重试无用, 后者可以退避重试。
    """

    def __init__(self, host: str, path: str, *, url: str = "", rule: str = "") -> None:
        detail = rule or "Disallow: /"
        super().__init__(
            f"robots.txt 禁止抓取 {host}{path} (命中 {detail})", url=url or f"https://{host}{path}"
        )
        self.host = host
        self.path = path
        self.rule = detail


@dataclass(slots=True, frozen=True)
class RobotsRule:
    """一条 ``Allow`` / ``Disallow`` 规则."""

    allow: bool
    pattern: str

    @property
    def label(self) -> str:
        return f"{'Allow' if self.allow else 'Disallow'}: {self.pattern}"

    def to_dict(self) -> dict[str, Any]:
        return {"allow": self.allow, "pattern": self.pattern}


@dataclass
class RobotsPolicy:
    """某个 host 的 robots 判定结果(已解析, 可直接用)."""

    host: str
    rules: list[RobotsRule] = field(default_factory=list)
    crawl_delay: float = 0.0
    #: HTTP 状态码; 0 = 网络层面没拿到
    status: int = 0
    fetched_at: float = 0.0
    #: 是否来自缓存(内存 / 落盘)
    from_cache: bool = False
    #: 该 host 在豁免名单里(跳过了 robots 检查)
    exempt: bool = False
    #: 本次是否真的取到了 robots.txt(``--offline`` 且无缓存时为 False)
    fetched: bool = True
    reason: str = ""
    #: 原始 robots.txt 文本(落盘缓存要存它; 豁免时为 "")
    text: str = ""

    @property
    def disallow_all(self) -> bool:
        return any((not rule.allow) and rule.pattern in ("/", "/*") for rule in self.rules)

    def allows(self, path: str) -> tuple[bool, str]:
        """``(是否允许, 命中的规则说明)``; 最长匹配优先, 同长时 Allow 优先."""
        if self.exempt:
            return True, "host 已显式豁免 robots 检查"
        candidates = [str(path or "/")]
        unquoted = urllib.parse.unquote(candidates[0])
        if unquoted != candidates[0]:
            candidates.append(unquoted)
        best: tuple[int, bool, str] | None = None
        for rule in self.rules:
            if not rule.pattern:
                continue
            if not any(_robots_match(rule.pattern, candidate) for candidate in candidates):
                continue
            score = len(rule.pattern)
            if best is None or score > best[0] or (score == best[0] and rule.allow and not best[1]):
                best = (score, rule.allow, rule.pattern)
        if best is None:
            return True, ""
        return best[1], f"{'Allow' if best[1] else 'Disallow'}: {best[2]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "status": self.status,
            "crawl_delay": self.crawl_delay,
            "rules": [rule.to_dict() for rule in self.rules],
            "exempt": self.exempt,
            "from_cache": self.from_cache,
            "reason": self.reason,
        }


def _robots_path(url: str) -> str:
    """URL -> robots 匹配用的路径(含查询串, 空则 ``/``)."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "/"
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return path


def _robots_match(pattern: str, path: str) -> bool:
    """robots 通配匹配: ``*`` = 任意长度, 结尾 ``$`` = 精确到末尾(否则是前缀匹配)."""
    if not pattern:
        return False
    anchor_end = pattern.endswith("$")
    body = pattern[:-1] if anchor_end else pattern
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    try:
        if anchor_end:
            return re.match("^" + regex + r"\Z", path) is not None
        return re.match("^" + regex, path) is not None
    except re.error:  # pragma: no cover - 构造出来的正则不会坏
        return False


def parse_robots(text: str, user_agent: str) -> tuple[list[RobotsRule], float]:
    """解析 robots.txt, 返回 ``(适用于该 UA 的规则, Crawl-delay)``.

    按 RFC 9309 的基本形态实现:

    * ``User-agent`` 连续行属于同一组(直到出现规则行);
    * ``*`` 组是兜底; 命中了具名 UA 组就不再并入 ``*`` 组;
    * 一个 UA 被多组命中时合并;
    * ``Disallow:`` 空值 = 不限制(丢弃该规则); ``Allow:`` 同理。
    """
    agents: list[str] = []
    groups: list[tuple[list[str], list[RobotsRule], float]] = []
    rules: list[RobotsRule] = []
    delay = 0.0
    seen_rule = False

    def flush() -> None:
        nonlocal agents, rules, delay, seen_rule
        if agents:
            groups.append((list(agents), list(rules), delay))
        agents, rules, delay, seen_rule = [], [], 0.0, False

    for raw in re.split(r"\r\n|\r|\n", str(text or "")):
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        directive, _, value = line.partition(":")
        directive = directive.strip().lower()
        value = value.strip()
        if directive == "user-agent":
            if seen_rule:
                # 上一组已经有规则了 -> 新的一组开始
                flush()
            agent = value.strip()
            if agent:
                agents.append(agent)
            continue
        if directive in ("allow", "disallow"):
            seen_rule = True
            if not agents:
                continue  # 没有 User-agent 的规则行: 忽略
            if not value:
                continue  # 空值 = 不限制
            rules.append(RobotsRule(allow=directive == "allow", pattern=value))
            continue
        if directive == "crawl-delay":
            seen_rule = True
            try:
                delay = max(delay, float(value))
            except ValueError:
                pass
    flush()

    ua = str(user_agent or "").lower()
    best_specificity = -1
    selected: list[tuple[list[RobotsRule], float]] = []
    for group_agents, group_rules, group_delay in groups:
        specificity = -1
        for agent in group_agents:
            token = agent.lower()
            if token == "*":
                specificity = max(specificity, 0)
            elif token and token in ua:
                specificity = max(specificity, len(token))
        if specificity < 0:
            continue
        if specificity > best_specificity:
            best_specificity = specificity
            selected = [(group_rules, group_delay)]
        elif specificity == best_specificity:
            selected.append((group_rules, group_delay))

    if not selected:
        return [], 0.0
    merged: list[RobotsRule] = []
    crawl_delay = 0.0
    for group_rules, group_delay in selected:
        merged.extend(group_rules)
        crawl_delay = max(crawl_delay, group_delay)
    return merged, crawl_delay


def _sanitize_robots_filename(host: str) -> str:
    return _SAFE_FILE_RE.sub("_", str(host or "unknown").lower()) or "unknown"


class RobotsCache:
    """robots.txt 的落盘缓存(``<cache_dir>/robots/<host>.json``).

    只存"取到的原始结果"(状态码 / 文本 / 时间), 解析在读取时重做 ——
    这样解析逻辑升级后旧缓存立刻生效, 不必等 TTL 过期。
    """

    def __init__(self, directory: Path | str | None) -> None:
        self.directory = Path(directory) if directory else None

    def _path(self, host: str) -> Path | None:
        if self.directory is None:
            return None
        return self.directory / f"{_sanitize_robots_filename(host)}.json"

    def get(self, host: str, *, now: float | None = None, ttl: float = 86400.0) -> dict[str, Any] | None:
        path = self._path(host)
        if path is None or not path.is_file():
            return None
        stamp = time.time() if now is None else float(now)
        try:
            payload = jsonlib.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        fetched_at = float(payload.get("fetched_at") or 0.0)
        if ttl <= 0 or (stamp - fetched_at) > ttl:
            return None
        return payload

    def put(self, host: str, *, status: int, text: str, fetched_at: float) -> None:
        path = self._path(host)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"host": host, "status": int(status), "text": str(text or ""),
                       "fetched_at": float(fetched_at)}
            path.write_text(jsonlib.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError:  # 缓存失败不该影响抓取
            return

    def clear(self) -> int:
        if self.directory is None or not self.directory.is_dir():
            return 0
        removed = 0
        for item in self.directory.glob("*.json"):
            try:
                item.unlink()
                removed += 1
            except OSError:
                continue
        return removed


def decode_body(body: bytes, content_type: str = "") -> str:
    """按 charset 猜测解码字节串(不抛异常)."""
    charset = ""
    if content_type:
        for part in content_type.split(";"):
            part = part.strip()
            if part.lower().startswith("charset="):
                charset = part.split("=", 1)[1].strip().strip("\"'")
                break
    candidates: list[str] = []
    if charset:
        candidates.append(charset)
    candidates.extend(_TEXT_DECODERS)
    for enc in candidates:
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", "replace")


@dataclass
class Response:
    url: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    from_cache: bool = False
    elapsed: float = 0.0
    encoding: str = ""
    _text: str | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        if self._text is None:
            self._text = decode_body(self.body, self.headers.get("content-type", ""))
        return self._text

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")

    def json(self) -> Any:
        text = self.text.strip()
        if not text:
            raise HttpError("响应为空, 无法解析 JSON", status=self.status, url=self.url)
        try:
            return jsonlib.loads(text)
        except ValueError:
            # 兼容 JSONP: callback({...});
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    return jsonlib.loads(text[start : end + 1])
                except ValueError:
                    pass
            raise HttpError(
                f"JSON 解析失败: {text[:200]!r}", status=self.status, url=self.url, body=self.body[:2000]
            )


class HttpClient:
    """带代理 / 重试 / 限速 / 缓存的 HTTP 客户端."""

    def __init__(
        self,
        config: Config | None = None,
        *,
        cache: HttpCache | None = None,
        logger: Any = None,
        default_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config or Config.default()
        self.log = logger
        self._cache = cache
        self._cookie_jar = CookieJar()
        self._lock = threading.Lock()
        self._last_hit: dict[str, float] = {}
        self._ua_index = random.randrange(1024)
        self._openers: dict[bool, urllib.request.OpenerDirector] = {}
        # robots: 内存缓存(host -> RobotsPolicy) + 落盘缓存 + 串行取用锁。
        # 一个 host 在 TTL 内**只取一次** robots.txt —— 锁在整个"取 + 解析"期间持有,
        # 并发线程不会重复请求(robots.txt 很小, 这点串行代价可以接受)。
        self._robots: dict[str, RobotsPolicy] = {}
        self._robots_lock = threading.Lock()
        self._robots_store = RobotsCache(self._robots_cache_dir())
        self._robots_warned: set[str] = set()
        self._robots_fetches = 0
        #: ``sources.per_source.<名字>.respect_robots`` -> host 映射(惰性计算)
        self._source_robots_map: dict[str, bool] | None = None
        self._source_robots_host_names: dict[str, str] = {}
        self.default_headers: dict[str, str] = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
            "Accept-Language": self.config.http.accept_language,
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        if default_headers:
            self.default_headers.update(default_headers)

    # -- 基础设施 -----------------------------------------------------------

    @property
    def cache(self) -> HttpCache | None:
        if self._cache is None:
            try:
                self.config.ensure_dirs()
                self._cache = HttpCache(self.config.cache_dir / "http_cache.sqlite3")
            except OSError:
                self._cache = None
        return self._cache

    def _next_user_agent(self) -> str:
        agents = self.config.general.user_agents or []
        if not agents:
            return self.identity_user_agent()
        with self._lock:
            self._ua_index = (self._ua_index + 1) % len(agents)
            return agents[self._ua_index]

    def identity_user_agent(self) -> str:
        """可识别的自我标识: ``dddb/<version> (+<联系地址>)``."""
        contact = str(getattr(self.config.http, "contact", "") or "").strip()
        base = f"dddb/{__version__}"
        return f"{base} (+{contact})" if contact else base

    def user_agent_for(self, host: str) -> str:
        """按 host 选 UA: 显式配置 > 必须浏览器 UA 的站点 > 可识别自我标识."""
        explicit = str(getattr(self.config.http, "user_agent", "") or "").strip()
        if explicit:
            return explicit
        if host_matches(host, getattr(self.config.http, "browser_ua_hosts", None) or []):
            return self._next_user_agent()
        return self.identity_user_agent()

    # -- robots.txt ---------------------------------------------------------

    def _robots_cache_dir(self) -> Path | None:
        try:
            return self.config.cache_dir / "robots"
        except (AttributeError, OSError, TypeError):  # pragma: no cover
            return None

    def robots_exempt_reason(self, host: str) -> str:
        """``""`` = 要遵守 robots; 否则返回跳过检查的原因."""
        http = self.config.http
        if not bool(getattr(http, "respect_robots", True)):
            return "respect_robots = false(全局关闭)"
        if host_matches(host, getattr(http, "robots_override_hosts", None) or []):
            return "host 在 robots_override_hosts 里(显式豁免)"
        source = self._source_robots_overrides().get(str(host or "").strip().lower())
        if source is False:
            name = self._source_robots_names().get(str(host or "").strip().lower(), "")
            return f"数据源 {name} 配置了 respect_robots = false(单源豁免)"
        return ""

    def robots_exempt(self, host: str) -> bool:
        return bool(self.robots_exempt_reason(host))

    def _source_robots_overrides(self) -> dict[str, bool]:
        """``[sources.per_source.<名字>] respect_robots`` -> ``{host: 值}``.

        数据源适配器不感知 robots, 所以这里按各源声明的 ``homepage`` 把
        "单源豁免" 映射到 host 上 —— 这样 ``[sources.per_source.vgmdb]
        respect_robots = false`` 才能真正生效(否则只能用 host 名单)。
        惰性计算 + 缓存, 失败一律当作没有豁免。
        """
        if self._source_robots_map is None:
            mapping: dict[str, bool] = {}
            names: dict[str, str] = {}
            try:
                from .sources import REGISTRY, source_class

                for name in REGISTRY:
                    value = self.config.sources.opt(name, "respect_robots", None)
                    if value is None:
                        continue
                    try:
                        host = _host_of(str(getattr(source_class(name), "homepage", "") or ""))
                    except Exception:  # 单个源导入失败不影响其它源
                        continue
                    if host:
                        mapping[host] = bool(value)
                        names[host] = name
            except Exception:  # pragma: no cover - 极端环境下没有数据源注册表
                mapping, names = {}, {}
            self._source_robots_map = mapping
            self._source_robots_host_names = names
        return self._source_robots_map

    def _source_robots_names(self) -> dict[str, str]:
        self._source_robots_overrides()
        return self._source_robots_host_names

    def _warn_exempt(self, host: str, reason: str) -> None:
        if host in self._robots_warned:
            return
        self._robots_warned.add(host)
        if self.log:
            self.log.warning("robots: %s 已显式豁免 robots.txt 检查 —— %s", host, reason)

    def _robots_ttl(self, status: int) -> float:
        http = self.config.http
        if status == 0 or status == 429 or status >= 500:
            return max(1.0, float(getattr(http, "robots_error_ttl", 300) or 300))
        return max(1.0, float(getattr(http, "robots_cache_ttl", 86400) or 86400))

    def _policy_from(self, host: str, status: int, text: str, fetched_at: float) -> RobotsPolicy:
        """HTTP 结果 -> 判定(严格按 RFC 9309 的三种访问结果).

        * "Successful Access" (2xx): 按解析出的规则办;
        * "Unavailable" (400-499, §2.3.1.3): **允许访问任意资源** —— 404 固然是
          "没有 robots.txt", 401/403 也算"对我们不可用", RFC 明确写了 MAY access
          any resources, 所以这里**不**把 403 当成全站禁止(dojin-music 的
          robots.txt 常年 403, 一刀切成禁止会直接打断采集, 且不符合 RFC);
        * "Unreachable" (5xx / 网络故障 / 429, §2.3.1.4): **必须假设全站禁止**,
          但只记 :attr:`HttpConfig.robots_error_ttl`(默认 5 分钟), 免得一次抖动
          让某个站点一整天都不能采。
        """
        code = int(status)
        if 200 <= code < 300:
            rules, delay = parse_robots(text, self.identity_user_agent())
            return RobotsPolicy(
                host=host, rules=rules, crawl_delay=delay, status=code,
                fetched_at=fetched_at, text=text, reason=f"robots.txt 可读(HTTP {code})",
            )
        if code == 0 or code == 429 or code >= 500:
            label = f"HTTP {code}" if code else "网络/代理故障"
            if code == 429:
                label = "HTTP 429(被限流)"
            ttl = int(self._robots_ttl(code))
            return RobotsPolicy(
                host=host, rules=[RobotsRule(False, "/")], status=code, fetched_at=fetched_at,
                text=text,
                reason=f"{label} 取不到 robots.txt -> 按 RFC 9309 暂假设全站禁止({ttl}s 后重试)",
            )
        if 400 <= code < 500:
            hint = "robots.txt 不可用(HTTP {})".format(code)
            if code in (404, 410):
                hint = f"robots.txt 不存在(HTTP {code})"
            elif code in (401, 403):
                hint = f"robots.txt 对我们不可用(HTTP {code})"
            return RobotsPolicy(
                host=host, rules=[], status=code, fetched_at=fetched_at, text=text,
                reason=f"{hint} -> 按 RFC 9309 视为不限制",
            )
        # 3xx 等: urllib 已跟随跳转, 走到这里只能是奇怪的状态码 —— 不限制更安全
        return RobotsPolicy(
            host=host, rules=[], status=code, fetched_at=fetched_at, text=text,
            reason=f"HTTP {code} -> 未识别, 按不限制处理",
        )

    def _load_robots_from_disk(self, host: str, *, now: float) -> RobotsPolicy | None:
        payload = self._robots_store.get(host, now=now)
        if payload is None:
            return None
        status = int(payload.get("status") or 0)
        fetched_at = float(payload.get("fetched_at") or 0.0)
        if (now - fetched_at) > self._robots_ttl(status):
            return None
        policy = self._policy_from(host, status, str(payload.get("text") or ""), fetched_at)
        policy.from_cache = True
        return policy

    def _fetch_robots(self, host: str, scheme: str = "https", netloc: str = "") -> RobotsPolicy:
        url = f"{scheme or 'https'}://{netloc or host}/robots.txt"
        status = 0
        text = ""
        try:
            resp = self.request(
                "GET", url, use_cache=False, retries=1, raise_for_status=False,
                timeout=min(ROBOTS_FETCH_TIMEOUT, float(self.config.http.timeout or ROBOTS_FETCH_TIMEOUT)),
                _respect_robots=False,
            )
            status = int(getattr(resp, "status", 0) or 0)
            if 200 <= status < 300:
                text = decode_body(
                    (getattr(resp, "body", b"") or b"")[:ROBOTS_MAX_BYTES],
                    getattr(resp, "content_type", ""),
                )
        except Exception as exc:  # 取不到 robots 不该把请求弄崩(按禁止 + 短 TTL 处理)
            status = 0
            if self.log:
                self.log.warning("取 robots.txt 失败 %s: %s", host, exc)
        self._robots_fetches += 1
        policy = self._policy_from(host, status, text, time.time())
        self._robots_store.put(host, status=status, text=text, fetched_at=policy.fetched_at)
        if self.log:
            self.log.info("robots: %s -> %s", host, policy.reason)
        return policy

    def robots_policy(
        self,
        host: str,
        *,
        refresh: bool = False,
        allow_fetch: bool = True,
        scheme: str = "https",
        netloc: str = "",
    ) -> RobotsPolicy:
        """取(并缓存)某 host 的 robots 判定; 每个 host 在 TTL 内只请求一次.

        ``allow_fetch=False``(``dddb robots --offline``)只读缓存, 绝不发请求;
        缓存里也没有就返回"状态未知", 由调用方决定怎么显示。
        """
        target = str(host or "").strip().lower()
        if not target:
            return RobotsPolicy(host="", reason="没有 host", exempt=True)
        now = time.time()
        reason = self.robots_exempt_reason(target)
        if reason:
            policy = RobotsPolicy(host=target, exempt=True, status=0, fetched_at=now,
                                  reason=f"已豁免: {reason}")
            with self._robots_lock:
                self._robots[target] = policy
            self._warn_exempt(target, reason)
            return policy
        with self._robots_lock:
            hit = self._robots.get(target)
            if hit is not None and not refresh and (now - hit.fetched_at) <= self._robots_ttl(hit.status):
                hit.from_cache = True
                return hit
            policy = (
                self._load_robots_from_disk(target, now=now)
                if not refresh
                else None
            )
            if policy is None and allow_fetch:
                policy = self._fetch_robots(target, scheme, netloc)
            elif policy is None:
                policy = RobotsPolicy(
                    host=target, status=0, fetched_at=now, fetched=False,
                    reason="离线模式且缓存里没有这个 host 的 robots.txt -> 状态未知",
                )
            self._robots[target] = policy
            return policy

    def robots_allows(
        self, url: str, *, refresh: bool = False
    ) -> tuple[bool, RobotsPolicy, str]:
        """``(是否允许抓这个 URL, 判定, 命中的规则说明)``."""
        host = _host_of(url)
        scheme = _scheme_of(url) or "https"
        policy = self.robots_policy(
            host, refresh=refresh, scheme=scheme, netloc=_netloc_of(url)
        )
        if policy.exempt:
            return True, policy, policy.reason
        allowed, rule = policy.allows(_robots_path(url))
        return allowed, policy, rule

    def min_interval(self, url: str, extra_delay: float = 0.0) -> float:
        """该 URL 实际生效的最小请求间隔 = max(rate_limit, Crawl-delay, extra)."""
        interval = max(
            float(self.config.http.rate_limit or 0.0),
            float(extra_delay or 0.0),
        )
        if bool(getattr(self.config.http, "respect_robots", True)) and url:
            host = _host_of(url)
            if host and not self.robots_exempt(host):
                delay = float(
                    self.robots_policy(
                        host, scheme=_scheme_of(url) or "https", netloc=_netloc_of(url)
                    ).crawl_delay or 0.0
                )
                cap = float(getattr(self.config.http, "crawl_delay_max", 0.0) or 0.0)
                if cap > 0:
                    delay = min(delay, cap)
                interval = max(interval, delay)
        return interval

    def robots_report(
        self, url: str, *, refresh: bool = False, allow_fetch: bool = True
    ) -> dict[str, Any]:
        """给 ``dddb robots <url>`` 用的可打印判定结果."""
        host = _host_of(url)
        scheme = _scheme_of(url) or "https"
        netloc = _netloc_of(url) or host
        policy = self.robots_policy(
            host, refresh=refresh, allow_fetch=allow_fetch, scheme=scheme, netloc=netloc
        )
        was_cached = bool(policy.from_cache)
        path = _robots_path(url)
        allowed, rule = (True, policy.reason) if policy.exempt else policy.allows(path)
        return {
            "url": url,
            "host": host,
            "robots_url": f"{scheme}://{netloc}/robots.txt" if host else "",
            "path": path,
            "respect_robots": bool(getattr(self.config.http, "respect_robots", True)),
            "exempt": bool(policy.exempt),
            "exempt_reason": self.robots_exempt_reason(host),
            "allowed": bool(allowed),
            "rule": rule,
            "http_status": int(policy.status),
            "fetched": bool(policy.fetched),
            "crawl_delay": float(policy.crawl_delay),
            "min_interval": self.min_interval(url),
            "cached": was_cached,
            "reason": policy.reason,
            "rules": [r.to_dict() for r in policy.rules],
            "fetches": int(self._robots_fetches),
        }

    def clear_robots_cache(self) -> int:
        """清空内存 + 落盘的 robots 缓存(``dddb robots --refresh`` 用)."""
        with self._robots_lock:
            count = len(self._robots)
            self._robots.clear()
            self._robots_warned.clear()
        return count + self._robots_store.clear()

    def _ssl_context(self) -> ssl.SSLContext | None:
        if self.config.http.verify_tls:
            return None
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def _build_opener(self, use_proxy: bool) -> urllib.request.OpenerDirector:
        handlers: list[urllib.request.BaseHandler] = [
            urllib.request.HTTPCookieProcessor(self._cookie_jar),
        ]
        if use_proxy and self.config.proxy.url:
            handlers.append(urllib.request.ProxyHandler({"http": self.config.proxy.url, "https": self.config.proxy.url}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        ctx = self._ssl_context()
        if ctx is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        return urllib.request.build_opener(*handlers)

    def _opener(self, use_proxy: bool) -> urllib.request.OpenerDirector:
        with self._lock:
            opener = self._openers.get(use_proxy)
            if opener is None:
                opener = self._build_opener(use_proxy)
                self._openers[use_proxy] = opener
            return opener

    # -- 限速 ---------------------------------------------------------------

    def _throttle(self, host: str, extra_delay: float = 0.0, crawl_delay: float = 0.0) -> None:
        interval = max(
            float(self.config.http.rate_limit or 0.0),
            float(extra_delay or 0.0),
            float(crawl_delay or 0.0),
        )
        if interval <= 0 or not host:
            return
        with self._lock:
            last = self._last_hit.get(host, 0.0)
            now = time.monotonic()
            wait = interval - (now - last)
            if wait > 0:
                time.sleep(wait)
            self._last_hit[host] = time.monotonic()

    # -- 请求 ---------------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | bytes | None = None,
        json_body: Any = None,
        headers: Mapping[str, str] | None = None,
        use_proxy: bool | None = None,
        use_cache: bool = True,
        cache_ttl: int | None = None,
        allow_stale: bool = False,
        retries: int | None = None,
        timeout: float | None = None,
        throttle_extra: float = 0.0,
        raise_for_status: bool = True,
        _respect_robots: bool = True,
    ) -> Response:
        method = method.upper()
        if params:
            url = _with_params(url, params)
        url = _encode_url(url)
        host = _host_of(url)

        if use_proxy is None:
            use_proxy = self.config.proxy.should_proxy(url)

        body_bytes: bytes | None = None
        req_headers: dict[str, str] = dict(self.default_headers)
        req_headers["User-Agent"] = self.user_agent_for(host)

        if json_body is not None:
            body_bytes = jsonlib.dumps(json_body, ensure_ascii=False).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json; charset=utf-8")
        elif isinstance(data, (bytes, bytearray)):
            body_bytes = bytes(data)
        elif data is not None:
            body_bytes = urllib.parse.urlencode(data, doseq=True).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")

        if headers:
            req_headers.update({k: v for k, v in headers.items() if v is not None})

        ttl = self.config.http.cache_ttl if cache_ttl is None else cache_ttl
        cache = self.cache
        if use_cache and cache is not None and ttl and method in ("GET", "HEAD"):
            hit = cache.get(method, url, None, allow_stale=allow_stale)
            if hit is not None:
                return Response(
                    url=url,
                    status=hit["status"],
                    headers=hit["headers"],
                    body=hit["body"],
                    from_cache=True,
                    elapsed=0.0,
                )

        attempts = (self.config.http.retries if retries is None else retries) + 1
        last_exc: Exception | None = None

        # robots.txt 的检查放在**响应缓存之后**: 命中缓存等于没有向对方发请求,
        # 离线重放不该被 robots 拦住; 真正要发的每一次网络请求才需要许可。
        crawl_delay = 0.0
        if _respect_robots and host and urllib.parse.urlsplit(url).scheme in ("http", "https"):
            allowed, policy, rule = self.robots_allows(url)
            if not allowed:
                raise RobotsDisallowed(
                    host, _robots_path(url), url=url, rule=rule or "Disallow: /"
                )
            cap = float(getattr(self.config.http, "crawl_delay_max", 0.0) or 0.0)
            crawl_delay = float(policy.crawl_delay or 0.0)
            if cap > 0:
                crawl_delay = min(crawl_delay, cap)

        for attempt in range(attempts):
            self._throttle(host, throttle_extra, crawl_delay)
            started = time.monotonic()
            req = urllib.request.Request(url, data=body_bytes, method=method, headers=req_headers)
            try:
                opener = self._opener(bool(use_proxy))
                with opener.open(req, timeout=timeout or self.config.http.timeout) as resp:
                    raw = resp.read()
                    status = int(resp.status)
                    resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                    elapsed = time.monotonic() - started
                    raw = _decompress(raw, resp_headers)
                    result = Response(
                        url=resp.geturl(),
                        status=status,
                        headers=resp_headers,
                        body=raw,
                        elapsed=elapsed,
                        encoding=resp_headers.get("content-encoding", ""),
                    )
                    if cache is not None and use_cache and ttl and method in ("GET", "HEAD") and status < 400:
                        try:
                            cache.put(method, url, status, resp_headers, raw, ttl=ttl, request_body=None)
                        except Exception:  # 缓存失败不影响主流程
                            pass
                    return result
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                raw = b""
                try:
                    raw = _decompress(exc.read(), {k.lower(): v for k, v in (exc.headers or {}).items()})
                except Exception:
                    raw = b""
                resp_headers = {k.lower(): v for k, v in (exc.headers or {}).items()}
                last_exc = HttpError(
                    f"HTTP {status} {exc.reason}", status=status, url=url, body=raw[:4000]
                )
                if status == 429:
                    retry_after = _parse_retry_after(resp_headers.get("retry-after"))
                    last_exc = RateLimited(
                        f"HTTP 429 {exc.reason}", status=status, url=url, retry_after=retry_after
                    )
                if status not in _RETRY_STATUS or attempt == attempts - 1:
                    if raise_for_status:
                        raise last_exc
                    return Response(url=url, status=status, headers=resp_headers, body=raw)
                self._sleep_backoff(attempt, resp_headers.get("retry-after"))
                continue
            except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError, ConnectionError, OSError) as exc:
                reason = getattr(exc, "reason", exc)
                if use_proxy and _looks_like_proxy_error(reason):
                    last_exc = ProxyError(
                        f"代理 {self.config.proxy.url} 不可用: {reason}. "
                        "请检查代理是否启动, 或用 DNH_PROXY_URL / DNH_PROXY_MODE=off 覆盖。"
                    )
                else:
                    last_exc = HttpError(f"请求失败: {reason}", url=url)
                if attempt == attempts - 1:
                    if raise_for_status:
                        raise last_exc
                    return Response(url=url, status=0, headers={}, body=b"")
                self._sleep_backoff(attempt, None)
                continue

        if last_exc is not None:
            raise last_exc
        raise HttpError("请求失败(未知原因)", url=url)

    def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        wait = _parse_retry_after(retry_after)
        if wait is None:
            base = float(self.config.http.backoff or 1.5)
            wait = (base ** attempt) + random.uniform(0, 0.6)
        wait = min(wait, 60.0)
        if self.log:
            self.log.debug("重试前等待 %.1fs (第 %d 次)", wait, attempt + 1)
        time.sleep(wait)

    # -- 便捷方法 -----------------------------------------------------------

    def get(self, url: str, **kw: Any) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> Response:
        return self.request("POST", url, **kw)

    def head(self, url: str, **kw: Any) -> Response:
        kw.setdefault("use_cache", False)
        return self.request("HEAD", url, **kw)

    def get_text(self, url: str, **kw: Any) -> str:
        return self.get(url, **kw).text

    def get_json(self, url: str, **kw: Any) -> Any:
        return self.get(url, **kw).json()

    def post_json(self, url: str, **kw: Any) -> Any:
        return self.post(url, **kw).json()

    # -- 文件下载 -----------------------------------------------------------

    def download(
        self,
        url: str,
        dest: Path | str,
        *,
        max_bytes: int = 12 * _MIB,
        only_images: bool = True,
        use_proxy: bool | None = None,
    ) -> Path:
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        resp = self.request("GET", url, use_cache=False, use_proxy=use_proxy, retries=2)
        if not resp.ok:
            raise HttpError(f"下载失败 {url}", status=resp.status, url=url)
        ctype = resp.content_type.lower()
        if only_images and ctype and not ctype.startswith("image/"):
            raise HttpError(f"目标不是图片 (content-type={ctype})", status=resp.status, url=url)
        if len(resp.body) > max_bytes:
            raise HttpError(f"图片过大 ({len(resp.body)} bytes)", url=url)
        dest_path.write_bytes(resp.body)
        return dest_path

    # -- 会话状态 -----------------------------------------------------------

    def set_cookie(self, name: str, value: str, domain: str) -> None:
        from http.cookiejar import Cookie

        cookie = Cookie(
            version=0, name=name, value=value, port=None, port_specified=False,
            domain=domain, domain_specified=True, domain_initial_dot=domain.startswith("."),
            path="/", path_specified=True, secure=True, expires=None, discard=False,
            comment=None, comment_url=None, rest={}, rfc2109=False,
        )
        self._cookie_jar.set_cookie(cookie)

    def load_cookie_header(self, header: str, domain: str = ".music.163.com") -> None:
        """把 ``a=1; b=2`` 形式的 Cookie 头灌进 cookie jar."""
        for chunk in (header or "").split(";"):
            if "=" not in chunk:
                continue
            name, value = chunk.split("=", 1)
            name, value = name.strip(), value.strip()
            if name:
                self.set_cookie(name, value, domain)

    def cookie_header(self, domain: str = ".music.163.com") -> str:
        parts = []
        for c in self._cookie_jar:
            if domain.lstrip(".") in c.domain.lstrip("."):
                parts.append(f"{c.name}={c.value}")
        return "; ".join(parts)

    def cookies(self) -> list[dict[str, str]]:
        return [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path} for c in self._cookie_jar]

    def close(self) -> None:
        if self._cache is not None:
            try:
                self._cache.close()
            except Exception:
                pass
            self._cache = None


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------


def _with_params(url: str, params: Mapping[str, Any]) -> str:
    parts = urllib.parse.urlsplit(url)
    existing = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    extra = [(k, v) for k, v in params.items() if v is not None]
    query = urllib.parse.urlencode([*existing, *extra], doseq=True)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def _encode_url(url: str) -> str:
    """把 URL 里的非 ASCII 字符百分号编码.

    ``urllib`` 只接受 ASCII 形式的 URL —— 站点上带日文文件名的图
    (``.../CDケース.png``)会让 Request 直接抛 ``UnicodeEncodeError``,
    在调用方看来就像"请求失败", 实际是我们的编码问题。
    """
    if not url:
        return url
    try:
        url.encode("ascii")
        return url
    except UnicodeEncodeError:
        pass
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=~")
    query = urllib.parse.quote(parts.query, safe="=&?/:;+,%@[]")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def _decompress(raw: bytes, headers: Mapping[str, str]) -> bytes:
    enc = (headers.get("content-encoding") or "").lower()
    if not enc or not raw:
        return raw
    try:
        if "gzip" in enc:
            return gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)
    except (OSError, zlib.error, EOFError):
        return raw
    return raw


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        dt = parsedate_to_datetime(value)
        return max(0.0, dt.timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def _looks_like_proxy_error(reason: Any) -> bool:
    text = str(reason).lower()
    return any(
        token in text
        for token in ("proxy", "tunnel", "connection refused", "actively refused", "10061", "407")
    )


def download_many(
    client: HttpClient,
    jobs: Iterable[tuple[str, Path]],
    *,
    logger: Any = None,
) -> dict[str, str]:
    """批量下载, 返回 ``{url: 本地路径}``; 失败项被跳过."""
    out: dict[str, str] = {}
    for url, dest in jobs:
        if not url:
            continue
        try:
            path = client.download(url, dest)
            out[url] = str(path)
        except Exception as exc:  # 封面失败不该中断流程
            if logger:
                logger.debug("封面下载失败 %s: %s", url, exc)
    return out
