"""注册验证:**本地验证码(算术题)** + **邮箱验证码** —— 纯 stdlib, 离线可测.

两块能力互不依赖, 页面各自接:

1. :class:`CaptchaStore` / :func:`new_captcha` / :func:`check_captcha`
   服务端生成、**一次性**、有时间窗、错 3 次作废。令牌化(``captcha_id``)而不是
   绑定 Cookie —— 这样页面只要把 :func:`new_captcha` 返回的 ``id`` 放进隐藏域即可,
   不依赖会话, 也不怕多标签页互相踢掉。验证码本身不落库(它只是一次性的短命数据),
   进程重启会让手上未提交的验证码失效, 用户刷新页面就换一道新的。
   ``kind="chars"`` 的字符题是把字符当文本印在页面上(没有图形库), 机器能抄;
   默认用 ``arithmetic`` 算术题, 字符题仅为"页面上看起来像验证码"而保留。

2. 邮箱验证码(6 位数字):
   * **必须可开关**: 只有配了 SMTP(:func:`email_enabled`)才启用; 没配就返回
     ``(False, 原因)`` 而不是抛异常 —— 注册流程不会因为没邮件服务被卡死;
   * 验证码只存哈希(复用 :func:`dnh.public.users.hash_password`), 15 分钟过期,
     错 5 次作废, 同一账号 60 秒内不能重发(防刷);
   * ``mailer`` 可注入(见 :class:`RecordingMailer`), 所以**整条链路离线可测**。

环境变量(全部可选; 缺 HOST/FROM 即视为未配置)::

    DDDB_SMTP_HOST       SMTP 主机, 例如 smtp.example.com
    DDDB_SMTP_PORT       端口, 默认 587
    DDDB_SMTP_USER       SMTP 用户名(为空则不登录)
    DDDB_SMTP_PASSWORD   SMTP 口令
    DDDB_SMTP_FROM       发件人地址, 缺省回退到 DDDB_SMTP_USER
    DDDB_SMTP_TLS        1(默认)= 明文连接后 STARTTLS; 0 = 不用 TLS
    DDDB_SMTP_SSL        1 = 直接 SSL(SMTPS, 通常配合 465 端口)
    DDDB_SMTP_TIMEOUT    秒, 默认 10
    DDDB_EMAIL_VERIFY    显式开关: 0/false/off = 即使配了 SMTP 也关闭邮箱验证

配置好之后注册流程就是"验证码 + 邮箱验证"; 没配置时只有验证码, 账号建出来即为
可用状态(见 :func:`dnh.public.users.email_verification_blocks_login` 的说明)。
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from typing import Any, Mapping, Sequence

from .users import (
    UserStore,
    email_problem,
    hash_password,
    normalize_email,
    verify_password,
)

logger = logging.getLogger("dnh.public.verify")

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

#: 验证码有效期(秒)
CAPTCHA_TTL = 600.0
#: 同一道验证码最多允许错几次(超过即作废, 必须换新的)
CAPTCHA_MAX_ATTEMPTS = 3
#: 内存里最多同时挂多少道验证码(超出先扔最早的)
CAPTCHA_MAX_ENTRIES = 2000
#: 支持的题型: ``arithmetic`` 算术题(默认) / ``chars`` 字符抄写题
CAPTCHA_KINDS: tuple[str, ...] = ("arithmetic", "chars")
#: 字符题用的字符集(去掉了 0/O/1/I/l 这类容易看错的)
CAPTCHA_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CAPTCHA_CHAR_LENGTH = 5

#: 邮箱验证码位数 / 有效期 / 最多尝试次数 / 重发间隔
EMAIL_CODE_DIGITS = 6
EMAIL_CODE_TTL = 15 * 60.0
EMAIL_CODE_MAX_ATTEMPTS = 5
EMAIL_RESEND_INTERVAL = 60.0

#: 邮箱验证码的哈希迭代次数(6 位数字空间小, 用 PBKDF2 挡住离线爆破)
EMAIL_CODE_ITERATIONS = 50_000

#: 邮箱验证码的用途标记(verify_codes.purpose)
PURPOSE_EMAIL = "email"

#: 环境变量名(导出便于文档/测试引用)
SMTP_ENV_KEYS: tuple[str, ...] = (
    "DDDB_SMTP_HOST",
    "DDDB_SMTP_PORT",
    "DDDB_SMTP_USER",
    "DDDB_SMTP_PASSWORD",
    "DDDB_SMTP_FROM",
    "DDDB_SMTP_TLS",
    "DDDB_SMTP_SSL",
    "DDDB_SMTP_TIMEOUT",
    "DDDB_EMAIL_VERIFY",
)

_DEFAULT_SMTP_PORT = 587
_DEFAULT_SMTP_TIMEOUT = 10.0


class MailerError(RuntimeError):
    """发信失败(SMTP 连不上 / 认证失败 / 收件人被拒)."""


# --------------------------------------------------------------------------
# 验证码
# --------------------------------------------------------------------------


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


class CaptchaStore:
    """内存验证码表(一次性 / 过期 / 限次).

    * ``ttl``: 单道验证码的有效期;
    * ``max_attempts``: 允许答错几次, 超过直接作废(要重新 :meth:`issue`);
    * ``max_entries``: 上限, 超出先淘汰最早的(防止被刷爆内存);
    * ``clock``: 可注入的时间函数, 单测里不用真的等 10 分钟。
    """

    def __init__(
        self,
        *,
        ttl: float = CAPTCHA_TTL,
        max_attempts: int = CAPTCHA_MAX_ATTEMPTS,
        max_entries: int = CAPTCHA_MAX_ENTRIES,
        clock: Any = None,
    ) -> None:
        self.ttl = max(1.0, float(ttl))
        self.max_attempts = max(1, int(max_attempts))
        self.max_entries = max(16, int(max_entries))
        self._clock = clock or time.time
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self.issued = 0
        self.rejected = 0

    # -- 内部 ---------------------------------------------------------------

    def _now(self, now: float | None) -> float:
        return float(self._clock() if now is None else float(now))

    def _make_question(self, kind: str) -> tuple[str, str]:
        """返回 ``(题目文本, 正确答案)``."""
        if kind == "chars":
            answer = "".join(secrets.choice(CAPTCHA_CHARS) for _ in range(CAPTCHA_CHAR_LENGTH))
            return f"请把下面这 {CAPTCHA_CHAR_LENGTH} 个字符照抄一遍: {answer}", answer
        left = secrets.randbelow(19) + 2      # 2..20
        right = secrets.randbelow(13) + 2     # 2..14
        operator = secrets.choice(["+", "-", "×"])
        if operator == "-":
            big, small = max(left, right), min(left, right)
            return f"{big} - {small} = ?", str(big - small)
        if operator == "×":
            a = secrets.randbelow(8) + 2      # 2..9
            b = secrets.randbelow(8) + 2
            return f"{a} × {b} = ?", str(a * b)
        return f"{left} + {right} = ?", str(left + right)

    @staticmethod
    def _normalize_answer(answer: Any) -> str:
        text = str(answer if answer is not None else "").strip()
        text = text.replace(" ", "").replace("\t", "")
        if text.endswith("="):
            text = text[:-1]
        if text.startswith("+") and text[1:].isdigit():
            text = text[1:]
        return text.casefold()

    def _prune_locked(self, now: float) -> int:
        doomed = [key for key, item in self._items.items() if item["expires_at"] <= now]
        for key in doomed:
            self._items.pop(key, None)
        if len(self._items) > self.max_entries:
            # 最老的先扔
            for key, _ in sorted(self._items.items(), key=lambda kv: kv[1]["created_at"])[
                : len(self._items) - self.max_entries
            ]:
                self._items.pop(key, None)
        return len(doomed)

    # -- 对外 ---------------------------------------------------------------

    def issue(self, *, kind: str = "auto", now: float | None = None,
              graphic: bool = False) -> dict[str, Any]:
        """生成一道验证码. 返回可直接塞进模板的字典(见 :func:`new_captcha`)."""
        wanted = str(kind or "").strip().lower()
        if wanted == "auto" or wanted not in CAPTCHA_KINDS:
            wanted = "arithmetic"
        stamp = self._now(now)
        question, answer = self._make_question(wanted)
        captcha_id = secrets.token_urlsafe(24)
        with self._lock:
            self._prune_locked(stamp)
            self._items[captcha_id] = {
                "answer": answer,
                "question": question,
                "kind": wanted,
                "created_at": stamp,
                "expires_at": stamp + self.ttl,
                "attempts": 0,
            }
            self.issued += 1
        # **图形题不暴露答案**: 题面由 /captcha.svg 现场渲染, 页面只拿到 id。
        exposed = "" if (graphic and wanted == "chars") else question
        return {
            "id": captcha_id,
            "question": exposed,
            "kind": wanted,
            "ttl": int(self.ttl),
            "expires_at": stamp + self.ttl,
            "max_attempts": self.max_attempts,
        }

    def check(self, captcha_id: Any, answer: Any, *, now: float | None = None) -> tuple[bool, str]:
        """校验并**消费**验证码. 返回 ``(是否通过, 给用户看的原因)``."""
        key = str(captcha_id or "").strip()
        if not key:
            return False, "请先完成验证码"
        stamp = self._now(now)
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return False, "验证码已失效, 请刷新页面换一道"
            if item["expires_at"] <= stamp:
                self._items.pop(key, None)
                return False, "验证码已过期, 请刷新页面换一道"
            if item["attempts"] >= self.max_attempts:
                self._items.pop(key, None)
                return False, "错误次数过多, 请刷新页面换一道"
            given = self._normalize_answer(answer)
            expected = self._normalize_answer(item["answer"])
            if not given or given != expected:
                item["attempts"] += 1
                left = self.max_attempts - item["attempts"]
                if left <= 0:
                    self._items.pop(key, None)
                    return False, "错误次数过多, 请刷新页面换一道"
                return False, f"验证码不正确, 还可以再试 {left} 次"
            # 一次性的: 对了就立刻销毁
            self._items.pop(key, None)
            return True, ""

    def peek(self, captcha_id: Any, *, now: float | None = None) -> dict[str, Any] | None:
        """**只读**取一条验证码(不消费、不计错误次数), 供服务端渲染图形用.

        页面永远拿不到这个返回值; 过期/不存在返回 ``None``。
        """
        key = str(captcha_id or "").strip()
        if not key:
            return None
        stamp = self._now(now)
        with self._lock:
            item = self._items.get(key)
            if item is None or item["expires_at"] <= stamp:
                return None
            return dict(item)

    def outstanding(self, *, now: float | None = None) -> int:
        """当前还没被消费/没过期/没错满的验证码数量."""
        stamp = self._now(now)
        with self._lock:
            self._prune_locked(stamp)
            return len(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def stats(self, *, now: float | None = None) -> dict[str, Any]:
        stamp = self._now(now)
        with self._lock:
            self._prune_locked(stamp)
            return {
                "issued": self.issued,
                "rejected": self.rejected,
                "outstanding": len(self._items),
                "ttl": self.ttl,
                "max_attempts": self.max_attempts,
            }


#: 进程级默认验证码表(http.server 是单进程多线程, 这样最省事)
_DEFAULT_CAPTCHA_STORE = CaptchaStore()


def new_captcha(kind: str = "auto", *, store: CaptchaStore | None = None,
               now: float | None = None, graphic: bool = False) -> dict[str, Any]:
    """生成一道验证码, 返回::

        {
          "id": "kQ7…",                 # 放进表单隐藏域 captcha_id
          "question": "6 + 7 = ?",      # 显示给用户抄的题目
          "kind": "arithmetic",         # arithmetic / chars
          "ttl": 600,                   # 秒
          "expires_at": 1762…,          # 绝对时间戳
          "max_attempts": 3,
        }
    """
    return (store or _DEFAULT_CAPTCHA_STORE).issue(kind=kind, now=now, graphic=graphic)


def _svg_escape(text: Any) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def captcha_svg(captcha_id: Any, *, store: CaptchaStore | None = None,
                now: float | None = None) -> str:
    """把一道 ``chars`` 验证码渲染成 **SVG 图片**(纯 stdlib, 不引第三方).

    视觉混淆要点(用户反馈"混淆不足"后加强):
    * 字号/颜色随机, 每个字 **旋转 ±32° + 倾斜 ±14° + 缩放**, 且有轻微位移重叠;
    * 每个字再画一个**低透明度重影**(偏移+旋转), 干扰分割;
    * 背景有斜向色带与杂点;
    * **先画字、后压噪声**: 曲线与噪点覆盖在字形之上(不是垫在底下);
    * 答案不进 HTML / aria-label / <title>。
    找不到或已过期返回空串(路由层转 404)。
    """
    item = (store or _DEFAULT_CAPTCHA_STORE).peek(captcha_id, now=now)
    if not item:
        return ""
    text = str(item.get("answer") or "")
    if not text:
        return ""
    rng = secrets.SystemRandom()
    palette = ["#e7edf5", "#7aa2ff", "#a78bfa", "#9ad1ff", "#c4b5fd"]
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 56" width="160" height="56"'
        ' role="img" aria-label="图形验证码">',
        '<defs><linearGradient id="cbg" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#0d1220"/><stop offset="1" stop-color="#161d2e"/>'
        '</linearGradient></defs>',
        '<rect width="160" height="56" rx="8" fill="url(#cbg)"/>',
    ]
    # 背景斜带
    for _ in range(3):
        x = rng.randint(-20, 140)
        parts.append(
            f'<rect x="{x}" y="-10" width="{rng.randint(10, 26)}" height="76"'
            f' fill="{rng.choice(palette)}" opacity="0.06"'
            f' transform="rotate(18 {x} 28)"/>')
    # 背景杂点
    for _ in range(14):
        parts.append(
            f'<circle cx="{rng.randint(2, 158)}" cy="{rng.randint(2, 54)}"'
            f' r="{rng.randint(1, 2)}" fill="{rng.choice(palette)}" opacity="0.22"/>')

    step = 124 / max(1, len(text))
    base_y = 37
    glyphs = []
    for index, char in enumerate(text):
        x = 20 + index * step + rng.uniform(-3, 3)
        y = base_y + rng.uniform(-5, 5)
        size = rng.choice([23, 25, 27, 29])
        skew = rng.uniform(-14, 14)
        rotation = rng.uniform(-32, 32)
        scale = rng.uniform(0.92, 1.18)
        colour = rng.choice(palette)
        glyphs.append((x, y, size, skew, rotation, scale, colour, char))

    # ① 先画重影(半透明, 偏移) —— 让字形边界更难切分
    for x, y, size, skew, rotation, scale, colour, char in glyphs:
        gx = x + rng.uniform(-3.5, 3.5)
        gy = y + rng.uniform(-3.5, 3.5)
        parts.append(
            f'<text x="{gx:.1f}" y="{gy:.1f}" font-family="ui-monospace,Menlo,monospace"'
            f' font-size="{size}" font-weight="700" fill="{colour}" opacity="0.30"'
            f' transform="translate({gx:.1f} {gy:.1f}) rotate({rotation + rng.uniform(-8, 8):.1f})'
            f' skewX({skew:.1f}) scale({scale:.2f}) translate({-gx:.1f} {-gy:.1f})"'
            f'>{_svg_escape(char)}</text>')
    # ② 正字(完整混淆: translate→rotate→skew→scale 再平移回来)
    for x, y, size, skew, rotation, scale, colour, char in glyphs:
        parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family="ui-monospace,Menlo,monospace"'
            f' font-size="{size}" font-weight="700" fill="{colour}"'
            f' transform="translate({x:.1f} {y:.1f}) rotate({rotation:.1f})'
            f' skewX({skew:.1f}) scale({scale:.2f}) translate({-x:.1f} {-y:.1f})"'
            f'>{_svg_escape(char)}</text>')
    # ③ 后压噪声: 曲线在字上
    for _ in range(3):
        x1, y1 = rng.randint(-8, 168), rng.randint(0, 56)
        x2, y2 = rng.randint(-8, 168), rng.randint(0, 56)
        x3, y3 = rng.randint(-8, 168), rng.randint(0, 56)
        parts.append(
            f'<path d="M{x1} {y1} Q{x2} {y2} {x3} {y3}" stroke="{rng.choice(palette)}"'
            f' stroke-width="{rng.uniform(1.0, 1.9):.1f}" fill="none" opacity="0.55"/>')
    # ④ 后压噪声: 斜线 + 噪点
    for _ in range(4):
        x = rng.randint(0, 150)
        parts.append(
            f'<line x1="{x}" y1="0" x2="{x + rng.randint(-26, 26)}" y2="56"'
            f' stroke="{rng.choice(palette)}" stroke-width="1.0" opacity="0.40"/>')
    for _ in range(20):
        parts.append(
            f'<circle cx="{rng.randint(0, 160)}" cy="{rng.randint(0, 56)}"'
            f' r="{rng.uniform(0.8, 1.8):.1f}" fill="{rng.choice(palette)}" opacity="0.5"/>')
    parts.append("</svg>")
    return "".join(parts)


def check_captcha(captcha_id: Any, answer: Any, *, store: CaptchaStore | None = None,
                  now: float | None = None) -> tuple[bool, str]:
    """校验验证码(一次性). 返回 ``(是否通过, 原因)``; 通过时原因是空串."""
    return (store or _DEFAULT_CAPTCHA_STORE).check(captcha_id, answer, now=now)


def captcha_stats(*, store: CaptchaStore | None = None, now: float | None = None) -> dict[str, Any]:
    return (store or _DEFAULT_CAPTCHA_STORE).stats(now=now)


def reset_captchas(*, store: CaptchaStore | None = None) -> None:
    """清空验证码表(测试收尾用)."""
    (store or _DEFAULT_CAPTCHA_STORE).clear()


# --------------------------------------------------------------------------
# SMTP 配置 / 发信
# --------------------------------------------------------------------------


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def smtp_settings(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """从环境变量读出 SMTP 配置; **没配全时返回空字典**.

    判定: ``DDDB_SMTP_HOST`` 与发件人(``DDDB_SMTP_FROM``, 缺省回退
    ``DDDB_SMTP_USER``)都存在才算配好 —— 缺发件人根本发不出信, 不算启用。
    """
    env = _env(environ)
    host = str(env.get("DDDB_SMTP_HOST", "")).strip()
    sender = str(env.get("DDDB_SMTP_FROM", "")).strip() or str(env.get("DDDB_SMTP_USER", "")).strip()
    if not host or not sender:
        return {}
    try:
        port = int(str(env.get("DDDB_SMTP_PORT", "") or _DEFAULT_SMTP_PORT).strip())
    except (TypeError, ValueError):
        port = _DEFAULT_SMTP_PORT
    try:
        timeout = float(str(env.get("DDDB_SMTP_TIMEOUT", "") or _DEFAULT_SMTP_TIMEOUT).strip())
    except (TypeError, ValueError):
        timeout = _DEFAULT_SMTP_TIMEOUT
    user = str(env.get("DDDB_SMTP_USER", "")).strip()
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": str(env.get("DDDB_SMTP_PASSWORD", "")),
        "from": sender,
        "tls": _truthy(env.get("DDDB_SMTP_TLS", "1")),
        "ssl": _truthy(env.get("DDDB_SMTP_SSL", "0")),
        "timeout": max(1.0, timeout),
    }


#: Resend 的 HTTP API(用户选定的方案: Resend + Cloudflare 做邮箱验证)
RESEND_API_URL = "https://api.resend.com/emails"


def resend_settings(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Resend 配置: 需要 ``RESEND_API_KEY`` + 发件人(``DDDB_MAIL_FROM``, 回退 ``DDDB_SMTP_FROM``)."""
    env = _env(environ)
    key = str(env.get("RESEND_API_KEY", "")).strip()
    sender = str(env.get("DDDB_MAIL_FROM", "") or env.get("DDDB_SMTP_FROM", "")).strip()
    if not key or not sender:
        return {}
    try:
        timeout = float(str(env.get("DDDB_MAIL_TIMEOUT", "") or _DEFAULT_SMTP_TIMEOUT).strip())
    except (TypeError, ValueError):
        timeout = _DEFAULT_SMTP_TIMEOUT
    return {"api_key": key, "from": sender, "timeout": max(1.0, timeout)}


