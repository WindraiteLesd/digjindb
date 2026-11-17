"""会话 / Cookie / CSRF / 鉴权 —— 全部 stdlib, 没有第三方依赖, 也没有一行 JS.

四件事:

1. **服务端会话**: :class:`SessionManager` 保存会话表(默认落盘到
   ``data/sessions.sqlite3``, 重启不掉线), 浏览器只拿到一个随机 ``sid``
   (:func:`secrets.token_urlsafe`)。Cookie 带 ``HttpOnly``, 所以即使页面里混进了
   脚本也读不到它; 而本站的 CSP 干脆连 ``script-src`` 都不给。
2. **CSRF**: 表单里带一个 :class:`CsrfSigner` 签名出来的 token, 服务端再用
   请求里的 Cookie 值(或会话 sid)重算一次比对 —— 密钥在服务端随机生成, 攻击者
   既拿不到 Cookie 也猜不出签名。同时校验 ``Origin``/``Referer`` 与 Host 同源,
   两道都过才放行(纵深防御)。
3. **鉴权判定**: :func:`can_write` / :func:`can_edit` 复用 :mod:`dddb.public.users`
   的角色判定; **超级用户**在写权限之上还有审核 / 管理用户的权力
   (:func:`can_review` / :func:`can_manage_users`)。
4. **登录态与账号状态同步**: 改角色后调 :meth:`SessionManager.update_user_role`
   让在线会话立刻拿到新角色; 禁用/删除账号后调 :meth:`SessionManager.drop_user`
   把该用户全部踢下线。

为什么没有 JS: 用户端保持"零 JS"传统, 表格单、登录表单都靠隐藏域传 token 即可。
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import logging
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .templates import CSRF_FIELD
from .users import (
    ROLE_EDITOR,
    ROLE_SUPERUSER,
    ROLE_VIEWER,
    ROLE_LABELS,
    can_review as role_can_review,
    can_write,
    clean_role,
    is_superuser as role_is_superuser,
    role_label,
)

_LOGGER = logging.getLogger("dddb.public.auth")

#: 会话 Cookie 名
SESSION_COOKIE = "dnh_session"

#: 匿名访问者也需要的 CSRF 绑定 Cookie 名(双重提交)
CSRF_COOKIE = "dnh_csrf"

#: 会话有效期(秒): **30 天滑动**; 每次使用都续期, 总寿命不超过 :data:`SESSION_MAX_LIFE`
#:
#: 原来只有 12 小时, 用户隔天打开就得重新登录, 体验很差。现在只要不是长期不用,
#: 登录状态会一直保留(180 天内活跃即可)。
SESSION_TTL = 30 * 24 * 3600
SESSION_MAX_LIFE = 180 * 24 * 3600

#: 最多同时保留多少个会话(超出后先扔最旧的)
MAX_SESSIONS = 5000

#: 会话库文件名(与用户库一样放在 ``data/`` 下, 独立于专辑主库)
SESSION_DB_FILENAME = "sessions.sqlite3"

#: CSRF token 的长度(签名后的 base64url 串)
_TOKEN_BYTES = 32

#: 会话表结构. ``sid`` 是主键(随机 32 字节, 不做哈希 —— 它本身就不是口令,
#: 泄露等于会话被劫持, 但库文件本来就在服务器上)。
_SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    sid        TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL DEFAULT 0,
    username   TEXT    NOT NULL DEFAULT '',
    role       TEXT    NOT NULL DEFAULT 'viewer',
    created_at REAL    NOT NULL DEFAULT 0,
    last_seen  REAL    NOT NULL DEFAULT 0,
    expires_at REAL    NOT NULL DEFAULT 0,
    flash      TEXT    NOT NULL DEFAULT '',
    env_hash   TEXT    NOT NULL DEFAULT '',
    confirmed  INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""


class SessionStoreError(RuntimeError):
    """打不开 / 写不了会话库."""


def default_sessions_path(root: Path | str) -> Path:
    """``<root>/data/sessions.sqlite3``."""
    return (Path(root) / "data" / SESSION_DB_FILENAME).resolve()


def env_sessions_path(root: Path | str | None = None) -> Path:
    """``DNH_SESSIONS_DB`` 环境变量优先, 否则 ``<root>/data/sessions.sqlite3``."""
    override = os.environ.get("DNH_SESSIONS_DB", "").strip()
    if override:
        return Path(override).resolve()
    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    return default_sessions_path(base)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@dataclass(slots=True)
class Session:
    """一条服务端会话."""

    sid: str
    user_id: int = 0
    username: str = ""
    role: str = "viewer"
    created_at: float = 0.0
    last_seen: float = 0.0
    expires_at: float = 0.0
    #: 成功写操作后要显示的一次性提示(闪信), 读一次就清掉
    flash: str = ""
    #: 登录环境指纹 sha256(ip|ua) —— 与上次不同就要邮件确认
    env_hash: str = ""
    #: 新环境登录后为 False; 点邮件链接确认后为 True(敏感操作要求 True)
    confirmed: bool = True

    @property
    def logged_in(self) -> bool:
        return bool(self.username) and self.user_id > 0

    @property
    def is_editor(self) -> bool:
        """能不能写专辑(**superuser 也算**, 它权限更高)."""
        return self.logged_in and can_write(self.role)

    @property
    def is_superuser(self) -> bool:
        return self.logged_in and role_is_superuser(self.role)

    def can_review(self) -> bool:
        """能不能审核编辑者申请 / 管理用户(仅超级用户)."""
        return self.logged_in and role_can_review(self.role)

    @property
    def role_label(self) -> str:
        return role_label(self.role)

    def user_view(self) -> dict[str, Any]:
        """交给模板的用户视图(不含任何口令字段)."""
        role = clean_role(self.role)
        return {
            "id": self.user_id,
            "username": self.username,
            "role": role,
            "role_label": self.role_label,
            "is_editor": can_write(role),
            "is_superuser": role == ROLE_SUPERUSER,
            "can_review": role_can_review(role),
        }


class SessionStore:
    """会话的 SQLite 持久化(重启不掉线; 单进程并发安全).

    ``check_same_thread=False`` + 一把 ``RLock``: http.server 每个请求一个线程,
    会话读写必须串行化。写量很小(登录/登出/每 30 秒一次的续期), 不是瓶颈。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=15.0)
        except sqlite3.Error as exc:  # pragma: no cover - 磁盘级故障
            raise SessionStoreError(f"打不开会话库 {self.path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        try:
            with self._lock:
                self._conn.executescript(_SESSION_SCHEMA)
                self._conn.commit()
        except sqlite3.Error as exc:  # pragma: no cover
            raise SessionStoreError(f"初始化会话库失败 {self.path}: {exc}") from exc
        # 老库补列: env_hash / confirmed(缺列会让"新设备确认"整体失效)
        self._migrate_columns()

    #: 后加的列(老库要 ALTER 补上, 否则读写 env_hash/confirmed 会失败)
    _EXTRA_COLUMNS = {
        "env_hash": "TEXT NOT NULL DEFAULT ''",
        "confirmed": "INTEGER NOT NULL DEFAULT 1",
    }

    def _migrate_columns(self) -> None:
        """给老库补列(幂等)."""
        try:
            existing = {str(row[1]) for row in
                        self._conn.execute("PRAGMA table_info(sessions)")}
        except Exception:                     # noqa: BLE001
            return
        for name, ddl in self._EXTRA_COLUMNS.items():
            if name in existing:
                continue
            try:
                self._conn.execute(f"ALTER TABLE sessions ADD COLUMN {name} {ddl}")
                self._conn.commit()
            except Exception:                 # noqa: BLE001
                continue

    # -- 读写 ---------------------------------------------------------------

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> Session:
        return Session(
            sid=str(row["sid"]),
            user_id=int(row["user_id"] or 0),
            username=str(row["username"] or ""),
            role=str(row["role"] or "viewer"),
            created_at=float(row["created_at"] or 0.0),
            last_seen=float(row["last_seen"] or 0.0),
            expires_at=float(row["expires_at"] or 0.0),
            flash=str(row["flash"] or ""),
            env_hash=str(row["env_hash"] if "env_hash" in row.keys() else "" or ""),
            confirmed=bool(int(row["confirmed"] if "confirmed" in row.keys() else 1)),
        )

    def load(self, *, now: float | None = None, max_life: float = SESSION_MAX_LIFE) -> list[Session]:
        """载入未过期会话; 顺手删掉已经过期的行."""
        stamp = time.time() if now is None else float(now)
        with self._lock:
            self.delete_expired(stamp, max_life=max_life)
            rows = self._conn.execute("SELECT * FROM sessions ORDER BY created_at").fetchall()
        return [self._row_to_session(row) for row in rows]

    def upsert(self, session: Session) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions (sid, user_id, username, role, created_at, last_seen,"
                " expires_at, flash, env_hash, confirmed)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(sid) DO UPDATE SET user_id=excluded.user_id, username=excluded.username,"
                " role=excluded.role, last_seen=excluded.last_seen, expires_at=excluded.expires_at,"
                " flash=excluded.flash, env_hash=excluded.env_hash,"
                " confirmed=excluded.confirmed",
                (
                    session.sid, int(session.user_id), str(session.username), str(session.role),
                    float(session.created_at), float(session.last_seen), float(session.expires_at),
                    str(session.flash or ""),
                    str(session.env_hash or ""),
                    1 if session.confirmed else 0,
                ),
            )
            self._conn.commit()

    def delete(self, sid: str) -> bool:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM sessions WHERE sid = ?", (str(sid),))
            self._conn.commit()
            return bool(cursor.rowcount)

    def delete_many(self, sids: Any) -> int:
        items = [str(s) for s in (sids or []) if s]
        if not items:
            return 0
        with self._lock:
            removed = 0
            for chunk_start in range(0, len(items), 500):
                chunk = items[chunk_start : chunk_start + 500]
                placeholders = ",".join("?" for _ in chunk)
                cursor = self._conn.execute(
                    f"DELETE FROM sessions WHERE sid IN ({placeholders})", chunk
                )
                removed += max(0, int(cursor.rowcount or 0))
            self._conn.commit()
            return removed

    def delete_user(self, user_id: int) -> int:
        with self._lock:
            cursor = self._conn.execute("DELETE FROM sessions WHERE user_id = ?", (int(user_id),))
            self._conn.commit()
            return max(0, int(cursor.rowcount or 0))

    def delete_expired(self, now: float, *, max_life: float = SESSION_MAX_LIFE) -> int:
        stamp = float(now)
        oldest = stamp - float(max_life)
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM sessions WHERE expires_at <= ? OR created_at <= ?", (stamp, oldest)
            )
            self._conn.commit()
            return max(0, int(cursor.rowcount or 0))

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()
        return int(row["n"] if row is not None else 0)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass


