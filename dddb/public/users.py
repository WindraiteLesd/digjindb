"""用户端自己的账号存储(**独立数据库文件**, 与专辑主库完全无关).

设计要点:

* **物理隔离**: 用户数据落在 ``data/public_users.sqlite3``(可用 ``--users-db``
  覆盖), 绝不往专辑主库 ``data/dddb.sqlite3`` 里加表 —— 用户端是可以整体删除、
  整体搬走的一个目录, 主库的 schema 不该因为"多了几个访客"而变化;
* **密码绝不明文**: 用 stdlib :func:`hashlib.pbkdf2_hmac` + 每用户随机 salt,
  存成 ``pbkdf2_sha256$<iterations>$<salt>$<digest>`` 自描述字符串(以后调大
  迭代次数也能验旧密码);
* **三级角色** ``viewer`` < ``editor`` < ``superuser``:

  ==============  ======  ===========================================
  角色            能写?   额外能力
  ==============  ======  ===========================================
  ``viewer``      否      浏览、看「我的账号」、**申请成为编辑者**
  ``editor``      是      订正专辑资料(与旧版一致)
  ``superuser``   是      审核编辑者申请、改角色 / 禁用账号、看审核队列
  ==============  ======  ===========================================

  注册一律 ``viewer``; ``editor`` 有两条路: 一次性邀请码(旧机制, 保留), 或者
  **用户自己申请 → 超级用户审核通过**(本次新增, 见 :meth:`UserStore.request_editor`)。
* **邮箱验证**: ``users`` 表新增 ``email`` / ``email_verified`` / ``verified_at``;
  ``is_active`` 用于禁用账号。**老库自动迁移**(``ALTER TABLE ADD COLUMN``),
  老数据里没有的记录一律视为"已通过邮箱验证", 于是升级不会把老账号锁在门外。
* 只用 stdlib, 没有任何第三方依赖。

命令行::

    python -m dddb.public.users init                       # 建表 + 迁表(幂等)
    python -m dddb.public.users add <用户名> [--role viewer|editor|superuser]
    python -m dddb.public.users list / show <用户名>
    python -m dddb.public.users request <用户名> --reason "想补曲目表"
    python -m dddb.public.users requests [--status pending|approved|rejected|all]
    python -m dddb.public.users approve <用户名|申请id> [--note "..."]
    python -m dddb.public.users reject  <用户名|申请id> [--note "..."]
    python -m dddb.public.users set-role <用户名> --role <角色>
    python -m dddb.public.users disable <用户名> / enable <用户名>
    python -m dddb.public.users verify <用户名>            # 人工标记邮箱已验证
    python -m dddb.public.users set-email <用户名> <邮箱>
    python -m dddb.public.users passwd <用户名> / remove <用户名>
    python -m dddb.public.users invite [--note "..."] / invites
"""

from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

logger = logging.getLogger("dddb.public.users")

#: 用户库默认文件名(位于项目 ``data/`` 目录下)
DEFAULT_USERS_FILENAME = "public_users.sqlite3"

# --------------------------------------------------------------------------
# 角色
# --------------------------------------------------------------------------

ROLE_VIEWER = "viewer"
ROLE_EDITOR = "editor"
ROLE_SUPERUSER = "superuser"

#: 全部合法角色, **由低到高**(顺序即权限高低; 后端只认这三个值)
ROLES: tuple[str, ...] = (ROLE_VIEWER, ROLE_EDITOR, ROLE_SUPERUSER)

#: 能写(编辑专辑资料)的角色
WRITE_ROLES: frozenset[str] = frozenset({ROLE_EDITOR, ROLE_SUPERUSER})

#: 能审核编辑者申请 / 管理用户的角色
REVIEW_ROLES: frozenset[str] = frozenset({ROLE_SUPERUSER})

#: 角色 -> 数值等级(便于"至少是编辑者"这类判断)
ROLE_RANK: dict[str, int] = {ROLE_VIEWER: 10, ROLE_EDITOR: 20, ROLE_SUPERUSER: 30}

#: 角色 -> 中文名
ROLE_LABELS: dict[str, str] = {
    ROLE_VIEWER: "普通用户",
    ROLE_EDITOR: "编辑者",
    ROLE_SUPERUSER: "超级用户",
}

#: 编辑者申请的状态
REQUEST_PENDING = "pending"
REQUEST_APPROVED = "approved"
REQUEST_REJECTED = "rejected"
REQUEST_STATUSES: tuple[str, ...] = (REQUEST_PENDING, REQUEST_APPROVED, REQUEST_REJECTED)
REQUEST_STATUS_LABELS: dict[str, str] = {
    REQUEST_PENDING: "待审核",
    REQUEST_APPROVED: "已通过",
    REQUEST_REJECTED: "已拒绝",
}

#: 申请理由 / 审核备注的长度上限
MAX_REASON = 500
MAX_NOTE = 500

#: 登录失败/拒绝的原因代码(给页面做分支用)
LOGIN_OK = "ok"
LOGIN_BAD_CREDENTIALS = "bad_credentials"
LOGIN_INACTIVE = "inactive"                 # 账号被禁用
LOGIN_UNVERIFIED_EMAIL = "unverified_email"  # 邮箱还没验证

# --------------------------------------------------------------------------
# 密码哈希 / 邮箱
# --------------------------------------------------------------------------

#: 密码哈希参数(存档格式里自带, 以后调大不影响老密码的校验)
PBKDF2_ITERATIONS = 240_000
PBKDF2_SALT_BYTES = 16
PBKDF2_DKLEN = 32
PBKDF2_ALGO = "pbkdf2_sha256"

#: 用户名 / 密码的长度约束
MIN_USERNAME = 3
MAX_USERNAME = 32
MIN_PASSWORD = 8
MAX_PASSWORD = 200

#: 邮箱长度上限与一个"够用就好"的格式检查(真正的有效性靠验证码)
MAX_EMAIL = 254
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s.]{2,24}$")

#: 用户名里不允许出现的字符(防止把分隔符写进密码 / 路径 / URL)
USERNAME_BAD = set(" \t\r\n/\\\"'`<>&|?#%")

#: 用户名里**不能包含**的违规词(按"抹平分隔符后的子串"匹配; 长度 >= 4 的才做子串)。
#: 目的: 防冒充站方/管理员、防广告与辱骂 —— 用户明确要求"用户名里不能有违规词汇"。
USERNAME_BANNED_PATTERNS: tuple[str, ...] = (
    # **只拦内容违规**: 广告 / 联系方式 / 辱骂。
    # 注意: "冒充站方"类(admin/管理员/官方/客服…) **不再禁止** ——
    # 改用身份标识徽章区分(见 templates.identity_badge), 名字可自由取, 但
    # 展示时永远带"用户/编辑者/站务"徽章, 冒充不起来。
    "taobao", "tiktok", "telegram",
    "fuck", "shit", "bitch", "asshole", "nigger",
)

#: 太短不宜做子串匹配的词(会把 `wxz`/`qqq` 这类正常名字误伤), 只在**整词**出现时拒。
USERNAME_BANNED_SHORT: tuple[str, ...] = ("wx", "vx", "qq", "tk")

# --------------------------------------------------------------------------
# 表结构(+ 老库迁移)
# --------------------------------------------------------------------------