class ResendMailer:
    """走 Resend HTTP API 的发信器(纯 stdlib: urllib).

    ``send()`` 失败抛 :class:`MailerError`; 成功返回 None.
    """

    def __init__(self, settings: Mapping[str, Any] | None = None, *,
                 environ: Mapping[str, str] | None = None,
                 urlopen: Any = None) -> None:
        self.settings = dict(settings or resend_settings(environ))
        if not self.settings:
            raise MailerError("未配置 Resend(RESEND_API_KEY / DDDB_MAIL_FROM)")
        self._urlopen = urlopen or urllib.request.urlopen

    def send(self, to: str, subject: str, body: str) -> None:
        payload = {
            "from": self.settings["from"],
            "to": [str(to)],
            "subject": str(subject),
            "text": str(body),
        }
        request = urllib.request.Request(
            RESEND_API_URL,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.settings['api_key']}",
                "Content-Type": "application/json",
                # Resend 要求带 UA, 否则可能被拒
                "User-Agent": "dnh-ddbb/1.0 (+https://www.doujindigdb.top)",
            },
        )
        try:
            with self._urlopen(request, timeout=float(self.settings["timeout"])) as resp:
                status = int(getattr(resp, "status", 200) or 200)
                resp.read()
                if status >= 300:
                    raise MailerError(f"Resend 返回 HTTP {status}")
        except MailerError:
            raise
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:                       # noqa: BLE001
                pass
            raise MailerError(f"Resend 发信失败: HTTP {exc.code} {detail}".strip()) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise MailerError(f"Resend 发信失败: {exc}") from exc