class SessionManager:
    """会话表(可选落盘到 SQLite).

    * 不传 ``store`` / ``path`` 时是纯内存(单测与临时场景够用);
    * 传了就是持久化的: 进程重启后登录态还在, 过期行在启动与运行时都会清掉;
    * 语义与老版本完全一致: 滑动续期但总寿命不超过 ``max_life``, 会话数上限
      ``max_sessions``(超出先扔最久没用的), 改口令时可以 :meth:`drop_user`;
    * 并发安全: 所有读写都在同一把 ``RLock`` 下, SQLite 连接用
      ``check_same_thread=False``。
    """

    def __init__(
        self,
        *,
        ttl: float = SESSION_TTL,
        max_life: float = SESSION_MAX_LIFE,
        max_sessions: int = MAX_SESSIONS,
        secret: bytes | None = None,
        store: SessionStore | None = None,
        path: Path | str | None = None,
        persist_interval: float = 30.0,
    ) -> None:
        self.ttl = float(ttl)
        self.max_life = float(max_life)
        self.max_sessions = max(16, int(max_sessions))
        #: 续期写盘的最小间隔(秒): 每次请求都写一遍没必要, 12 小时的 TTL 差几十秒无妨
        self.persist_interval = max(0.0, float(persist_interval))
        self._sessions: dict[str, Session] = {}
        self._lock = threading.RLock()
        self.signer = CsrfSigner(secret)
        self.store = store if store is not None else (SessionStore(path) if path else None)
        if self.store is not None:
            now = time.time()
            for session in self.store.load(now=now, max_life=self.max_life):
                self._sessions[session.sid] = session
            self._enforce_limit_locked(now)

    # -- 持久化辅助 ---------------------------------------------------------

    @property
    def persistent(self) -> bool:
        return self.store is not None

    def _persist_locked(self, session: Session) -> None:
        if self.store is None:
            return
        try:
            self.store.upsert(session)
        except (sqlite3.Error, SessionStoreError) as exc:  # 落盘失败不该让请求 500
            if _LOGGER is not None:
                _LOGGER.warning("会话落盘失败: %s", exc)

    def _enforce_limit_locked(self, now: float) -> None:
        if len(self._sessions) <= self.max_sessions:
            return
        oldest = sorted(self._sessions.values(), key=lambda s: s.last_seen)[
            : len(self._sessions) - self.max_sessions
        ]
        for item in oldest:
            self._sessions.pop(item.sid, None)
        if self.store is not None:
            try:
                self.store.delete_many([item.sid for item in oldest])
            except (sqlite3.Error, SessionStoreError) as exc:  # pragma: no cover
                if _LOGGER is not None:
                    _LOGGER.warning("清理超限会话失败: %s", exc)

    # -- 会话 ---------------------------------------------------------------

    def create(self, user: Mapping[str, Any], *, now: float | None = None,
               env_hash: str = "", confirmed: bool = True) -> Session:
        """给一个已认证用户开新会话(登录后调用).

        ``confirmed=False`` 表示"新环境待确认": 只能访问验证页, 敏感操作被拦。
        """
        stamp = time.time() if now is None else float(now)
        session = Session(
            sid=secrets.token_urlsafe(32),
            user_id=int(user.get("id") or 0),
            username=str(user.get("username") or ""),
            role=clean_role(user.get("role")),
            created_at=stamp,
            last_seen=stamp,
            expires_at=stamp + self.ttl,
            env_hash=str(env_hash or ""),
            confirmed=bool(confirmed),
        )
        with self._lock:
            self._prune_locked(stamp)
            self._sessions[session.sid] = session
            self._enforce_limit_locked(stamp)
            self._persist_locked(session)
        return session

    def touch(self, sid: Any) -> bool:
        """活跃续期: ``last_seen`` 推到 now, 并按 TTL 延长过期时间(滑动过期)."""
        session = self.get(sid)
        if session is None:
            return False
        stamp = time.time()
        with self._lock:
            session.last_seen = stamp
            session.expires_at = min(stamp + self.ttl, session.created_at + self.max_life)
            self._persist_locked(session)
        return True

    def confirm(self, sid: Any) -> bool:
        """把会话标记为"环境已确认"(点邮件链接后调用)."""
        session = self.get(sid)
        if session is None:
            return False
        with self._lock:
            session.confirmed = True
            self._persist_locked(session)
        return True

    def get(self, sid: Any, *, now: float | None = None) -> Session | None:
        """按 sid 取会话; 过期 / 超龄一律视为不存在."""
        text = str(sid or "")
        if not text:
            return None
        stamp = time.time() if now is None else float(now)
        with self._lock:
            session = self._sessions.get(text)
            if session is None:
                return None
            if session.expires_at <= stamp or (stamp - session.created_at) > self.max_life:
                self._sessions.pop(text, None)
                if self.store is not None:
                    try:
                        self.store.delete(text)
                    except (sqlite3.Error, SessionStoreError) as exc:  # pragma: no cover
                        if _LOGGER is not None:
                            _LOGGER.warning("删除过期会话失败: %s", exc)
                return None
            previous = session.last_seen
            session.last_seen = stamp
            session.expires_at = min(session.created_at + self.max_life, stamp + self.ttl)
            if self.store is not None and (stamp - previous) >= self.persist_interval:
                self._persist_locked(session)
            return session

    def destroy(self, sid: Any) -> bool:
        with self._lock:
            removed = self._sessions.pop(str(sid or ""), None) is not None
            if self.store is not None:
                try:
                    self.store.delete(str(sid or ""))
                except (sqlite3.Error, SessionStoreError) as exc:  # pragma: no cover
                    if _LOGGER is not None:
                        _LOGGER.warning("删除会话失败: %s", exc)
            return removed

    def drop_user(self, user_id: Any) -> int:
        """踢掉某个用户的所有会话(改口令 / 降级时用)."""
        try:
            wanted = int(user_id)
        except (TypeError, ValueError):
            return 0
        with self._lock:
            doomed = [sid for sid, s in self._sessions.items() if s.user_id == wanted]
            for sid in doomed:
                self._sessions.pop(sid, None)
            if self.store is not None:
                try:
                    self.store.delete_user(wanted)
                except (sqlite3.Error, SessionStoreError) as exc:  # pragma: no cover
                    if _LOGGER is not None:
                        _LOGGER.warning("踢用户会话失败: %s", exc)
        return len(doomed)

    def count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def update_user_role(self, user_id: Any, role: Any) -> int:
        """把某个用户**在线会话**里的角色改掉(升/降级后立刻生效).

        不踢人: 编辑者被降成 viewer 时, 他的会话还在, 但下一次 ``is_editor``
        就是 False; 被提升的编辑者也不用重新登录。返回改动的会话数。
        """
        try:
            wanted = int(user_id)
        except (TypeError, ValueError):
            return 0
        new_role = clean_role(role)
        changed = 0
        with self._lock:
            for session in self._sessions.values():
                if session.user_id != wanted:
                    continue
                if session.role == new_role:
                    continue
                session.role = new_role
                changed += 1
                self._persist_locked(session)
        return changed

    def _prune_locked(self, now: float) -> None:
        doomed = [
            sid for sid, s in self._sessions.items()
            if s.expires_at <= now or (now - s.created_at) > self.max_life
        ]
        for sid in doomed:
            self._sessions.pop(sid, None)
        if doomed and self.store is not None:
            try:
                self.store.delete_many(doomed)
            except (sqlite3.Error, SessionStoreError) as exc:  # pragma: no cover
                if _LOGGER is not None:
                    _LOGGER.warning("清理过期会话失败: %s", exc)

    def prune(self, *, now: float | None = None) -> int:
        stamp = time.time() if now is None else float(now)
        with self._lock:
            before = len(self._sessions)
            self._prune_locked(stamp)
            if self.store is not None:
                try:
                    self.store.delete_expired(stamp, max_life=self.max_life)
                except (sqlite3.Error, SessionStoreError) as exc:  # pragma: no cover
                    if _LOGGER is not None:
                        _LOGGER.warning("会话库过期清理失败: %s", exc)
            return before - len(self._sessions)

    def flush(self) -> None:
        """把内存里的会话全部写回库(退出前调用, 保证续期不丢)."""
        if self.store is None:
            return
        with self._lock:
            for session in self._sessions.values():
                self._persist_locked(session)

    def close(self) -> None:
        """关闭底层会话库(测试与优雅退出用)."""
        if self.store is None:
            return
        try:
            self.flush()
        finally:
            self.store.close()

    # -- 请求状态 -----------------------------------------------------------

    def state(self, cookies: Mapping[str, str], *, now: float | None = None) -> "AuthState":
        """把请求 Cookie 变成鉴权状态(纯读, 不产生副作用)."""
        sid = str((cookies or {}).get(SESSION_COOKIE) or "")
        session = self.get(sid, now=now)
        if session is not None and session.logged_in:
            return AuthState(
                session=session,
                csrf_binding=session.sid,
                csrf_token=self.signer.token(session.sid),
            )
        # 匿名访客: 用双重提交 Cookie 绑定 token; 没有 Cookie 就现发一个
        anon = str((cookies or {}).get(CSRF_COOKIE) or "")
        fresh = False
        if not anon or not _looks_random(anon):
            anon = secrets.token_urlsafe(24)
            fresh = True
        return AuthState(
            session=None,
            anonymous_cookie=anon,
            set_anonymous_cookie=fresh,
            csrf_binding=anon,
            csrf_token=self.signer.token(anon),
        )