#: 基础表: ``users`` 的**原始 6 列**永远保留(向后兼容), 新列靠迁移追加.
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'viewer',
  created_at REAL NOT NULL,
  last_login REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS invite_codes (
  code TEXT PRIMARY KEY,
  note TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  used_by TEXT NOT NULL DEFAULT '',
  used_at REAL NOT NULL DEFAULT 0,
  created_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS editor_requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  created_at REAL NOT NULL DEFAULT 0,
  handled_by TEXT NOT NULL DEFAULT '',
  handled_at REAL NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS verify_codes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  purpose TEXT NOT NULL DEFAULT 'email',
  subject TEXT NOT NULL DEFAULT '',
  code_hash TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL DEFAULT 0,
  expires_at REAL NOT NULL DEFAULT 0,
  attempts INTEGER NOT NULL DEFAULT 0,
  consumed_at REAL NOT NULL DEFAULT 0
);
"""

#: ``users`` 表的增量列(老库缺哪列补哪列). 顺序就是追加顺序.
USER_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("email", "TEXT NOT NULL DEFAULT ''"),
    ("email_verified", "INTEGER NOT NULL DEFAULT 1"),
    ("verified_at", "REAL NOT NULL DEFAULT 0"),
    ("is_active", "INTEGER NOT NULL DEFAULT 1"),
    # 上次登录环境指纹(sha256(ip|ua)); 换了环境要发邮件确认
    ("last_env_hash", "TEXT NOT NULL DEFAULT ''"),
    # 最后活跃时间(用户 m14663): ``last_login`` 只记「登录那一刻」, 用它当
    # 「最后活跃」是错的 —— 有人登录一次连着用一周, 资料页会一直显示那一天。
    # 这一列由 :meth:`UserStore.touch_seen` 在每次请求时(节流)刷新。
    ("last_seen", "REAL NOT NULL DEFAULT 0"),
)

#: ``invite_codes`` 表的增量列(``created_by``: 让编辑者自己发邀请码之后才有的列).
INVITE_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("created_by", "TEXT NOT NULL DEFAULT ''"),
)

#: 建在迁移之后的索引(必须晚于列追加, 否则老库上会因缺列失败)
POST_MIGRATION_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE UNIQUE INDEX IF NOT EXISTS idx_editor_requests_pending
    ON editor_requests(user_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_editor_requests_status ON editor_requests(status, created_at);
CREATE INDEX IF NOT EXISTS idx_verify_codes_subject ON verify_codes(purpose, subject, created_at);
"""


class UserStoreError(RuntimeError):
    """用户库打不开 / 写不进去."""


# --------------------------------------------------------------------------
# 密码哈希(纯函数, 可单测)
# --------------------------------------------------------------------------


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def hash_password(
    password: str,
    *,
    salt: bytes | None = None,
    iterations: int = PBKDF2_ITERATIONS,
) -> str:
    """``pbkdf2_sha256$<iterations>$<salt_b64url>$<digest_b64url>``.

    ``salt`` 只为测试可复现而暴露; 正常调用永远走 :func:`secrets.token_bytes`。
    邮箱验证码也复用同一套哈希(见 :mod:`dddb.public.verify`)。
    """
    if password is None:
        password = ""
    if not isinstance(password, str):  # pragma: no cover - 防御性
        password = str(password)
    used_salt = bytes(salt) if salt else secrets.token_bytes(PBKDF2_SALT_BYTES)
    rounds = max(1, int(iterations))
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), used_salt, rounds, dklen=PBKDF2_DKLEN)
    return f"{PBKDF2_ALGO}${rounds}${_b64(used_salt)}${_b64(digest)}"


def verify_password(password: str, encoded: Any) -> bool:
    """校验密码. 任何格式异常一律返回 ``False``(不抛异常, 不泄漏内部状态)."""
    if not isinstance(encoded, str) or encoded.count("$") != 3:
        return False
    algo, rounds_text, salt_text, digest_text = encoded.split("$", 3)
    if algo != PBKDF2_ALGO:
        return False
    try:
        rounds = int(rounds_text)
        salt = _unb64(salt_text)
        expected = _unb64(digest_text)
    except (ValueError, TypeError, binascii.Error):
        return False
    if rounds <= 0 or rounds > 10_000_000 or not salt or not expected:
        return False
    try:
        actual = hashlib.pbkdf2_hmac(
            "sha256", str(password or "").encode("utf-8"), salt, rounds, dklen=len(expected)
        )
    except (ValueError, TypeError):  # pragma: no cover - 防御性
        return False
    return hmac.compare_digest(actual, expected)


#: 密码组成要求(用户原话: "密码为至少 8 位数字加英文字母的组合")
PASSWORD_DIGITS = "0123456789"


def _is_ascii_letter(char: str) -> bool:
    """英文字母: **只认 ASCII**(``Ａ``/``中``/``ア`` 都不算)."""
    return char.isascii() and char.isalpha()


def _is_password_digit(char: str) -> bool:
    """数字: 只认半角 0-9(全角数字不算, 免得用户以为满足了却过不了)."""
    return char in PASSWORD_DIGITS


def can_create_invite(role: Any) -> bool:
    """**谁能生成邀请码**: 编辑者与超级用户(用户要求"编辑者可直接生成")."""
    return clean_role(role) in WRITE_ROLES


def password_problem(password: Any) -> str:
    """密码不合规时返回原因(空串 = 合规).

    规则(前 4 条是老规则, 后 2 条是用户新要求):

    1. 非空;
    2. 至少 ``MIN_PASSWORD`` (8) 位;
    3. 不超过 ``MAX_PASSWORD`` 位;
    4. 首尾不能是空白字符;
    5. **至少含一个数字**; 
    6. **至少含一个英文字母**(``ch.isascii() and ch.isalpha()``, 中文不算)。
    """
    text = password if isinstance(password, str) else ""
    if not text:
        return "请输入密码"
    if len(text) < MIN_PASSWORD:
        return f"密码至少 {MIN_PASSWORD} 位"
    if len(text) > MAX_PASSWORD:
        return f"密码最长 {MAX_PASSWORD} 位"
    if text.strip() != text:
        return "密码首尾不能是空白字符"
    if not any(_is_password_digit(ch) for ch in text):
        return "密码需要包含数字"
    if not any(_is_ascii_letter(ch) for ch in text):
        return "密码需要包含英文字母"
    return ""


#: 密码策略的一句话说明(页面/CLI 提示用, 与 :func:`password_problem` 保持一致)
PASSWORD_RULE_TEXT = (
    f"至少 {MIN_PASSWORD} 位, 且必须同时包含数字和英文字母"
)


def registration_name_problem(username: Any, *, taken: bool = False) -> str:
    """**注册时的用户名判据**(注册页实时提示与提交校验共用这一份).

    顺序与用户可见提示一致: 长度/字符集 -> 违规词 -> 是否已被占用。
    空串 = 可以注册。
    """
    problem = username_problem(username)
    if problem:
        return problem
    banned = username_banned_word(username)
    if banned:
        return f"用户名里不能包含违规词汇: {banned}"
    if taken:
        return "这个用户名已被占用, 换一个吧"
    return ""


def username_problem(username: Any) -> str:
    """用户名不合规时返回原因(空串 = 合规)."""
    text = username if isinstance(username, str) else ""
    text = text.strip()
    if not text:
        return "请输入用户名"
    if len(text) < MIN_USERNAME:
        return f"用户名至少 {MIN_USERNAME} 个字符"
    if len(text) > MAX_USERNAME:
        return f"用户名最长 {MAX_USERNAME} 个字符"
    if any(ch in USERNAME_BAD for ch in text):
        return "用户名不能包含空格或 / \\\" ' ` < > & | ? # % 等字符"
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return "用户名不能包含控制字符"
    # **违规词不在这里**: 这是通用校验(CLI/系统建号也走), 站方账号要能建。
    # 用户自助注册的违规词拦截在 site.py 的 _do_register 里(见 username_banned_word)。
    return ""


def _flatten(value: str) -> str:
    """抹平大小写与分隔符(空格/_/./-), 让 `a d m i n` 也躲不过去."""
    return re.sub(r"[\s._\-]+", "", str(value).casefold())


def username_banned_word(username: Any) -> str:
    """返回命中的违规词(没有则空串). 供注册/改名/查重接口共用."""
    raw = str(username or "")
    if not raw:
        return ""
    flat = _flatten(raw)
    for word in USERNAME_BANNED_PATTERNS:
        if _flatten(word) in flat:
            return word
    tokens = {tok for tok in re.split(r"[\s._\-]+", raw.casefold()) if tok}
    for word in USERNAME_BANNED_SHORT:
        if word in tokens:
            return word
    return ""


def normalize_email(address: Any) -> str:
    """邮箱规整(去空白 + 小写). 空值返回空串."""
    return str(address or "").strip().lower()


def email_problem(address: Any) -> str:
    """邮箱不合规时返回原因(空串 = 合规); **空邮箱视为合规**(可以不填)."""
    text = normalize_email(address)
    if not text:
        return ""
    if len(text) > MAX_EMAIL:
        return f"邮箱最长 {MAX_EMAIL} 个字符"
    if not _EMAIL_RE.match(text):
        return "邮箱格式不对"
    return ""