def email_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """邮箱验证是否启用 = 配了 SMTP 且没有被 ``DDDB_EMAIL_VERIFY`` 显式关掉."""
    flag = str(_env(environ).get("DDDB_EMAIL_VERIFY", "")).strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    # Resend 优先, SMTP 作为备选 —— 两者任一配好就算启用
    return bool(resend_settings(environ) or smtp_settings(environ))


def email_disabled_reason(environ: Mapping[str, str] | None = None) -> str:
    """没启用邮箱验证时给页面/日志看的原因(启用时返回空串)."""
    if email_enabled(environ):
        return ""
    flag = str(_env(environ).get("DDDB_EMAIL_VERIFY", "")).strip().lower()
    if flag in ("0", "false", "no", "off"):
        return "邮箱验证已被 DDDB_EMAIL_VERIFY 关闭"
    env = _env(environ)
    missing = [key for key in ("RESEND_API_KEY", "DDDB_MAIL_FROM")
               if not str(env.get(key, "")).strip()]
    return ("未配置发信(Resend 需要 RESEND_API_KEY + DDDB_MAIL_FROM; "
            "或 SMTP 需要 DDDB_SMTP_HOST + DDDB_SMTP_FROM), 邮箱验证未启用")


class SmtpMailer:
    """真实的 SMTP 发信器. ``send()`` 失败抛 :class:`MailerError`."""

    def __init__(self, settings: Mapping[str, Any] | None = None, *,
                 environ: Mapping[str, str] | None = None) -> None:
        self.settings = dict(settings or smtp_settings(environ))
        if not self.settings:
            raise MailerError("未配置 SMTP")

    def send(self, to: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.settings["from"]
        message["To"] = str(to)
        message["Subject"] = subject
        message.set_content(str(body))
        host = self.settings["host"]
        port = int(self.settings["port"])
        timeout = float(self.settings["timeout"])
        try:
            if self.settings.get("ssl"):
                context = ssl.create_default_context()
                client: Any = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
            else:
                client = smtplib.SMTP(host, port, timeout=timeout)
            try:
                client.ehlo()
                if self.settings.get("tls") and not self.settings.get("ssl"):
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                if self.settings.get("user"):
                    client.login(self.settings["user"], self.settings.get("password") or "")
                client.send_message(message)
            finally:
                try:
                    client.quit()
                except (smtplib.SMTPException, OSError):  # pragma: no cover
                    pass
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            raise MailerError(f"SMTP 发信失败: {exc}") from exc


class RecordingMailer:
    """离线/测试用发信器: 只把邮件记在 ``outbox`` 里, 不联网.

    用法::

        mailer = RecordingMailer()
        ok, message = send_email_code(store, "alice", mailer=mailer)
        code = mailer.last_code()          # 测试里取出验证码
    """

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.outbox: list[dict[str, Any]] = []
        self._fail = fail
        self._lock = threading.RLock()

    def send(self, to: str, subject: str, body: str) -> None:
        if self._fail is not None:
            raise self._fail if isinstance(self._fail, MailerError) else MailerError(str(self._fail))
        with self._lock:
            self.outbox.append({"to": str(to), "subject": str(subject), "body": str(body)})

    def last(self) -> dict[str, Any] | None:
        return self.outbox[-1] if self.outbox else None

    def last_code(self) -> str:
        """从最近一封邮件正文里抠出 6 位验证码; 没有则返回空串."""
        message = self.last()
        if not message:
            return ""
        match = re.search(r"\b(\d{%d})\b" % EMAIL_CODE_DIGITS, message["body"])
        return match.group(1) if match else ""

    def clear(self) -> None:
        with self._lock:
            self.outbox.clear()


def mailer_from_env(environ: Mapping[str, str] | None = None) -> Any:
    """按环境变量构造发信器(**Resend 优先**, 其次 SMTP); 都没配返回 ``None``."""
    resend = resend_settings(environ)
    if resend:
        return ResendMailer(resend)
    smtp = smtp_settings(environ)
    return SmtpMailer(smtp) if smtp else None


#: 邮件链接用途: 邮箱验证 / 新环境登录确认
PURPOSE_EMAIL_LINK = "email_link"
PURPOSE_LOGIN_LINK = "login_link"
PURPOSE_PASSWORD_RESET = "password_reset"


def send_email_link(store: UserStore, username: Any, *, base_url: str,
                    purpose: str = PURPOSE_EMAIL_LINK, ttl: float = 24 * 3600.0,
                    mailer: Any = None, now: float | None = None,
                    environ: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """发送**带一次性链接**的验证邮件(注册绑定 / 新环境登录确认)."""
    user = store.get(username)
    if user is None:
        return False, "找不到这个用户"
    address = normalize_email(user.get("email"))
    if not address:
        return False, "该账号没有填写邮箱"
    token = store.issue_email_link(username, purpose=purpose, ttl=ttl, now=now)
    if not token:
        return False, "生成验证链接失败"
    # 同一封邮件里也给一个 **6 位验证码** 作为备选(手机不方便点链接/复制链接时用)
    stamp = time.time() if now is None else float(now)
    code = f"{secrets.randbelow(10 ** EMAIL_CODE_DIGITS):0{EMAIL_CODE_DIGITS}d}"
    code_purpose = PURPOSE_EMAIL if purpose == PURPOSE_EMAIL_LINK else "login"
    store.issue_verify_code(username, hash_password(code, iterations=EMAIL_CODE_ITERATIONS),
                            purpose=code_purpose, expires_at=stamp + EMAIL_CODE_TTL, now=stamp)
    sender = mailer if mailer is not None else mailer_from_env(environ)
    if sender is None:
        return False, email_disabled_reason(environ) or "邮箱验证未启用"
    link = (str(base_url or "").rstrip("/") or "") + "/verify?token=" + token
    if purpose == PURPOSE_PASSWORD_RESET:
        subject = "【DoujinDigDatabase】重置密码"
        body = (
            f"你好 {user.get('username')}:\n\n"
            "你(或有人)申请重置这个账号的密码。请点击下面的链接设置新密码：\n\n"
            f"{link}\n\n"
            f"或者输入验证码: {code}\n\n"
            "链接 24 小时内有效、只能用一次。不是你本人操作请忽略本邮件。\n"
            "-- DoujinDigDatabase (DDDB)\n"
        )
    elif purpose == PURPOSE_LOGIN_LINK:
        subject = "【DoujinDigDatabase】新环境登录确认"
        body = (
            f"你好 {user.get('username')}:\n\n"
            "我们检测到你从一个新的登录环境访问 DoujinDigDatabase。\n"
            "如果这是你本人，请点击下面的链接确认登录：\n\n"
            f"{link}\n\n"
            "如果不是你本人，请忽略本邮件并考虑修改密码。\n"
            "-- DoujinDigDatabase (DDDB)\n"
        )
    else:
        subject = "【DoujinDigDatabase】邮箱验证"
        body = (
            f"你好 {user.get('username')}:\n\n"
            "请点击下面的链接完成邮箱验证(24 小时内有效，只能用一次)：\n\n"
            f"{link}\n\n"
            "如果不是你本人注册，忽略这封邮件即可。\n"
            "-- DoujinDigDatabase (DDDB)\n"
        )
    # 正文里统一附上 6 位验证码(链接之外的备选路径)
    code_line = "或直接输入验证码: " + code + "\n\n"
    body = body.replace("-- DoujinDigDatabase (DDDB)",
                        code_line + "-- DoujinDigDatabase (DDDB)", 1)
    try:
        sender.send(address, subject, body)
    except MailerError as exc:
        return False, f"邮件发送失败: {exc}"
    return True, "验证邮件已发送, 请到邮箱点击链接"


def check_email_link(store: UserStore, token: Any, *,
                     purpose: str = PURPOSE_EMAIL_LINK,
                     now: float | None = None) -> tuple[bool, str, str]:
    """核销邮件链接 -> ``(成功?, 原因, 用户名)``."""
    return store.redeem_email_link(token, purpose=purpose, now=now)


def _code_message(username: str, code: str, ttl_seconds: float) -> tuple[str, str]:
    minutes = max(1, int(round(ttl_seconds / 60.0)))
    subject = "【DoujinDigDatabase】注册邮箱验证码"
    body = (
        f"你好 {username}:\n\n"
        f"你的注册邮箱验证码是: {code}\n"
        f"验证码 {minutes} 分钟内有效, 请勿转发给他人。\n\n"
        "如果不是你本人注册, 忽略这封邮件即可。\n"
        "-- DoujinDigDatabase (DDDB) · 同人音乐专辑挖掘数据库\n"
    )
    return subject, body


def send_email_code(
    store: UserStore,
    username: Any,
    *,
    mailer: Any = None,
    now: float | None = None,
    environ: Mapping[str, str] | None = None,
    purpose: str = PURPOSE_EMAIL,
) -> tuple[bool, str]:
    """给账号的邮箱发一个 6 位验证码. 返回 ``(是否发出, 给用户看的原因)``.

    * ``mailer`` 为空时按环境变量构造(**Resend 优先, SMTP 备选**, 见
      :func:`mailer_from_env`); 都没配置 -> ``(False, "未配置发信…")``(不抛异常,
      注册流程据此跳过邮箱验证);
    * ``mailer`` 传任意带 ``send(to, subject, body)`` 的对象即可(测试注入);
    * 同一账号 60 秒内只允许发一次(``EMAIL_RESEND_INTERVAL``);
    * 验证码只存哈希, 明文只出现在邮件正文里。
    """
    if mailer is None and not email_enabled(environ):
        return False, email_disabled_reason(environ) or "邮箱验证未启用"
    user = store.get(username)
    if user is None:
        return False, "找不到这个用户"
    address = normalize_email(user.get("email"))
    if not address:
        return False, "该账号没有填写邮箱, 无法发送验证码"
    if email_problem(address):
        return False, "该账号的邮箱格式不对, 请先修正"
    if user.get("email_verified"):
        return False, "这个邮箱已经验证过了, 无需重复发送"
    stamp = time.time() if now is None else float(now)
    latest = store.latest_verify_code(user["username"], purpose=purpose)
    if latest is not None:
        waited = stamp - float(latest["created_at"] or 0.0)
        if waited < EMAIL_RESEND_INTERVAL:
            left = int(EMAIL_RESEND_INTERVAL - waited) + 1
            return False, f"发送太频繁, 请 {left} 秒后再试"
    code = f"{secrets.randbelow(10 ** EMAIL_CODE_DIGITS):0{EMAIL_CODE_DIGITS}d}"
    code_hash = hash_password(code, iterations=EMAIL_CODE_ITERATIONS)
    record_id = store.issue_verify_code(
        user["username"], code_hash, purpose=purpose,
        expires_at=stamp + EMAIL_CODE_TTL, now=stamp,
    )
    if mailer is not None:
        sender = mailer
    else:
        # **必须走 mailer_from_env**: Resend 优先、SMTP 次之。
        # 原来这里写死 SmtpMailer(...), 导致配了 Resend 也会抛"未配置 SMTP"。
        sender = mailer_from_env(environ)
        if sender is None:                          # pragma: no cover - 上面已挡
            store.consume_verify_code(record_id, now=stamp)
            return False, email_disabled_reason(environ) or "邮箱验证未启用"
    subject, body = _code_message(user["username"], code, EMAIL_CODE_TTL)
    try:
        sender.send(address, subject, body)
    except MailerError as exc:
        # 没发出去就把码作废, 免得数据库里留一个没人知道的"有效码"
        store.consume_verify_code(record_id, now=stamp)
        logger.warning("发送邮箱验证码失败(%s): %s", user["username"], exc)
        return False, f"邮件发送失败: {exc}"
    except Exception as exc:  # pragma: no cover - 注入的 mailer 出错也不该 500
        store.consume_verify_code(record_id, now=stamp)
        logger.warning("发送邮箱验证码异常(%s): %s", user["username"], exc)
        return False, f"邮件发送失败: {exc}"
    logger.info("已向 %s 发送邮箱验证码", user["username"])
    return True, "验证码已发送到你的邮箱, 请查收"


def check_email_code(
    store: UserStore,
    username: Any,
    code: Any,
    *,
    now: float | None = None,
    purpose: str = PURPOSE_EMAIL,
) -> tuple[bool, str]:
    """校验邮箱验证码; 通过则把账号标记为"邮箱已验证".

    返回 ``(是否通过, 原因)``。失败原因区分: 没有待验证的码 / 已过期 /
    尝试次数过多 / 验证码不正确(并告诉还剩几次)。
    """
    user = store.get(username)
    if user is None:
        return False, "找不到这个用户"
    # 「已验证」只对**邮箱验证**这一用途有意义; 改密码/注销的验证码必须能继续校验
    if purpose == PURPOSE_EMAIL and user.get("email_verified"):
        return False, "这个邮箱已经验证过了"
    stamp = time.time() if now is None else float(now)
    record = store.latest_verify_code(user["username"], purpose=purpose)
    if record is None:
        return False, "没有待验证的验证码, 请先获取验证码"
    if float(record["expires_at"] or 0.0) <= stamp:
        store.consume_verify_code(record["id"], now=stamp)
        return False, "验证码已过期, 请重新获取"
    if int(record["attempts"] or 0) >= EMAIL_CODE_MAX_ATTEMPTS:
        store.consume_verify_code(record["id"], now=stamp)
        return False, "尝试次数过多, 请重新获取验证码"
    given = str(code if code is not None else "").strip()
    if not given or not verify_password(given, record["code_hash"]):
        attempts = store.bump_verify_attempts(record["id"])
        left = EMAIL_CODE_MAX_ATTEMPTS - attempts
        if left <= 0:
            store.consume_verify_code(record["id"], now=stamp)
            return False, "尝试次数过多, 请重新获取验证码"
        return False, f"验证码不正确, 还可以再试 {left} 次"
    store.consume_verify_code(record["id"], now=stamp)
    # 只有"邮箱验证"这一用途才改变账号的邮箱验证状态
    if purpose == PURPOSE_EMAIL:
        store.mark_email_verified(user["username"], now=stamp)
    return True, ""


def email_verification_state(
    store: UserStore,
    username: Any,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """给页面用的状态字典: 要不要验证 / 启没启用 / 验过没有 / 有没有邮箱.

    ::

        {"enabled": True, "required": True, "has_email": True,
         "verified": False, "email": "a***@example.com", "reason": ""}
    """
    enabled = email_enabled(environ)
    user = store.get(username) or {}
    email = normalize_email(user.get("email"))
    verified = bool(user.get("email_verified"))
    return {
        "enabled": enabled,
        "required": enabled and bool(email),
        "has_email": bool(email),
        "verified": verified,
        "email": mask_email(email),
        "reason": email_disabled_reason(environ) if not enabled else "",
    }


def mask_email(address: Any) -> str:
    """``alice@example.com`` -> ``a***e@example.com``(展示用; 空值原样返回)."""
    text = normalize_email(address)
    if not text or "@" not in text:
        return text
    local, _, domain = text.partition("@")
    if len(local) <= 2:
        masked = local[0] + "***" if local else "***"
    else:
        masked = local[0] + "***" + local[-1]
    return f"{masked}@{domain}"


__all__ = [
    "CAPTCHA_TTL",
    "CAPTCHA_MAX_ATTEMPTS",
    "CAPTCHA_MAX_ENTRIES",
    "CAPTCHA_KINDS",
    "CAPTCHA_CHARS",
    "CAPTCHA_CHAR_LENGTH",
    "EMAIL_CODE_DIGITS",
    "EMAIL_CODE_TTL",
    "EMAIL_CODE_MAX_ATTEMPTS",
    "EMAIL_RESEND_INTERVAL",
    "EMAIL_CODE_ITERATIONS",
    "PURPOSE_EMAIL",
    "PURPOSE_EMAIL_LINK",
    "PURPOSE_LOGIN_LINK",
    "PURPOSE_PASSWORD_RESET",
    "send_email_link",
    "check_email_link",
    "SMTP_ENV_KEYS",
    "MailerError",
    "CaptchaStore",
    "RecordingMailer",
    "SmtpMailer",
    "captcha_stats",
    "check_captcha",
    "check_email_code",
    "email_disabled_reason",
    "email_enabled",
    "email_verification_state",
    "mailer_from_env",
    "mask_email",
    "new_captcha",
    "reset_captchas",
    "send_email_code",
    "smtp_settings",
]