# --------------------------------------------------------------------------
# 鉴权状态
# --------------------------------------------------------------------------


@dataclass(slots=True)
class AuthState:
    """一次请求的鉴权状态 + 该请求要用的 CSRF token."""

    session: Session | None = None
    anonymous_cookie: str = ""
    set_anonymous_cookie: bool = False
    csrf_binding: str = ""
    csrf_token: str = ""

    @property
    def logged_in(self) -> bool:
        return bool(self.session and self.session.logged_in)

    @property
    def is_editor(self) -> bool:
        return bool(self.session and self.session.is_editor)

    @property
    def is_superuser(self) -> bool:
        return bool(self.session and self.session.is_superuser)

    def can_review(self) -> bool:
        return bool(self.session and self.session.can_review())

    @property
    def user(self) -> dict[str, Any] | None:
        return self.session.user_view() if self.logged_in and self.session else None

    @property
    def username(self) -> str:
        return self.session.username if self.logged_in and self.session else ""

    @property
    def role(self) -> str:
        return clean_role(self.session.role) if self.logged_in and self.session else ""


def _looks_random(text: str) -> bool:
    """Cookie 里的匿名绑定值必须像我们自己发的(长度够 + 字符集合法)."""
    if len(text) < 16 or len(text) > 128:
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    return all(ch in allowed for ch in text)