def email_verification_blocks_login() -> bool:
    """邮箱未验证的账号能不能登录? **不能**(``authenticate`` 返回 ``unverified_email``).

    注意: 这只影响 ``email_verified=0`` 的账号 —— 老库迁移出来的账号、CLI 建的
    账号、以及"没填邮箱"的账号都是已验证(1), 所以升级不会把任何人锁在门外。
    """
    return True


# --------------------------------------------------------------------------
# 角色工具(纯函数, 可单测)
# --------------------------------------------------------------------------


def clean_role(role: Any) -> str:
    """把任意输入规整成合法角色(未知值一律降级为 ``viewer``)."""
    text = str(role or "").strip().lower()
    return text if text in ROLES else ROLE_VIEWER


def role_rank(role: Any) -> int:
    return ROLE_RANK.get(clean_role(role), 0)


def has_role(role: Any, minimum: Any) -> bool:
    """``role`` 是否**至少**达到 ``minimum``(未知用户/角色一律 False)."""
    text = str(role or "").strip().lower()
    if text not in ROLES:
        return False
    return ROLE_RANK[text] >= ROLE_RANK.get(clean_role(minimum), 99)


def can_write(role: Any) -> bool:
    """这个角色能不能写(未登录/未知角色一律不能)."""
    return str(role or "").strip().lower() in WRITE_ROLES


def can_review(role: Any) -> bool:
    """能不能审核编辑者申请 / 管理用户(只有超级用户)."""
    return str(role or "").strip().lower() in REVIEW_ROLES


def is_superuser(role: Any) -> bool:
    return clean_role(role) == ROLE_SUPERUSER


def role_label(role: Any) -> str:
    return ROLE_LABELS.get(clean_role(role), ROLE_LABELS[ROLE_VIEWER])


def request_status_label(status: Any) -> str:
    return REQUEST_STATUS_LABELS.get(str(status or "").strip().lower(), "待审核")


def _time_text(stamp: Any, lang: Any = "zh") -> str:
    """时间戳 -> ``2026-11-08 04:30``(空/非法 -> ``—``).

    按**语言时区**显示(zh=UTC+8), 不用服务器本地时区。
    """
    from .timefmt import format_stamp

    return format_stamp(stamp, lang, empty="—")


# --------------------------------------------------------------------------
# 登录结果
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoginResult:
    """:meth:`UserStore.authenticate` 的返回值(页面按 ``reason`` 分支).

    * ``ok`` —— 成功, ``user`` 是视图字典(含 role/role_label);
    * ``reason`` —— ``ok`` / ``bad_credentials`` / ``inactive`` / ``unverified_email``;
    * ``need_verify`` —— True 表示"密码对、但邮箱没验证", 页面应当引导去验证;
    * ``message`` —— 可直接展示给用户的中文说明。
    """

    ok: bool
    reason: str
    user: dict[str, Any] | None = None
    message: str = ""

    @property
    def need_verify(self) -> bool:
        return self.reason == LOGIN_UNVERIFIED_EMAIL

    @property
    def denied(self) -> bool:
        return not self.ok


# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------


def default_users_path(root: Path | str) -> Path:
    """``<root>/data/public_users.sqlite3``."""
    return (Path(root) / "data" / DEFAULT_USERS_FILENAME).resolve()