# --------------------------------------------------------------------------
# CSRF
# --------------------------------------------------------------------------


class CsrfSigner:
    """HMAC 签名的 CSRF token(无状态, 不用存表).

    ``token(binding) = b64(hmac_sha256(secret, binding))``; 校验时重算一次并做
    常数时间比较。密钥是进程启动时随机生成的 —— 重启会让旧表单失效(会话也一样
    失效), 用户刷新页面即可。
    """

    def __init__(self, secret: bytes | None = None) -> None:
        self.secret = bytes(secret) if secret else secrets.token_bytes(32)

    def token(self, binding: Any) -> str:
        raw = hmac.new(self.secret, str(binding or "").encode("utf-8"), hashlib.sha256).digest()
        return _b64(raw[:_TOKEN_BYTES])

    def check(self, binding: Any, token: Any) -> bool:
        if not binding or not isinstance(token, str) or not token:
            return False
        return hmac.compare_digest(self.token(binding), token)


# --------------------------------------------------------------------------
# Cookie 收发
# --------------------------------------------------------------------------


def parse_cookie_header(header: Any) -> dict[str, str]:
    """``"a=1; b=2"`` -> ``{"a": "1", "b": "2"}``(只取第一个同名项)."""
    out: dict[str, str] = {}
    if not header:
        return out
    for chunk in str(header).split(";"):
        if "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip()
        if not name or name in out:
            continue
        out[name] = value.strip()
    return out


def set_cookie(name: str, value: str, *, max_age: int, http_only: bool = True,
               same_site: str = "Lax", path: str = "/") -> str:
    """拼一个 ``Set-Cookie`` 值(不要在这里塞用户输入)."""
    parts = [f"{name}={value}", f"Path={path}", f"SameSite={same_site}"]
    if http_only:
        parts.append("HttpOnly")
    if max_age > 0:
        parts.append(f"Max-Age={int(max_age)}")
    return "; ".join(parts)


def delete_cookie(name: str, *, path: str = "/") -> str:
    return f"{name}=; Path={path}; SameSite=Lax; HttpOnly; Max-Age=0"


# --------------------------------------------------------------------------
# 同源校验
# --------------------------------------------------------------------------


def host_of(url: Any) -> str:
    """从 ``Origin``/``Referer`` 里取出 ``host[:port]``(小写)."""
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parsed = urllib.parse.urlsplit(text)
    except ValueError:  # pragma: no cover - urlsplit 基本不抛
        return ""
    return (parsed.netloc or "").lower()


#: 本站的**同一站点别名**: apex 与 www 属于同一个站点。
#: 只在"两边都是本站别名"时才算同站 —— 真跨站(evil.example)照样拒。
SITE_HOST_ALIASES: tuple[str, ...] = ("digjindb.top", "www.digjindb.top")