class UserStore:
    """用户库(独立 sqlite 文件). 线程安全, 只写这个小文件."""

    def __init__(self, path: Path | str, *, iterations: int = PBKDF2_ITERATIONS) -> None:
        self.path = Path(path)
        self.iterations = int(iterations)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=15.0)
        except sqlite3.Error as exc:  # pragma: no cover - 磁盘级故障
            raise UserStoreError(f"打不开用户库 {self.path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        try:
            with self._lock:
                self._conn.executescript(SCHEMA)
                self._conn.commit()
                self._migrate_locked()
        except sqlite3.Error as exc:
            raise UserStoreError(f"初始化用户库失败 {self.path}: {exc}") from exc

    # -- 低层 ---------------------------------------------------------------

    @property
    def readonly(self) -> bool:
        return False

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, tuple(params))
            self._conn.commit()

    def _execute_rowcount(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._lock:
            cursor = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return max(0, int(cursor.rowcount or 0))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def columns(self) -> list[str]:
        """``users`` 表当前列(**前 6 列永远是原始列**, 新列追加在后面)."""
        return [str(row["name"]) for row in self._query("PRAGMA table_info(users)")]

    def table_names(self) -> list[str]:
        """用户库里的表名(排序后). 便于 CLI / 运维确认迁移结果."""
        return sorted(
            str(row["name"]) for row in self._query(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        )

    def schema_version(self) -> int:
        """一个轻量的"表结构版本"标记: 6 = 只有原始列, 10 = 已加 4 个扩展列."""
        return len(self.columns())

    def _migrate_locked(self) -> list[str]:
        """给老库补列 + 建索引. 幂等, 返回**本次新加**的列名(测试用).

        **两张表都要迁**: ``users``(邮箱/禁用等) 与 ``invite_codes``(created_by)。
        以前只迁 users, 结果云端 ``invite_codes.created_by`` 一直不存在,
        邀请码页直接 500(``no such column: created_by``)。
        """
        added: list[str] = []
        for table, columns in (("users", USER_EXTRA_COLUMNS),
                               ("invite_codes", INVITE_EXTRA_COLUMNS)):
            try:
                existing = {str(row["name"]) for row in
                            self._conn.execute(f"PRAGMA table_info({table})")}
            except sqlite3.Error:                    # pragma: no cover - 表还没建
                continue
            for name, ddl in columns:
                if name in existing:
                    continue
                try:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                    added.append(f"{table}.{name}")
                except sqlite3.Error as exc:         # pragma: no cover - 并发迁移
                    logger.warning("给 %s 补列 %s 失败: %s", table, name, exc)
        self._conn.commit()
        try:
            self._conn.executescript(POST_MIGRATION_INDEXES)
            self._conn.commit()
        except sqlite3.Error as exc:  # pragma: no cover
            logger.warning("建索引失败: %s", exc)
        if added:
            logger.info("用户库已迁移, 新增列: %s", ", ".join(added))
        return added

    # -- 读 -----------------------------------------------------------------

    @staticmethod
    def _row_value(row: sqlite3.Row, key: str, default: Any = None) -> Any:
        """安全取列(老库/新库的列集合不同, 缺列时给默认值)."""
        try:
            return row[key]
        except (IndexError, KeyError):
            return default

    @classmethod
    def _user_dict(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        """行 -> 视图字典. **绝不把 password_hash 交出去**."""
        if row is None:
            return None
        role = clean_role(cls._row_value(row, "role"))
        mode = cls._row_value(row, "email_verified", 1)
        active = cls._row_value(row, "is_active", 1)
        return {
            "id": int(row["id"]),
            "username": str(row["username"]),
            "role": role,
            "role_label": role_label(role),
            "created_at": float(row["created_at"] or 0.0),
            "last_login": float(row["last_login"] or 0.0),
            # 最后活跃: 老库还没写过这一列时回退到 last_login, 免得资料页
            # 变成「最后活跃 —」。两者都是 unix 时间戳。
            "last_seen": float(cls._row_value(row, "last_seen", 0.0) or 0.0)
            or float(row["last_login"] or 0.0),
            "email": normalize_email(cls._row_value(row, "email", "")),
            "email_verified": bool(int(mode or 0)),
            "verified_at": float(cls._row_value(row, "verified_at", 0.0) or 0.0),
            "is_active": bool(int(active if active is not None else 1)),
            # 便捷布尔(页面直接拿去控制按钮显示)
            "is_editor": role in (ROLE_EDITOR, ROLE_SUPERUSER),
            "can_write": can_write(role),
            "can_review": can_review(role),
            "is_superuser": role == ROLE_SUPERUSER,
        }

    def get(self, username: Any) -> dict[str, Any] | None:
        text = str(username or "").strip()
        if not text:
            return None
        return self._user_dict(
            self._one("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (text,))
        )

    def get_by_id(self, user_id: Any) -> dict[str, Any] | None:
        try:
            value = int(user_id)
        except (TypeError, ValueError):
            return None
        return self._user_dict(self._one("SELECT * FROM users WHERE id = ?", (value,)))

    def list_users(self) -> list[dict[str, Any]]:
        """账号列表: **按序号(id)升序**, 不按用户名首字母.

        按 id 排才和"用户 #8 / #9"这类展示对得上; 按名字排会让序号看起来乱跳,
        排查账号时很难扫。
        """
        rows = self._query("SELECT * FROM users ORDER BY id")
        return [self._user_dict(row) or {} for row in rows]

    def count(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM users")
        return int(row["n"]) if row is not None else 0

    def count_by_role(self, role: Any) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM users WHERE role = ?", (clean_role(role),))
        return int(row["n"]) if row is not None else 0

    def superuser_count(self) -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM users WHERE role = ?"
            " AND COALESCE(is_active, 1) = 1", (ROLE_SUPERUSER,)
        )
        return int(row["n"]) if row is not None else 0

    def pending_request_for(self, username: Any) -> dict[str, Any] | None:
        user = self.get(username)
        if user is None:
            return None
        return self.editor_request_view(
            self._one("SELECT * FROM editor_requests WHERE user_id = ? AND status = ?"
                      " ORDER BY id DESC LIMIT 1", (int(user["id"]), REQUEST_PENDING))
        )

    # -- 写:建号 / 密码 / 角色 / 状态 ---------------------------------------

    def create_user(
        self,
        username: Any,
        password: Any,
        *,
        role: str = ROLE_VIEWER,
        email: Any = "",
        email_verified: bool | None = None,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """建号. 返回 ``(成功?, 消息)`` —— 失败原因是给人看的, 不抛异常.

        ``email_verified`` 的判定:

        * 显式传入就以传入值为准(注册流程传 ``False``, CLI/管理员默认 ``True``);
        * ``None`` + 填了邮箱 -> ``False``(需要走验证流程);
        * ``None`` + 没填邮箱 -> ``True``(没有邮箱就没有"待验证"这回事, 否则
          账号会永远登录不了)。
        """
        problem = username_problem(username) or password_problem(password) or email_problem(email)
        if problem:
            return False, problem
        name = str(username).strip()
        address = normalize_email(email)
        wanted = clean_role(role)
        if email_verified is None:
            verified = True if not address else False
        else:
            verified = bool(email_verified)
        if not address:
            verified = True
        stamp = time.time() if now is None else float(now)
        try:
            self._execute(
                "INSERT INTO users(username, password_hash, role, created_at, last_login,"
                " email, email_verified, verified_at, is_active)"
                " VALUES (?,?,?,?,0,?,?,?,1)",
                (
                    name, hash_password(str(password), iterations=self.iterations), wanted, stamp,
                    address, 1 if verified else 0, stamp if verified else 0.0,
                ),
            )
        except sqlite3.IntegrityError:
            return False, "这个用户名已经被占用了"
        except sqlite3.Error as exc:  # pragma: no cover
            return False, f"写入用户库失败: {exc}"
        return True, ""

    def set_password(self, username: Any, password: Any) -> tuple[bool, str]:
        problem = password_problem(password)
        if problem:
            return False, problem
        if self.get(username) is None:
            return False, "找不到这个用户"
        self._execute(
            "UPDATE users SET password_hash = ? WHERE username = ? COLLATE NOCASE",
            (hash_password(str(password), iterations=self.iterations), str(username).strip()),
        )
        return True, ""

    def set_email(self, username: Any, email: Any, *, verify: bool = False,
                  now: float | None = None) -> tuple[bool, str]:
        """改邮箱. ``verify=True`` 时同时标记为已验证(管理员操作/测试用)."""
        problem = email_problem(email)
        if problem:
            return False, problem
        if self.get(username) is None:
            return False, "找不到这个用户"
        address = normalize_email(email)
        stamp = time.time() if now is None else float(now)
        verified = bool(verify) or not address
        self._execute(
            "UPDATE users SET email = ?, email_verified = ?, verified_at = ?"
            " WHERE username = ? COLLATE NOCASE",
            (address, 1 if verified else 0, stamp if verified else 0.0, str(username).strip()),
        )
        return True, ""

    def verify_link_token(self, token: Any, *, purpose: str = "email",
                          now: float | None = None) -> tuple[bool, str, str]:
        """**只看不消费**地校验链接令牌(重置密码页先展示表单用)."""
        text = str(token or "").strip()
        if not text:
            return False, "链接不完整", ""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        rows = self._query(
            "SELECT * FROM verify_codes WHERE purpose = ? AND code_hash = ?",
            (str(purpose), digest))
        if not rows:
            return False, "链接无效或已被使用", ""
        row = rows[0]
        stamp = time.time() if now is None else float(now)
        if float(row["consumed_at"] or 0) > 0:
            return False, "链接已经用过了", ""
        if float(row["expires_at"] or 0) <= stamp:
            return False, "链接已过期, 请重新申请", ""
        return True, "", str(row["subject"] or "")

    def find_by_email(self, email: Any) -> dict[str, Any] | None:
        """按邮箱找用户(找回密码用)."""
        address = normalize_email(email)
        if not address:
            return None
        rows = self._query(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (address,))
        return self._user_dict(rows[0]) if rows else None

    def purge_unverified(self, *, older_than: float = 24 * 3600.0,
                         now: float | None = None) -> int:
        """删除"注册后超过 N 小时仍未验证邮箱"的账号; 返回删除条数."""
        stamp = time.time() if now is None else float(now)
        cutoff = stamp - float(older_than)
        rows = self._query(
            "SELECT id, username FROM users WHERE email_verified = 0"
            " AND email != '' AND created_at <= ?", (cutoff,))
        removed = 0
        for row in rows:
            try:
                self._execute("DELETE FROM users WHERE id = ?", (int(row["id"]),))
                removed += 1
            except sqlite3.Error:                # pragma: no cover
                continue
        return removed

    # -- 邮件链接令牌(点邮件里的链接才算验证) -----------------------------

    def issue_email_link(self, username: Any, *, purpose: str = "email",
                         ttl: float = 24 * 3600.0,
                         now: float | None = None) -> str:
        """签发一次性链接令牌, 返回**明文令牌**(库里只存 sha256)."""
        user = self.get(username)
        if user is None:
            return ""
        token = secrets.token_urlsafe(32)
        stamp = time.time() if now is None else float(now)
        self._execute(
            "INSERT INTO verify_codes(purpose, subject, code_hash, created_at, expires_at)"
            " VALUES (?,?,?,?,?)",
            (str(purpose), str(user["username"]),
             hashlib.sha256(token.encode("utf-8")).hexdigest(), stamp, stamp + float(ttl)),
        )
        return token

    def redeem_email_link(self, token: Any, *, purpose: str = "email",
                          now: float | None = None) -> tuple[bool, str, str]:
        """核销链接令牌 -> ``(成功?, 原因, 用户名)``."""
        text = str(token or "").strip()
        if not text:
            return False, "链接不完整", ""
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        rows = self._query(
            "SELECT * FROM verify_codes WHERE purpose = ? AND code_hash = ?",
            (str(purpose), digest),
        )
        if not rows:
            return False, "链接无效或已被使用", ""
        row = rows[0]
        stamp = time.time() if now is None else float(now)
        if float(row["consumed_at"] or 0) > 0:
            return False, "链接已经用过了", ""
        if float(row["expires_at"] or 0) <= stamp:
            return False, "链接已过期, 请重新发送验证邮件", ""
        self._execute("UPDATE verify_codes SET consumed_at = ? WHERE id = ?",
                      (stamp, int(row["id"])))
        return True, "", str(row["subject"] or "")

    def last_env(self, username: Any) -> str:
        """直查列(``get()`` 的字典是早期列清单拼的, 不含后加的 last_env_hash)."""
        row = self._one(
            "SELECT last_env_hash FROM users WHERE username = ? COLLATE NOCASE",
            (str(username or "").strip(),))
        return str(row["last_env_hash"] or "") if row is not None else ""

    def set_last_env(self, username: Any, env_hash: Any) -> bool:
        user = self.get(username)
        if user is None:
            return False
        self._execute("UPDATE users SET last_env_hash = ? WHERE id = ?",
                      (str(env_hash or "")[:128], int(user["id"])))
        return True

    # -- 最后活跃(用户 m14663) ----------------------------------------------

    def touch_seen(self, user_id: Any, *, now: float | None = None,
                   min_gap: float = 300.0) -> bool:
        """把「最后活跃」推到当下. 距上次记录不足 ``min_gap`` 秒就直接跳过.

        为什么节流: 一次页面浏览会带出 css/js/图片好几个请求, 每次都写库
        既浪费又把 sqlite 写锁抢得很频繁。5 分钟粒度对「最后活跃」这种展示
        完全够用。返回是否真的写了。
        """
        try:
            number = int(user_id)
        except (TypeError, ValueError):
            return False
        if number <= 0:
            return False
        stamp = time.time() if now is None else float(now)
        row = self._one("SELECT last_seen FROM users WHERE id = ?", (number,))
        if row is None:
            return False
        try:
            previous = float(row["last_seen"] or 0.0)
        except (TypeError, ValueError):  # pragma: no cover - 防御性
            previous = 0.0
        if previous and 0 <= stamp - previous < max(0.0, float(min_gap)):
            return False
        try:
            self._execute("UPDATE users SET last_seen = ? WHERE id = ?",
                          (stamp, number))
        except sqlite3.Error:  # pragma: no cover
            logger.warning("刷新 last_seen 失败: %s", number)
            return False
        return True

    def mark_email_verified(self, username: Any, *, now: float | None = None) -> tuple[bool, str]:
        """把账号标记为"邮箱已验证"(验证码通过 / 管理员救急)."""
        user = self.get(username)
        if user is None:
            return False, "找不到这个用户"
        stamp = time.time() if now is None else float(now)
        self._execute(
            "UPDATE users SET email_verified = 1, verified_at = ? WHERE id = ?",
            (stamp, int(user["id"])),
        )
        return True, ""

    def require_superuser(self, actor: Any) -> tuple[bool, str]:
        """``actor`` 是不是超级用户. 返回 ``(是否允许, 拒绝原因)``.

        ``actor`` 传 ``None``/``""`` 表示"系统/CLI 操作", 直接放行(命令行没有
        登录态, 由操作系统权限兜底); 页面接线时**必须**传当前登录用户名。
        """
        name = str(actor or "").strip()
        if not name:
            return True, ""
        user = self.get(name)
        if user is None:
            return False, "找不到操作者账号"
        if not user["is_active"]:
            return False, "操作者账号已被禁用"
        if not can_review(user["role"]):
            return False, "需要超级用户权限"
        return True, ""

    def set_role(self, username: Any, role: Any, *, actor: Any = None) -> tuple[bool, str]:
        """改角色. ``actor`` 非空时必须是超级用户(页面务必传)."""
        allowed, why = self.require_superuser(actor)
        if not allowed:
            return False, why
        user = self.get(username)
        if user is None:
            return False, "找不到这个用户"
        wanted = clean_role(role)
        if user["role"] == ROLE_SUPERUSER and wanted != ROLE_SUPERUSER and self.superuser_count() <= 1:
            return False, "不能降级最后一个超级用户"
        self._execute(
            "UPDATE users SET role = ? WHERE username = ? COLLATE NOCASE",
            (wanted, str(username).strip()),
        )
        return True, ""

    def promote(self, username: Any, *, actor: Any = None) -> tuple[bool, str]:
        """便捷方法: 提升为编辑者(等价于 ``set_role(u, "editor")``)."""
        return self.set_role(username, ROLE_EDITOR, actor=actor)

    def set_active(self, username: Any, active: bool, *, actor: Any = None) -> tuple[bool, str]:
        """启用 / 禁用账号. 禁用后 :meth:`authenticate` 直接拒绝。

        不允许禁用最后一个可用的超级用户(否则没人能再进后台)。
        """
        allowed, why = self.require_superuser(actor)
        if not allowed:
            return False, why
        user = self.get(username)
        if user is None:
            return False, "找不到这个用户"
        if not active and user["role"] == ROLE_SUPERUSER and self.superuser_count() <= 1:
            return False, "不能禁用最后一个超级用户"
        self._execute(
            "UPDATE users SET is_active = ? WHERE username = ? COLLATE NOCASE",
            (1 if active else 0, str(username).strip()),
        )
        return True, ""

    def remove(self, username: Any, *, actor: Any = None) -> tuple[bool, str]:
        allowed, why = self.require_superuser(actor)
        if not allowed:
            return False, why
        user = self.get(username)
        if user is None:
            return False, "找不到这个用户"
        if user["role"] == ROLE_SUPERUSER and self.superuser_count() <= 1:
            return False, "不能删除最后一个超级用户"
        self._execute("DELETE FROM users WHERE username = ? COLLATE NOCASE", (str(username).strip(),))
        self._execute("DELETE FROM editor_requests WHERE user_id = ?", (int(user["id"]),))
        return True, ""

    # -- 登录 ---------------------------------------------------------------

    def authenticate(self, username: Any, password: Any, *,
                     now: float | None = None) -> LoginResult:
        """登录鉴权, **带原因**的完整结果(页面用这个方法).

        顺序: 密码 -> 账号是否禁用 -> 邮箱是否验证 -> 放行。
        用户不存在/密码错都走一遍哈希, 让响应耗时接近(不泄漏用户名是否存在)。
        """
        name = str(username or "").strip()
        row = self._one("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (name,)) if name else None
        stored = str(row["password_hash"]) if row is not None else ""
        if not verify_password(password, stored):
            if row is None:
                verify_password(str(password or ""), hash_password("dummy-password-for-timing"))
            return LoginResult(False, LOGIN_BAD_CREDENTIALS, None, "用户名或密码不正确")
        user = self._user_dict(row) or {}
        if not user.get("is_active", True):
            return LoginResult(False, LOGIN_INACTIVE, user, "账号已被禁用, 请联系管理员")
        # **未验证邮箱也允许登录**(用户要求): 但只有访客权限, 页面会提示验证,
        # 超过 24 小时仍未验证的账号会被 purge_unverified() 删除。
        stamp = time.time() if now is None else float(now)
        try:
            # 登录同时把「最后活跃」推到当下 —— 否则资料页会拿很久以前
            # 的上次登录冒充活跃时间。
            self._execute(
                "UPDATE users SET last_login = ?, last_seen = ? WHERE id = ?",
                (stamp, stamp, int(row["id"])))
        except sqlite3.Error:  # pragma: no cover - 时间戳写不进去不该拦住登录
            logger.warning("刷新 last_login 失败: %s", name)
        user["last_login"] = stamp
        user["last_seen"] = stamp
        return LoginResult(True, LOGIN_OK, user, "")

    def check_login(self, username: Any, password: Any, *,
                    now: float | None = None) -> dict[str, Any] | None:
        """兼容旧接口: 只有**完全可用**的账号才返回用户字典, 否则 ``None``.

        页面想要"为什么登不上"的提示时请改用 :meth:`authenticate`。
        """
        result = self.authenticate(username, password, now=now)
        return result.user if result.ok else None

    # -- 邀请码 -------------------------------------------------------------

    def create_invite(self, *, note: str = "", created_by: str = "",
                      now: float | None = None) -> str:
        """生成一个一次性编辑者邀请码. ``created_by`` = 生成者用户名(页面用)."""
        stamp = time.time() if now is None else float(now)
        for _ in range(8):
            code = secrets.token_urlsafe(12)
            try:
                self._execute(
                    "INSERT INTO invite_codes(code, note, created_by, created_at)"
                    " VALUES (?,?,?,?)",
                    (code, str(note or ""), str(created_by or "").strip(), stamp),
                )
                return code
            except sqlite3.IntegrityError:  # pragma: no cover - 撞码概率极低
                continue
        raise UserStoreError("生成邀请码失败")  # pragma: no cover

    def redeem_invite(self, code: Any, username: Any, *, now: float | None = None) -> bool:
        """核销邀请码(一次性). 有效则标记为被谁用掉并返回 ``True``."""
        text = str(code or "").strip()
        if not text:
            return False
        row = self._one("SELECT * FROM invite_codes WHERE code = ?", (text,))
        if row is None or str(row["used_by"] or ""):
            return False
        stamp = time.time() if now is None else float(now)
        self._execute(
            "UPDATE invite_codes SET used_by = ?, used_at = ? WHERE code = ? AND used_by = ''",
            (str(username or "").strip(), stamp, text),
        )
        return True

    def invite_info(self, code: Any) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM invite_codes WHERE code = ?", (str(code or "").strip(),))
        if row is None:
            return None
        return {
            "code": str(row["code"]),
            "note": str(row["note"] or ""),
            "created_at": float(row["created_at"] or 0.0),
            "used_by": str(row["used_by"] or ""),
            "used_at": float(row["used_at"] or 0.0),
            "used": bool(str(row["used_by"] or "")),
            "created_by": str(row["created_by"] if "created_by" in row.keys() else "" or ""),
        }

    def list_invites(self, *, created_by: str | None = None) -> list[dict[str, Any]]:
        """列出邀请码. ``created_by`` 非空时只列这个人生成的(编辑者看自己的)."""
        if created_by is None:
            rows = self._query("SELECT * FROM invite_codes ORDER BY created_at DESC")
        else:
            rows = self._query(
                "SELECT * FROM invite_codes WHERE created_by = ? ORDER BY created_at DESC",
                (str(created_by).strip(),))
        out = []
        for row in rows:
            out.append({
                "code": str(row["code"]),
                "note": str(row["note"] or ""),
                "created_at": float(row["created_at"] or 0.0),
                "created_by": str(row["created_by"] if "created_by" in row.keys() else ""),
                "used_by": str(row["used_by"] or ""),
                "used": bool(str(row["used_by"] or "")),
            })
        return out

    def invite_stats(self, *, created_by: str | None = None) -> dict[str, int]:
        """邀请码统计(页面顶部那三个数): total / used / unused."""
        rows = self.list_invites(created_by=created_by)
        used = sum(1 for item in rows if item.get("used"))
        return {"total": len(rows), "used": used, "unused": len(rows) - used}

    # -- 编辑者申请 ---------------------------------------------------------

    @staticmethod
    def editor_request_view(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        status = str(row["status"] or REQUEST_PENDING).strip().lower()
        handled_at = float(row["handled_at"] or 0.0)
        return {
            "id": int(row["id"]),
            "user_id": int(row["user_id"]),
            "reason": str(row["reason"] or ""),
            "status": status if status in REQUEST_STATUSES else REQUEST_PENDING,
            "status_label": request_status_label(status),
            "created_at": float(row["created_at"] or 0.0),
            "created_at_text": _time_text(row["created_at"]),
            "handled_by": str(row["handled_by"] or ""),
            "handled_at": handled_at,
            "handled_at_text": _time_text(handled_at),
            "note": str(row["note"] or ""),
        }

    def request_editor(self, username: Any, *, reason: Any = "",
                       now: float | None = None) -> tuple[bool, str]:
        """普通用户申请成为编辑者. 返回 ``(是否受理, 消息)``.

        拒绝的情形: 用户不存在 / 已经有写权限(editor、superuser) / 已有一条
        ``pending`` 申请(表上有部分唯一索引兜底)。
        """
        user = self.get(username)
        if user is None:
            return False, "找不到这个用户"
        if can_write(user["role"]):
            return False, "你已经有编辑权限了"
        if not user["is_active"]:
            return False, "账号已被禁用, 无法申请"
        if self.pending_request_for(user["username"]) is not None:
            return False, "你已经提交过申请了, 请等待审核"
        text = str(reason or "").strip()
        if len(text) > MAX_REASON:
            return False, f"申请理由最长 {MAX_REASON} 个字符"
        stamp = time.time() if now is None else float(now)
        try:
            self._execute(
                "INSERT INTO editor_requests(user_id, reason, status, created_at)"
                " VALUES (?,?,?,?)",
                (int(user["id"]), text, REQUEST_PENDING, stamp),
            )
        except sqlite3.IntegrityError:
            return False, "你已经提交过申请了, 请等待审核"
        except sqlite3.Error as exc:  # pragma: no cover
            return False, f"写入申请失败: {exc}"
        return True, ""

    def _find_request(self, identifier: Any) -> sqlite3.Row | None:
        """按"申请 id"(int / 纯数字且无同名用户)或"用户名"定位一条申请."""
        as_id: int | None = None
        if isinstance(identifier, bool):
            as_id = None
        elif isinstance(identifier, int):
            as_id = identifier
        else:
            text = str(identifier or "").strip()
            if text.isdigit() and self.get(text) is None:
                as_id = int(text)
        if as_id is not None:
            return self._one("SELECT * FROM editor_requests WHERE id = ?", (int(as_id),))
        user = self.get(identifier)
        if user is None:
            return None
        return self._one(
            "SELECT * FROM editor_requests WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (int(user["id"]),),
        )

    def get_editor_request(self, identifier: Any) -> dict[str, Any] | None:
        """按申请 id 或用户名取**最新一条**申请(含已处理的历史)."""
        request = self.editor_request_view(self._find_request(identifier))
        if request is None:
            return None
        user = self.get_by_id(request["user_id"])
        request["username"] = str((user or {}).get("username") or "")
        request["user_role"] = str((user or {}).get("role") or "")
        request["user_role_label"] = str((user or {}).get("role_label") or "")
        return request

    def list_editor_requests(self, *, status: str | None = REQUEST_PENDING,
                             limit: int = 100) -> list[dict[str, Any]]:
        """列出申请. ``status=None`` 表示全部; 默认只看待审核, 按提交时间**升序**."""
        wanted = None if status in (None, "", "all") else str(status).strip().lower()
        if wanted is not None and wanted not in REQUEST_STATUSES:
            wanted = REQUEST_PENDING
        try:
            take = max(1, min(int(limit), 1000))
        except (TypeError, ValueError):
            take = 100
        if wanted is None:
            rows = self._query(
                "SELECT * FROM editor_requests ORDER BY created_at, id LIMIT ?", (take,))
        else:
            rows = self._query(
                "SELECT * FROM editor_requests WHERE status = ? ORDER BY created_at, id LIMIT ?",
                (wanted, take))
        out: list[dict[str, Any]] = []
        for row in rows:
            item = self.editor_request_view(row) or {}
            user = self.get_by_id(item.get("user_id"))
            item["username"] = str((user or {}).get("username") or "")
            item["user_role"] = str((user or {}).get("role") or "")
            out.append(item)
        return out

    def editor_request_counts(self) -> dict[str, int]:
        """``{"pending": n, "approved": n, "rejected": n, "all": n}``."""
        counts = {REQUEST_PENDING: 0, REQUEST_APPROVED: 0, REQUEST_REJECTED: 0}
        for row in self._query("SELECT status, COUNT(*) AS n FROM editor_requests GROUP BY status"):
            status = str(row["status"] or "")
            if status in counts:
                counts[status] = int(row["n"])
        counts["all"] = sum(counts.values())
        return counts

    def pending_editor_request_count(self) -> int:
        return self.editor_request_counts()[REQUEST_PENDING]

    def _handle_request(self, identifier: Any, *, approve: bool, actor: Any = None,
                        note: Any = "", now: float | None = None) -> tuple[bool, str]:
        allowed, why = self.require_superuser(actor)
        if not allowed:
            return False, why
        row = self._find_request(identifier)
        if row is None:
            return False, "找不到这条申请"
        if str(row["status"] or "") != REQUEST_PENDING:
            return False, "这条申请已经处理过了"
        user = self.get_by_id(int(row["user_id"]))
        if user is None:
            return False, "申请人账号不存在(可能已被删除)"
        text = str(note or "").strip()
        if len(text) > MAX_NOTE:
            return False, f"备注最长 {MAX_NOTE} 个字符"
        stamp = time.time() if now is None else float(now)
        status = REQUEST_APPROVED if approve else REQUEST_REJECTED
        self._execute(
            "UPDATE editor_requests SET status = ?, handled_by = ?, handled_at = ?, note = ?"
            " WHERE id = ? AND status = ?",
            (status, str(actor or "").strip() or "system", stamp, text, int(row["id"]),
             REQUEST_PENDING),
        )
        if approve and not can_write(user["role"]):
            self._execute(
                "UPDATE users SET role = ? WHERE id = ?", (ROLE_EDITOR, int(user["id"])))
        return True, ""

    def approve_editor(self, identifier: Any, *, actor: Any = None, note: Any = "",
                       now: float | None = None) -> tuple[bool, str]:
        """通过申请: 申请置 ``approved``, 用户角色升到 ``editor``.

        ``identifier``: ``int`` 或"纯数字且没有同名用户"按**申请 id** 处理,
        否则按**用户名**处理(取该用户最新一条申请)。``actor`` 非空必须是超级用户。
        """
        return self._handle_request(identifier, approve=True, actor=actor, note=note, now=now)

    def reject_editor(self, identifier: Any, *, actor: Any = None, note: Any = "",
                      now: float | None = None) -> tuple[bool, str]:
        """拒绝申请(角色不变)."""
        return self._handle_request(identifier, approve=False, actor=actor, note=note, now=now)

    # -- 邮箱验证码存储(供 :mod:`dddb.public.verify` 使用) -------------------

    def issue_verify_code(self, subject: Any, code_hash: str, *, purpose: str = "email",
                          expires_at: float, now: float | None = None) -> int:
        """写入一条验证码记录(同一 subject 的旧未用码先作废). 返回新记录 id."""
        stamp = time.time() if now is None else float(now)
        key = str(subject or "").strip()
        with self._lock:
            self._conn.execute(
                "UPDATE verify_codes SET consumed_at = ? WHERE purpose = ? AND subject = ?"
                " AND consumed_at = 0", (stamp, str(purpose), key),
            )
            cursor = self._conn.execute(
                "INSERT INTO verify_codes(purpose, subject, code_hash, created_at, expires_at)"
                " VALUES (?,?,?,?,?)",
                (str(purpose), key, str(code_hash), stamp, float(expires_at)),
            )
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    def latest_verify_code(self, subject: Any, *, purpose: str = "email") -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM verify_codes WHERE purpose = ? AND subject = ? AND consumed_at = 0"
            " ORDER BY id DESC LIMIT 1", (str(purpose), str(subject or "").strip()),
        )
        if row is None:
            return None
        return {
            "id": int(row["id"]),
            "purpose": str(row["purpose"]),
            "subject": str(row["subject"]),
            "code_hash": str(row["code_hash"]),
            "created_at": float(row["created_at"] or 0.0),
            "expires_at": float(row["expires_at"] or 0.0),
            "attempts": int(row["attempts"] or 0),
            "consumed_at": float(row["consumed_at"] or 0.0),
        }

    def bump_verify_attempts(self, code_id: Any) -> int:
        """尝试次数 +1, 返回新的次数(用于限次判定)."""
        try:
            value = int(code_id)
        except (TypeError, ValueError):
            return 0
        with self._lock:
            self._conn.execute("UPDATE verify_codes SET attempts = attempts + 1 WHERE id = ?", (value,))
            self._conn.commit()
            row = self._conn.execute("SELECT attempts FROM verify_codes WHERE id = ?", (value,)).fetchone()
        return int(row["attempts"]) if row is not None else 0

    def consume_verify_code(self, code_id: Any, *, now: float | None = None) -> bool:
        stamp = time.time() if now is None else float(now)
        try:
            value = int(code_id)
        except (TypeError, ValueError):
            return False
        return self._execute_rowcount(
            "UPDATE verify_codes SET consumed_at = ? WHERE id = ? AND consumed_at = 0",
            (stamp, value),
        ) > 0

    def verify_code_count(self, *, purpose: str = "email") -> int:
        row = self._one(
            "SELECT COUNT(*) AS n FROM verify_codes WHERE purpose = ? AND consumed_at = 0",
            (str(purpose),),
        )
        return int(row["n"]) if row is not None else 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _default_root() -> Path:
    """优先项目根(有 config/ 或 data/ 的那一层), 否则当前目录."""
    candidates = [Path.cwd(), Path(__file__).resolve().parents[2]]
    for cand in candidates:
        if (cand / "config" / "config.toml").exists() or (cand / "data").is_dir():
            return cand
    return Path.cwd()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dddb.public.users",
        description="用户端账号管理(独立数据库 data/public_users.sqlite3)",
    )
    parser.add_argument("--users-db", default="", help="用户库路径(默认 <root>/data/public_users.sqlite3)")
    parser.add_argument("--root", default="", help="项目根目录(默认自动探测)")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="建表 + 迁移(幂等)")

    sub.add_parser("purge-unverified",
                   help="删除注册超过 24 小时仍未验证邮箱的账号")
    p_add = sub.add_parser("add", help="新建账号(密码交互输入, 也支持 --password)")
    p_add.add_argument("username")
    p_add.add_argument("--role", default=ROLE_VIEWER, choices=list(ROLES))
    p_add.add_argument("--password", default="", help="密码(省略则交互式输入, 不建议写在命令行里)")
    p_add.add_argument("--email", default="", help="邮箱(给了但没验证时账号不能登录)")
    p_add.add_argument("--unverified", action="store_true",
                       help="把带邮箱的账号建成'待邮箱验证'状态(默认直接可用)")

    sub.add_parser("list", help="列出账号")

    p_show = sub.add_parser("show", help="查看单个账号(角色/邮箱/状态/申请)")
    p_show.add_argument("username")

    p_request = sub.add_parser("request", help="以普通用户身份提交编辑者申请")
    p_request.add_argument("username")
    p_request.add_argument("--reason", default="")

    p_requests = sub.add_parser("requests", help="列出编辑者申请")
    p_requests.add_argument("--status", default="pending",
                            choices=[*REQUEST_STATUSES, "all"])
    p_requests.add_argument("--limit", type=int, default=100)

    for name, text in (("approve", "通过申请"), ("reject", "拒绝申请")):
        sub_p = sub.add_parser(name, help=text)
        sub_p.add_argument("target", help="用户名或申请 id")
        sub_p.add_argument("--note", default="")
        sub_p.add_argument("--actor", default="", help="操作者用户名(超级用户)")

    p_setrole = sub.add_parser("set-role", help="改角色")
    p_setrole.add_argument("username")
    p_setrole.add_argument("--role", default=ROLE_EDITOR, choices=list(ROLES))
    p_setrole.add_argument("--actor", default="")

    p_promote = sub.add_parser("promote", help="旧命令别名: 提升为编辑者")
    p_promote.add_argument("username")
    p_promote.add_argument("--role", default=ROLE_EDITOR, choices=list(ROLES))

    for name, text in (("disable", "禁用账号"), ("enable", "启用账号")):
        sub_p = sub.add_parser(name, help=text)
        sub_p.add_argument("username")
        sub_p.add_argument("--actor", default="")

    p_verify = sub.add_parser("verify", help="人工把账号标记为'邮箱已验证'(救急)")
    p_verify.add_argument("username")

    p_setemail = sub.add_parser("set-email", help="改邮箱")
    p_setemail.add_argument("username")
    p_setemail.add_argument("email")
    p_setemail.add_argument("--verify", action="store_true", help="同时标记为已验证")

    p_passwd = sub.add_parser("passwd", help="改密码")
    p_passwd.add_argument("username")
    p_passwd.add_argument("--password", default="")

    p_remove = sub.add_parser("remove", help="删账号")
    p_remove.add_argument("username")
    p_remove.add_argument("--actor", default="")

    p_invite = sub.add_parser("invite", help="生成一次性编辑者邀请码")
    p_invite.add_argument("--note", default="")

    sub.add_parser("invites", help="列出邀请码")
    return parser


def _fmt_user(row: Mapping[str, Any]) -> str:
    flags = []
    flags.append("启用" if row.get("is_active", True) else "**禁用**")
    if row.get("email"):
        flags.append(f"邮箱{'已验证' if row.get('email_verified') else '未验证'}")
    last = row.get("last_seen") or row.get("last_login") or 0
    last_text = _time_text(last) if last else "从未"
    return (
        f"{row['id']:>4}  {row['username']:<20} {row['role']:<10} "
        f"{row.get('role_label') or role_label(row['role']):<8}  "
        f"{' '.join(flags):<22} 最后登录 {last_text}"
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    root = Path(args.root).resolve() if args.root else _default_root()
    path = Path(args.users_db) if args.users_db else default_users_path(root)
    store = UserStore(path)
    cmd = args.command or "init"
    try:
        if cmd == "init":
            print(f"用户库就绪: {store.path}")
            print(f"  users 列: {', '.join(store.columns())}")
            print(f"  角色: {', '.join(ROLES)}")
            print(f"  表: {', '.join(store.table_names())}")
            return 0
        if cmd == "purge-unverified":
            removed = store.purge_unverified()
            print(f"已删除未验证邮箱(超过 24 小时)的账号: {removed} 个")
            return 0
        if cmd == "add":
            password = args.password
            if not password:
                password = getpass.getpass("密码: ")
                again = getpass.getpass("再输一次: ")
                if password != again:
                    print("两次输入不一致", file=sys.stderr)
                    return 2
            ok, message = store.create_user(
                args.username, password, role=args.role, email=args.email,
                # 管理员/CLI 建的账号默认直接可用; --unverified 才进验证流程
                email_verified=False if args.unverified else True,
            )
            if not ok:
                print(f"失败: {message}", file=sys.stderr)
                return 2
            user = store.get(args.username) or {}
            print(f"已创建 {user.get('username')} ({user.get('role')})"
                  f"{'  邮箱待验证' if user.get('email') and not user.get('email_verified') else ''}")
            return 0
        if cmd == "list":
            rows = store.list_users()
            if not rows:
                print("(还没有账号)")
            for row in rows:
                print(_fmt_user(row))
            return 0
        if cmd == "show":
            user = store.get(args.username)
            if user is None:
                print("找不到这个用户", file=sys.stderr)
                return 2
            print(f"用户名 : {user['username']}")
            print(f"角色   : {user['role']} ({user['role_label']})")
            print(f"状态   : {'启用' if user['is_active'] else '禁用'}")
            print(f"邮箱   : {user['email'] or '(未设置)'}"
                  f"{' 已验证' if user['email_verified'] else ' 未验证'}")
            print(f"能写专辑: {'是' if user['can_write'] else '否'}   能审核/管用户: {'是' if user['can_review'] else '否'}")
            request = store.get_editor_request(user["username"])
            if request:
                print(f"最近申请: #{request['id']} {request['status_label']} "
                      f"({request['created_at_text']}) 理由={request['reason'] or '(无)'}")
            return 0
        if cmd == "request":
            ok, message = store.request_editor(args.username, reason=args.reason)
            print(message if not ok else f"已受理 {args.username} 的编辑者申请, 等待超级用户审核",
                  file=sys.stderr if not ok else sys.stdout)
            return 0 if ok else 2
        if cmd == "requests":
            counts = store.editor_request_counts()
            print(f"待审核 {counts['pending']} · 已通过 {counts['approved']} ·"
                  f" 已拒绝 {counts['rejected']} · 合计 {counts['all']}")
            rows = store.list_editor_requests(status=args.status, limit=args.limit)
            if not rows:
                print("(没有符合条件的申请)")
            for row in rows:
                print(f"#{row['id']:<4} {row['created_at_text']}  {row['username']:<20}"
                      f" {row['status_label']:<6} 理由={row['reason'] or '(无)'}"
                      f"{'  处理人=' + row['handled_by'] if row['handled_by'] else ''}"
                      f"{'  备注=' + row['note'] if row['note'] else ''}")
            return 0
        if cmd in ("approve", "reject"):
            handler = store.approve_editor if cmd == "approve" else store.reject_editor
            ok, message = handler(args.target, actor=args.actor or None, note=args.note)
            if not ok:
                print(f"失败: {message}", file=sys.stderr)
                return 2
            print(("已通过申请: " if cmd == "approve" else "已拒绝申请: ") + str(args.target))
            return 0
        if cmd == "set-role":
            ok, message = store.set_role(args.username, args.role, actor=args.actor or None)
            print((f"失败: {message}" if not ok else f"{args.username} -> {clean_role(args.role)}"),
                  file=sys.stderr if not ok else sys.stdout)
            return 0 if ok else 2
        if cmd == "promote":
            ok, message = store.set_role(args.username, args.role)
            print((f"失败: {message}" if not ok else f"{args.username} -> {clean_role(args.role)}"),
                  file=sys.stderr if not ok else sys.stdout)
            return 0 if ok else 2
        if cmd in ("disable", "enable"):
            ok, message = store.set_active(args.username, cmd == "enable", actor=args.actor or None)
            print((f"失败: {message}" if not ok
                   else f"{args.username} 已{'启用' if cmd == 'enable' else '禁用'}"),
                  file=sys.stderr if not ok else sys.stdout)
            return 0 if ok else 2
        if cmd == "verify":
            ok, message = store.mark_email_verified(args.username)
            print((f"失败: {message}" if not ok else f"{args.username} 已标记为邮箱已验证"),
                  file=sys.stderr if not ok else sys.stdout)
            return 0 if ok else 2
        if cmd == "set-email":
            ok, message = store.set_email(args.username, args.email, verify=args.verify)
            if ok and not args.verify:
                print(f"{args.username} 邮箱已更新(仍待验证: 验证前该账号不能登录, "
                      f"用 `verify {args.username}` 或 --verify 放行)")
                return 0
            print((f"失败: {message}" if not ok else f"{args.username} 邮箱已更新"),
                  file=sys.stderr if not ok else sys.stdout)
            return 0 if ok else 2
        if cmd == "passwd":
            password = args.password or getpass.getpass("新密码: ")
            ok, message = store.set_password(args.username, password)
            print((f"失败: {message}" if not ok else "密码已更新"),
                  file=sys.stderr if not ok else sys.stdout)
            return 0 if ok else 2
        if cmd == "remove":
            ok, message = store.remove(args.username, actor=args.actor or None)
            print((f"失败: {message}" if not ok else f"已删除 {args.username}"),
                  file=sys.stderr if not ok else sys.stdout)
            return 0 if ok else 2
        if cmd == "invite":
            code = store.create_invite(note=args.note)
            print("一次性编辑者邀请码(注册时填在「邀请码」里即可成为编辑者):")
            print(f"  {code}")
            return 0
        if cmd == "invites":
            rows = store.list_invites()
            if not rows:
                print("(没有邀请码)")
            for row in rows:
                state = f"已用({row['used_by']})" if row["used"] else "未用"
                print(f"{row['code']}  {state}  {row['note']}")
            return 0
    finally:
        store.close()
    return 2  # pragma: no cover - argparse 已挡住


def env_users_path(root: Path | str | None = None) -> Path:
    """``DNH_USERS_DB`` 环境变量优先, 否则 ``<root>/data/public_users.sqlite3``."""
    override = os.environ.get("DNH_USERS_DB", "").strip()
    if override:
        return Path(override).resolve()
    return default_users_path(root or _default_root())


__all__ = [
    "DEFAULT_USERS_FILENAME",
    "ROLE_VIEWER",
    "ROLE_EDITOR",
    "ROLE_SUPERUSER",
    "ROLES",
    "WRITE_ROLES",
    "REVIEW_ROLES",
    "ROLE_RANK",
    "ROLE_LABELS",
    "REQUEST_PENDING",
    "REQUEST_APPROVED",
    "REQUEST_REJECTED",
    "REQUEST_STATUSES",
    "REQUEST_STATUS_LABELS",
    "MAX_REASON",
    "MAX_NOTE",
    "LOGIN_OK",
    "LOGIN_BAD_CREDENTIALS",
    "LOGIN_INACTIVE",
    "LOGIN_UNVERIFIED_EMAIL",
    "USER_EXTRA_COLUMNS",
    "LoginResult",
    "UserStore",
    "UserStoreError",
    "build_parser",
    "can_review",
    "can_write",
    "clean_role",
    "default_users_path",
    "email_problem",
    "email_verification_blocks_login",
    "env_users_path",
    "has_role",
    "hash_password",
    "is_superuser",
    "main",
    "normalize_email",
    "password_problem",
    "request_status_label",
    "role_label",
    "role_rank",
    "username_problem",
    "verify_password",
]


if __name__ == "__main__":  # pragma: no cover - 手动运行才走
    raise SystemExit(main())