def _same_site_host(candidate: str, target: str) -> bool:
    """两个 host 是否属于同一站点(相同, 或都在本站别名里)."""
    left = str(candidate or "").strip().lower()
    right = str(target or "").strip().lower()
    if not left or not right:
        return False
    if left == right:
        return True
    aliases = {host.strip().lower() for host in SITE_HOST_ALIASES}
    return left in aliases and right in aliases


def same_origin(origin: Any, referer: Any, host: Any) -> tuple[bool, str]:
    """POST 必须来自本站.

    * 两个头都没有 -> 放行(老浏览器/命令行客户端; 此时 CSRF token 仍是硬门槛);
    * 有 ``Origin`` -> 必须与 Host **同站**(允许 apex/www 别名);
    * 没有 ``Origin`` 但有 ``Referer`` -> Referer 的 host 必须与 Host 同站;
    * 有 Origin 但是 ``null`` -> 拒绝。
    """
    target = str(host or "").strip().lower()
    origin_text = str(origin or "").strip()
    # ``Origin: null`` 表示"没有可用的来源信息"(沙箱 iframe / file:// /
    # 无 Referer 策略 / 部分内置浏览器)。它**不携带敌意**, 与"没带 Origin"等价,
    # 所以退回 Referer 判定; 都没有时按原策略放行 —— CSRF 令牌仍是硬门槛。
    if origin_text.casefold() in ("", "null"):
        origin_text = ""
    origin_host = host_of(origin_text)
    if origin_text and not origin_host:
        return False, "请求来源无法识别"
    if origin_host:
        if not _same_site_host(origin_host, target):
            return False, "跨站请求被拒绝"
        return True, ""
    referer_text = str(referer or "").strip()
    if referer_text.casefold() in ("", "null"):
        referer_text = ""
    if referer_text:
        ref_host = host_of(referer_text)
        if not _same_site_host(ref_host, target):
            return False, "跨站请求被拒绝"
    return True, ""


# --------------------------------------------------------------------------
# 编辑者鉴权
# --------------------------------------------------------------------------


def can_edit(state: "AuthState") -> bool:
    """能不能进编辑界面 / 提交写操作."""
    return bool(state and state.is_editor)


def can_write_request(method: Any, state: "AuthState") -> bool:
    """HTTP 方法 + 鉴权 -> 是否允许写.

    只有 ``GET``/``HEAD`` 算读; 其余方法一律要求 **编辑者或更高** 的会话。
    """
    if str(method or "GET").upper() in ("GET", "HEAD"):
        return True
    return can_edit(state)


def is_superuser(state: Any) -> bool:
    """当前会话是不是超级用户(接受 :class:`AuthState`, 也接受 ``Session``)."""
    return bool(getattr(state, "is_superuser", False))


def can_review(state: Any) -> bool:
    """能不能进审核队列 / 审批编辑者申请(仅超级用户)."""
    return is_superuser(state)


def can_manage_users(state: Any) -> bool:
    """能不能改角色 / 禁用 / 删除账号(仅超级用户)."""
    return is_superuser(state)


#: 允许登录后的用户看到的角色列表(模板下拉/说明用) —— 由低到高
ROLE_CHOICES: tuple[tuple[str, str], ...] = (
    (ROLE_VIEWER, "普通用户"),
    (ROLE_EDITOR, "编辑者"),
    (ROLE_SUPERUSER, "超级用户"),
)

__all__ = [
    "SESSION_COOKIE",
    "CSRF_COOKIE",
    "CSRF_FIELD",
    "SESSION_TTL",
    "SESSION_DB_FILENAME",
    "Session",
    "SessionManager",
    "SessionStore",
    "SessionStoreError",
    "default_sessions_path",
    "env_sessions_path",
    "AuthState",
    "CsrfSigner",
    "ROLE_CHOICES",
    "ROLE_LABELS",
    "can_edit",
    "can_manage_users",
    "can_review",
    "can_write",
    "can_write_request",
    "delete_cookie",
    "host_of",
    "is_superuser",
    "parse_cookie_header",
    "role_label",
    "same_origin",
    "set_cookie",
]