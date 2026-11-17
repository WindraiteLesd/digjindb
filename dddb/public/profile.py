"""个人中心的数据存储层(独立 SQLite 文件, 与 archive 库 / users 库都分开).

为什么单独一个库: 个人中心的数据(简介、头像、收藏、关注、私信、通知、反馈、贡献)
都是**用户产生的内容**, 与"专辑档案"和"账号凭据"的写入频率、备份策略、清理策略完全
不同 —— 混在一起会让"备份档案库"顺手带上私信, 让"清理账号"变成跨库事务。

设计约定(与 :mod:`dddb.public.users` 保持一致):

* 只用 stdlib ``sqlite3``; 连接 ``check_same_thread=False`` + 一把 ``RLock``
  (http.server 每请求一个线程, 这些读写都很小, 串行化最省心);
* 建表幂等(``CREATE TABLE IF NOT EXISTS``), 老库/新库都能直接开;
* **一切值都参数化**, 不做字符串拼 SQL;
* 非法参数(不存在的 user_id、空 key、越界长度)**不抛异常** —— 返回
  ``False`` / ``""`` / ``[]`` / ``(False, 原因)``, 页面层直接拿去渲染;
* 时间统一走 ``time.time()``, 但支持注入 ``clock``, 单测里不用 ``sleep``;
* 头像落盘到 ``<db 所在目录>/avatars/<user_id>.<ext>``(即 ``<root>/data/avatars/``),
  新头像**覆盖**旧头像(包括旧扩展名的那张)。

页面层只需要这一个类 + 两个纯函数(:func:`bio_problem` / :func:`avatar_problem`):

::

    from dddb.public.profile import ProfileStore, bio_problem, avatar_problem

    store = ProfileStore(root / "data" / "public_profiles.sqlite3")
    me = store.profile(user.id, user.username)      # 不存在则建
    ok, msg = store.update_profile(user.id, bio=text)
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .users import username_banned_word

logger = logging.getLogger("dddb.public.profile")

#: 个人中心库默认文件名(位于项目 ``data/`` 目录下)
DEFAULT_PROFILES_FILENAME = "public_profiles.sqlite3"
#: 头像子目录名(相对 db 所在目录)
AVATAR_DIRNAME = "avatars"

# --------------------------------------------------------------------------
# 长度 / 类型 / 白名单
# --------------------------------------------------------------------------

#: 简介长度上限(字符数, 不是字节)
BIO_MAX = 500
#: 私信正文 / 主题上限
MESSAGE_MAX = 2000
SUBJECT_MAX = 100
#: 反馈正文上限(另: 联系方式 200 字符)
FEEDBACK_MAX = 2000
CONTACT_MAX = 200
#: 贡献摘要上限
CONTRIBUTION_SUMMARY_MAX = 500

#: 头像大小上限与允许的类型(白名单 -> 落盘扩展名)
AVATAR_MAX_BYTES = 512 * 1024
AVATAR_TYPES: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}

#: 关注对象类型 / 贡献类型
FOLLOW_KINDS: tuple[str, ...] = ("circle", "artist", "event")
CONTRIBUTION_KINDS: tuple[str, ...] = ("edit", "upload", "new_album", "cover")

#: 隐私开关(只允许这几项; 值是 0/1)
PRIVACY_KEYS: tuple[str, ...] = (
    "show_birthday",
    "show_bio",
    "show_favorites",
    "show_follows",
    "show_comments",
    "show_contributions",
)

#: 隐私开关默认值(生日默认不公开, 其余默认公开)
PRIVACY_DEFAULTS: dict[str, int] = {
    # **隐私档位**(0=对所有人公开 1=对站内用户公开 2=仅对关注者公开 3=所有人都不可见)
    "vis_birthday": 3,
    "vis_bio": 0,
    "vis_favorites": 0,
    "vis_follows": 0,
    "vis_comments": 0,
    "vis_contributions": 0,
    "vis_messages": 3,
    "show_birthday": 0,
    "show_bio": 1,
    "show_favorites": 1,
    "show_follows": 1,
    "show_comments": 1,
    "show_contributions": 1,
}

#: 简介里最多允许几个链接
BIO_MAX_LINKS = 5

#: 简介里绝对不允许出现的伪协议(大小写/空白变形都拦)
_BIO_BAD_SCHEMES = ("javascript:", "data:", "vbscript:")

#: 形如 ``scheme://`` 的出现(用于"只允许 http/https")
_BIO_SCHEME_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]{1,15})://")
#: 形如 ``http(s)://...``(统计链接数量)
_BIO_LINK_RE = re.compile(r"(?i)\bhttps?://")
_WS_RE = re.compile(r"[\s\u0000-\u001f\u007f]+")

#: 生日允许两种写法: 完整日期(带年) 或 只到月日(保护隐私)
_BIRTHDAY_FULL_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_BIRTHDAY_MD_RE = re.compile(r"^(\d{2})-(\d{2})$")


# --------------------------------------------------------------------------
# 错误类型
# --------------------------------------------------------------------------


class ProfileStoreError(RuntimeError):
    """个人中心库打不开 / 写不进去(与 :class:`dddb.public.users.UserStoreError` 同一风格)."""


# --------------------------------------------------------------------------
# 纯函数: 简介校验 / 头像校验
# --------------------------------------------------------------------------


#: 隐私档位字段与中文说明(页面单选框用)
VISIBILITY_LABELS = (
    (0, "对所有人公开"),
    (1, "对站内用户公开"),
    (2, "仅对关注的用户公开"),
    (3, "所有人都不可见"),
)
VISIBILITY_ITEMS = (
    ("vis_birthday", "生日"),
    ("vis_bio", "简介"),
    ("vis_favorites", "收藏"),
    ("vis_follows", "关注"),
    ("vis_comments", "评论"),
    ("vis_contributions", "贡献"),
    ("vis_messages", "私信状态"),
)
#: 老库迁移: 这些列是后加的, 打开库时按需 ALTER
VISIBILITY_COLUMNS = {name: ddl for name, ddl in (
    ("vis_birthday", "INTEGER NOT NULL DEFAULT 3"),
    ("vis_bio", "INTEGER NOT NULL DEFAULT 0"),
    ("vis_favorites", "INTEGER NOT NULL DEFAULT 0"),
    ("vis_follows", "INTEGER NOT NULL DEFAULT 0"),
    ("vis_comments", "INTEGER NOT NULL DEFAULT 0"),
    ("vis_contributions", "INTEGER NOT NULL DEFAULT 0"),
    ("vis_messages", "INTEGER NOT NULL DEFAULT 3"),
    # 是否接受他人私信(1 接受 / 0 不接受) —— 替代原来的「私信状态」可见性档位
    ("accept_messages", "INTEGER NOT NULL DEFAULT 1"),
)}


def clean_visibility(value: Any, default: int = 0) -> int:
    """把单选框的输入规整成 0..3; 非法值用默认档."""
    try:
        level = int(str(value).strip() or "-1")
    except (TypeError, ValueError, AttributeError):
        return default
    return level if level in (0, 1, 2, 3) else default


_DEFAULT_VIS = {
    "vis_birthday": 3, "vis_bio": 0, "vis_favorites": 0, "vis_follows": 0,
    "vis_comments": 0, "vis_contributions": 0, "vis_messages": 3,
}


def bio_problem(text: Any) -> str:
    """简介不合规时返回原因(空串 = 合规).

    规则(用户要求"有一定内容审查, 可含自己的外链"):

    1. 长度 ≤ :data:`BIO_MAX`; 不能是"全是空白"(空串是合法的 = 清空简介);
    2. 不能含 ``<`` / ``>``(渲染层只做纯文本 + 自动识别 URL, 不需要也不接受 HTML);
    3. 违规词复用 :func:`dddb.public.users.username_banned_word`;
    4. 出现 ``javascript:`` / ``data:`` / ``vbscript:`` 一律拒;
    5. 形如 ``xxx://`` 的协议只允许 http/https;
    6. http/https 链接最多 :data:`BIO_MAX_LINKS` 个。
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        return "简介必须是文本"
    if len(text) > BIO_MAX:
        return f"简介最长 {BIO_MAX} 个字符"
    if text and not text.strip():
        return "简介不能全是空白"
    if "<" in text or ">" in text:
        return "简介不能包含 HTML 标签字符(< >)"
    banned = username_banned_word(text)
    if banned:
        return f"简介里不能包含违规词汇: {banned}"
    # 抹掉空白/控制字符再查伪协议, 免得 "java\tscript:" 绕过去
    flattened = _WS_RE.sub("", text).casefold()
    for scheme in _BIO_BAD_SCHEMES:
        if scheme in flattened:
            return f"简介里不能包含 {scheme} 这类链接"
    for match in _BIO_SCHEME_RE.finditer(text):
        scheme = match.group(1).lower()
        if scheme not in ("http", "https"):
            return "简介里的链接只允许 http/https"
    if len(_BIO_LINK_RE.findall(text)) > BIO_MAX_LINKS:
        return f"简介里最多 {BIO_MAX_LINKS} 个链接"
    return ""


def avatar_problem(content_type: Any, size: Any) -> str:
    """头像不合规时返回原因(空串 = 合规).

    ``content_type`` 允许带参数(``image/png; charset=binary`` 取前半段);
    大小必须是正整数且 ≤ :data:`AVATAR_MAX_BYTES`。
    """
    base = str(content_type or "").split(";")[0].strip().lower()
    if base not in AVATAR_TYPES:
        allowed = " / ".join(sorted(AVATAR_TYPES))
        return f"头像只支持 {allowed}"
    try:
        length = int(size)
    except (TypeError, ValueError):
        return "头像数据无效"
    if length <= 0:
        return "头像数据为空"
    if length > AVATAR_MAX_BYTES:
        return f"头像最大 {AVATAR_MAX_BYTES // 1024} KB"
    return ""


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _positive_id(value: Any) -> int | None:
    """``user_id`` 规整: 只接受正整数, 其余一律 ``None``(不抛)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None  # 1.5 这种不是合法 id
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _clamp_limit(limit: Any, *, default: int = 100, maximum: int = 1000) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


def default_profiles_path(root: Path | str) -> Path:
    """``<root>/data/public_profiles.sqlite3``."""
    return (Path(root) / "data" / DEFAULT_PROFILES_FILENAME).resolve()


def env_profiles_path(root: Path | str | None = None) -> Path:
    """``DNH_PROFILES_DB`` 环境变量优先, 否则 ``<root>/data/public_profiles.sqlite3``."""
    override = os.environ.get("DNH_PROFILES_DB", "").strip()
    if override:
        return Path(override).resolve()
    base = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    return default_profiles_path(base)


# --------------------------------------------------------------------------
# 表结构
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
  user_id INTEGER PRIMARY KEY,
  username TEXT NOT NULL DEFAULT '',
  birthday TEXT NOT NULL DEFAULT '',
  bio TEXT NOT NULL DEFAULT '',
  avatar TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL DEFAULT 0,
  vis_birthday INTEGER NOT NULL DEFAULT 3,
  vis_bio INTEGER NOT NULL DEFAULT 0,
  vis_favorites INTEGER NOT NULL DEFAULT 0,
  vis_follows INTEGER NOT NULL DEFAULT 0,
  vis_comments INTEGER NOT NULL DEFAULT 0,
  vis_contributions INTEGER NOT NULL DEFAULT 0,
  vis_messages INTEGER NOT NULL DEFAULT 3,
  show_birthday INTEGER NOT NULL DEFAULT 0,
  show_bio INTEGER NOT NULL DEFAULT 1,
  show_favorites INTEGER NOT NULL DEFAULT 1,
  show_follows INTEGER NOT NULL DEFAULT 1,
  show_comments INTEGER NOT NULL DEFAULT 1,
  show_contributions INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS favorites (
  user_id INTEGER NOT NULL,
  album_key TEXT NOT NULL,
  created_at REAL NOT NULL DEFAULT 0,
  UNIQUE(user_id, album_key)
);
CREATE TABLE IF NOT EXISTS follows (
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  key TEXT NOT NULL,
  created_at REAL NOT NULL DEFAULT 0,
  UNIQUE(user_id, kind, key)
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sender_id INTEGER NOT NULL,
  recipient_id INTEGER NOT NULL,
  subject TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL DEFAULT 0,
  read_at REAL NOT NULL DEFAULT 0,
  sender_deleted INTEGER NOT NULL DEFAULT 0,
  recipient_deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  link TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL DEFAULT 0,
  read_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  contact TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'new'
);

-- 编辑者界面布局偏好(卡片大小/顺序). 单独一张表, 不动 users 结构。
CREATE TABLE IF NOT EXISTS user_prefs (
  user_id INTEGER NOT NULL,
  key TEXT NOT NULL,
  value TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS contributions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL DEFAULT '',
  target TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL DEFAULT 0
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_user_prefs_user ON user_prefs(user_id);
CREATE INDEX IF NOT EXISTS idx_follows_user ON follows(user_id, kind);
CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id, created_at);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_contributions_user ON contributions(user_id, created_at);
"""

#: ``profiles`` 表对外暴露的列(顺序即 ``.profile()`` 里 dict 的插入顺序)
#: 隐私档位列(0=所有人 1=站内用户 2=仅关注者 3=不可见); 与 PRIVACY_KEYS 并存
VISIBILITY_KEYS: tuple[str, ...] = (
    "vis_birthday", "vis_bio", "vis_favorites", "vis_follows",
    "vis_comments", "vis_contributions", "vis_messages",
)
#: 新建档案时的档位默认值(与 VISIBILITY_KEYS 一一对应)
VISIBILITY_DEFAULTS: tuple[int, ...] = (3, 0, 0, 0, 0, 0, 3)

PROFILE_COLUMNS: tuple[str, ...] = (
    "user_id", "username", "birthday", "bio", "avatar", "updated_at",
    *PRIVACY_KEYS,
    *VISIBILITY_KEYS,
    "accept_messages",
)


class ProfileStore:
    """个人中心存储(独立 sqlite 文件). 线程安全; 非法参数不抛异常.

    ``clock`` 可注入(默认 :func:`time.time`), 便于单测断言时间戳;
    ``avatar_dir`` 默认是 **db 文件所在目录** 下的 ``avatars/``。
    """

    def __init__(
        self,
        path: Path | str,
        *,
        clock: Any = None,
        avatar_dir: Path | str | None = None,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or time.time
        self.avatar_dir = (
            Path(avatar_dir).resolve() if avatar_dir is not None
            else (self.path.parent / AVATAR_DIRNAME).resolve()
        )
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=15.0)
        except sqlite3.Error as exc:  # pragma: no cover - 磁盘级故障
            raise ProfileStoreError(f"打不开个人中心库 {self.path}: {exc}") from exc
        self._conn.row_factory = sqlite3.Row
        try:
            with self._lock:
                self._conn.executescript(SCHEMA)
                self._conn.commit()
                self._conn.executescript(INDEXES)
                self._conn.commit()
            self.avatar_dir.mkdir(parents=True, exist_ok=True)
            self._migrate_visibility()
        except (sqlite3.Error, OSError) as exc:
            raise ProfileStoreError(f"初始化个人中心库失败 {self.path}: {exc}") from exc

    # -- 低层 ---------------------------------------------------------------

    def _now(self) -> float:
        return float(self._clock())

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def _one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._lock:
            cursor = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return max(0, int(cursor.rowcount or 0))

    def _insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._lock:
            cursor = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:  # pragma: no cover
                pass

    def table_names(self) -> list[str]:
        """库里的表名(排序后; 便于运维/测试确认建表). **不含** sqlite 内部表."""
        return sorted(
            str(row["name"]) for row in self._query(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
            if not str(row["name"]).startswith("sqlite_")
        )

    def profile_columns(self) -> list[str]:
        return [str(row["name"]) for row in self._query("PRAGMA table_info(profiles)")]

    # -- profile ------------------------------------------------------------

    @staticmethod
    def _empty_profile(username: Any = "") -> dict[str, Any]:
        """非法 user_id 时给的"空档案"(不落库, 页面照常渲染)."""
        data: dict[str, Any] = {
            "user_id": 0,
            "username": str(username or ""),
            "birthday": "",
            "bio": "",
            "avatar": "",
            "updated_at": 0.0,
        }
        data.update({key: int(PRIVACY_DEFAULTS[key]) for key in PRIVACY_KEYS})
        # 隐私档位也要带上默认值(页面单选框直接读这些键)
        data.update({key: int(value) for key, value in
                     zip(VISIBILITY_KEYS, VISIBILITY_DEFAULTS)})
        data["accept_messages"] = 1     # 默认接受他人私信
        return data

    @staticmethod
    def _profile_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        data: dict[str, Any] = {
            "user_id": int(row["user_id"]),
            "username": str(row["username"] or ""),
            "birthday": str(row["birthday"] or ""),
            "bio": str(row["bio"] or ""),
            "avatar": str(row["avatar"] or ""),
            "updated_at": float(row["updated_at"] or 0.0),
        }
        for key in PRIVACY_KEYS:
            try:
                data[key] = int(row[key] or 0)
            except (IndexError, KeyError):  # pragma: no cover - 老库缺列兜底
                data[key] = int(PRIVACY_DEFAULTS[key])
        try:
            data["accept_messages"] = int(row["accept_messages"] if "accept_messages"
                                          in row.keys() else 1)
        except (IndexError, KeyError):
            data["accept_messages"] = 1
        return data

    def profile(self, user_id: int, username: str) -> dict[str, Any]:
        """取个人中心档案; **不存在则建**(get-or-create).

        传入的 ``username`` 会顺手对齐库里的名字(用户改名后下次访问就更新了)。
        非法 ``user_id`` 不抛异常, 返回一份空档案(``user_id=0``, 不落库)。
        """
        number = _positive_id(user_id)
        name = _text(username)
        if number is None:
            return self._empty_profile(name)
        row = self._one("SELECT * FROM profiles WHERE user_id = ?", (number,))
        if row is None:
            stamp = self._now()
            columns = ", ".join(PROFILE_COLUMNS)
            placeholders = ", ".join("?" for _ in PROFILE_COLUMNS)
            values: list[Any] = [number, name, "", "", "", stamp]
            values.extend(int(PRIVACY_DEFAULTS[key]) for key in PRIVACY_KEYS)
            values.extend(int(item) for item in VISIBILITY_DEFAULTS)
            values.append(1)                      # accept_messages 默认接受
            try:
                self._execute(
                    f"INSERT INTO profiles ({columns}) VALUES ({placeholders})", values,
                )
            except sqlite3.IntegrityError:  # pragma: no cover - 并发首建: 让别人先插
                pass
            row = self._one("SELECT * FROM profiles WHERE user_id = ?", (number,))
            data = self._profile_dict(row) or self._empty_profile(name)
            data["username"] = name
            return data
        if name and str(row["username"] or "") != name:
            # 用户改名 -> 档案里的展示名跟着走(不必等页面再传一次)
            self._execute(
                "UPDATE profiles SET username = ? WHERE user_id = ?", (name, number))
        data = self._profile_dict(row) or self._empty_profile(name)
        data["username"] = name or data["username"]
        return data

    def _ensure_profile(self, number: int, *, username: str = "") -> dict[str, Any]:
        return self.profile(number, username)

    def update_profile(
        self,
        user_id: int,
        *,
        birthday: Any = None,
        bio: Any = None,
    ) -> tuple[bool, str]:
        """改档案(``None`` = 这一项不动; 空串 = 清空).

        返回 ``(是否保存, 给用户看的原因)``。生日接受 ``YYYY-MM-DD`` 或 ``MM-DD``。
        """
        number = _positive_id(user_id)
        if number is None:
            return False, "无效的用户 id"
        self._ensure_profile(number)
        updates: dict[str, Any] = {}
        if birthday is not None:
            problem = self.birthday_problem(birthday)
            if problem:
                return False, problem
            updates["birthday"] = _text(birthday)
        if bio is not None:
            problem = bio_problem(bio)
            if problem:
                return False, problem
            updates["bio"] = str(bio)
        if not updates:
            return True, ""
        updates["updated_at"] = self._now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        try:
            self._execute(
                f"UPDATE profiles SET {assignments} WHERE user_id = ?",
                [*updates.values(), number],
            )
        except sqlite3.Error as exc:  # pragma: no cover - 磁盘故障
            logger.warning("保存档案失败(%s): %s", number, exc)
            return False, f"保存失败: {exc}"
        return True, ""

    @staticmethod
    def birthday_problem(birthday: Any) -> str:
        """生日校验: 空串(清空) / ``YYYY-MM-DD`` / ``MM-DD``, 必须是真实日期."""
        text = _text(birthday)
        if not text:
            return ""
        match = _BIRTHDAY_FULL_RE.match(text)
        if match:
            year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
            if not (1900 <= year <= time.localtime().tm_year):
                return "生日的年份不合理"
            if not _valid_month_day(month, day, year=year):
                return "生日不是有效日期"
            return ""
        match = _BIRTHDAY_MD_RE.match(text)
        if match:
            month, day = int(match.group(1)), int(match.group(2))
            if not _valid_month_day(month, day, year=None):
                return "生日不是有效日期(MM-DD)"
            return ""
        return "生日格式应为 YYYY-MM-DD 或 MM-DD"

    def _migrate_visibility(self) -> None:
        """老库补 vis_* 列(幂等)."""
        existing = {str(row["name"]) for row in
                    self._query("PRAGMA table_info(profiles)")}
        for name, ddl in VISIBILITY_COLUMNS.items():
            if name in existing:
                continue
            try:
                self._execute(f"ALTER TABLE profiles ADD COLUMN {name} {ddl}")
            except sqlite3.Error as exc:     # pragma: no cover - 并发迁移
                logger.warning("补列 %s 失败: %s", name, exc)
        # 老数据语义: 之前 show_bio=0 表示"不公开" -> 档位 3(所有人不可见)
        self._execute("UPDATE profiles SET vis_bio = 1 WHERE vis_bio = 0 AND show_bio = 0")
        with self._lock:
            self._conn.commit()

    def visibility(self, user_id: Any) -> dict[str, int]:
        """读一个人的隐私档位(缺失的用默认值补齐).

        直接查 ``vis_*`` 列: ``profile()`` 的字典是按早期列清单拼的, 不含后加的档位列。
        """
        try:
            uid = int(user_id or 0)
        except (TypeError, ValueError):
            return dict(_DEFAULT_VIS)
        row = self._one("SELECT * FROM profiles WHERE user_id = ?", (uid,)) if uid > 0 else None
        out = dict(_DEFAULT_VIS)
        if row is not None:
            keys = set(row.keys())
            for name in VISIBILITY_KEYS:
                if name in keys and row[name] is not None:
                    try:
                        out[name] = int(row[name])
                    except (TypeError, ValueError):
                        pass
        return out

    # -- 是否接受他人私信 --------------------------------------------------

    def get_pref(self, user_id: int, key: str, default: str = "") -> str:
        """读一条布局偏好(编辑界面卡片大小/顺序等)."""
        number = _positive_id(user_id)
        name = _text(key)
        if number is None or not name:
            return default
        row = self._one(
            "SELECT value FROM user_prefs WHERE user_id = ? AND key = ?",
            (number, name),
        )
        if row is None:
            return default
        value = str(row["value"] or "")
        return value if value else default

    def set_pref(self, user_id: int, key: str, value: str) -> bool:
        """写一条布局偏好(幂等 upsert)."""
        number = _positive_id(user_id)
        name = _text(key)
        if number is None or not name or len(name) > 64:
            return False
        payload = _text(value)
        if len(payload) > 4000:
            return False
        try:
            self._execute(
                "INSERT INTO user_prefs(user_id, key, value, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET "
                "value = excluded.value, updated_at = excluded.updated_at",
                (number, name, payload, self._now()),
            )
        except sqlite3.Error:
            return False
        return True

    def get_prefs(self, user_id: int, prefix: str = "") -> dict[str, str]:
        """读一批偏好(按 key 前缀过滤)."""
        number = _positive_id(user_id)
        if number is None:
            return {}
        if prefix:
            rows = self._query(
                "SELECT key, value FROM user_prefs "
                "WHERE user_id = ? AND key LIKE ?",
                (number, _text(prefix) + "%"),
            )
        else:
            rows = self._query(
                "SELECT key, value FROM user_prefs WHERE user_id = ?", (number,))
        return {str(r["key"]): str(r["value"] or "") for r in rows}

    def accepts_messages(self, user_id: Any) -> bool:
        """对方是否愿意接收私信(默认 True; 没有档案也按 True)."""
        try:
            uid = int(user_id or 0)
        except (TypeError, ValueError):
            return True
        row = self._one(
            "SELECT accept_messages FROM profiles WHERE user_id = ?", (uid,))
        if row is None:
            return True
        keys = row.keys() if hasattr(row, "keys") else ()
        return bool(int(row["accept_messages"] if "accept_messages" in keys else 1))

    def set_accept_messages(self, user_id: Any, value: Any) -> tuple[bool, str]:
        try:
            uid = int(user_id or 0)
        except (TypeError, ValueError):
            return False, "用户编号不对"
        if uid <= 0:
            return False, "用户编号不对"
        self.profile(uid, "")            # 确保档案行存在(不存在会建)
        flag = 1 if str(value).strip() in ("1", "true", "on", "yes", "接受") else 0
        self._execute("UPDATE profiles SET accept_messages = ? WHERE user_id = ?", (flag, uid))
        self._touch(uid) if hasattr(self, "_touch") else None
        return True, ""

    def set_visibility(self, user_id: Any, levels: Mapping[str, Any]) -> tuple[bool, str]:
        """按单选框结果保存 0..3 档位(只认 VISIBILITY_COLUMNS 里的键)."""
        allowed = {name for name, _ in VISIBILITY_ITEMS}
        updates: dict[str, int] = {}
        for name, value in (levels or {}).items():
            if name in allowed:
                updates[name] = clean_visibility(value, _DEFAULT_VIS.get(name, 0))
        if not updates:
            return False, "没有可保存的隐私设置"
        columns = ", ".join(f"{name} = ?" for name in updates)
        self._execute(f"UPDATE profiles SET {columns} WHERE user_id = ?",
                      (*updates.values(), int(user_id or 0)))
        return True, ""

    def set_privacy(self, user_id: int, **flags: Any) -> tuple[bool, str]:
        """改隐私开关. **只接受** :data:`PRIVACY_KEYS` 里的键; 值转成 0/1."""
        number = _positive_id(user_id)
        if number is None:
            return False, "无效的用户 id"
        unknown = [key for key in flags if key not in PRIVACY_KEYS]
        if unknown:
            return False, "不支持的隐私项: " + ", ".join(sorted(unknown))
        if not flags:
            return True, ""
        self._ensure_profile(number)
        values = {key: 1 if _truthy(value) else 0 for key, value in flags.items()}
        assignments = ", ".join(f"{key} = ?" for key in values)
        try:
            self._execute(
                f"UPDATE profiles SET {assignments}, updated_at = ? WHERE user_id = ?",
                [*values.values(), self._now(), number],
            )
        except sqlite3.Error as exc:  # pragma: no cover
            logger.warning("保存隐私设置失败(%s): %s", number, exc)
            return False, f"保存失败: {exc}"
        return True, ""

    # -- 头像 ---------------------------------------------------------------

    def save_avatar(
        self,
        user_id: int,
        data: bytes,
        content_type: str,
    ) -> tuple[bool, str, str]:
        """保存头像. 返回 ``(ok, 原因, 相对路径)``; 失败时 rel 为空串.

        * 只接受 :data:`AVATAR_TYPES` 白名单里的类型; 超 :data:`AVATAR_MAX_BYTES` 拒绝;
        * 落盘 ``avatars/<user_id>.<ext>``, **覆盖旧头像**(含旧扩展名的那张);
        * 成功时把相对路径写进 ``profiles.avatar``。
        """
        number = _positive_id(user_id)
        if number is None:
            return False, "无效的用户 id", ""
        payload = data if isinstance(data, (bytes, bytearray)) else None
        if payload is None:
            return False, "头像数据无效", ""
        problem = avatar_problem(content_type, len(payload))
        if problem:
            return False, problem, ""
        base = str(content_type or "").split(";")[0].strip().lower()
        extension = AVATAR_TYPES[base]
        rel = f"{AVATAR_DIRNAME}/{number}{extension}"
        target = self.avatar_dir / f"{number}{extension}"
        tmp = target.with_name(target.name + ".tmp")
        try:
            self.avatar_dir.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(bytes(payload))
            os.replace(tmp, target)
        except OSError as exc:  # pragma: no cover - 磁盘/权限问题
            logger.warning("写入头像失败(%s): %s", number, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False, f"保存头像失败: {exc}", ""
        # 旧头像可能是另一种扩展名 -> 清掉, 免得一个用户留两张
        for other in AVATAR_TYPES.values():
            if other == extension:
                continue
            stale = self.avatar_dir / f"{number}{other}"
            try:
                stale.unlink(missing_ok=True)
            except OSError:  # pragma: no cover
                logger.warning("清理旧头像失败: %s", stale)
        self._ensure_profile(number)
        try:
            self._execute(
                "UPDATE profiles SET avatar = ?, updated_at = ? WHERE user_id = ?",
                (rel, self._now(), number),
            )
        except sqlite3.Error as exc:  # pragma: no cover
            logger.warning("记录头像路径失败(%s): %s", number, exc)
            return False, f"保存头像失败: {exc}", ""
        return True, "", rel

    def avatar_path(self, user_id: int) -> str:
        """头像的**绝对路径**; 没有头像/文件不在/非法 id -> 空串."""
        number = _positive_id(user_id)
        if number is None:
            return ""
        row = self._one("SELECT avatar FROM profiles WHERE user_id = ?", (number,))
        if row is None:
            return ""
        rel = _text(row["avatar"])
        if not rel:
            return ""
        candidate = Path(rel)
        if not candidate.is_absolute():
            candidate = self.path.parent / candidate
        try:
            resolved = candidate.resolve()
        except OSError:  # pragma: no cover
            return ""
        # 只发头像目录里的文件(路径是我们自己拼的, 这里再兜一层)
        try:
            if not resolved.is_relative_to(self.avatar_dir):
                logger.warning("拒绝头像目录之外的路径: %s", resolved)
                return ""
        except (ValueError, OSError):  # pragma: no cover
            return ""
        return str(resolved) if resolved.is_file() else ""

    # -- 收藏 ---------------------------------------------------------------

    def toggle_favorite(self, user_id: int, album_key: str) -> bool:
        """收藏/取消收藏, 返回"操作之后是否已收藏". 非法参数返回 ``False``."""
        number = _positive_id(user_id)
        key = _text(album_key)
        if number is None or not key or len(key) > 512:
            return False
        existing = self._one(
            "SELECT 1 FROM favorites WHERE user_id = ? AND album_key = ?", (number, key))
        if existing is not None:
            self._execute(
                "DELETE FROM favorites WHERE user_id = ? AND album_key = ?", (number, key))
            return False
        try:
            self._execute(
                "INSERT INTO favorites(user_id, album_key, created_at) VALUES (?,?,?)",
                (number, key, self._now()),
            )
        except sqlite3.IntegrityError:  # pragma: no cover - 并发双写
            return True
        return True

    def is_favorite(self, user_id: int, album_key: str) -> bool:
        number = _positive_id(user_id)
        key = _text(album_key)
        if number is None or not key:
            return False
        return self._one(
            "SELECT 1 FROM favorites WHERE user_id = ? AND album_key = ?", (number, key)
        ) is not None

    def list_favorites(self, user_id: int, limit: int = 200) -> list[dict[str, Any]]:
        number = _positive_id(user_id)
        if number is None:
            return []
        take = _clamp_limit(limit, default=200)
        rows = self._query(
            "SELECT album_key, created_at FROM favorites WHERE user_id = ?"
            " ORDER BY created_at DESC, album_key LIMIT ?", (number, take),
        )
        return [
            {"album_key": str(row["album_key"]), "created_at": float(row["created_at"] or 0.0)}
            for row in rows
        ]

    # -- 关注 ---------------------------------------------------------------

    @staticmethod
    def _follow_kind(kind: Any) -> str:
        text = _text(kind).lower()
        return text if text in FOLLOW_KINDS else ""

    def toggle_follow(self, user_id: int, kind: str, key: str) -> bool:
        """关注/取关社团/艺人/展会. kind 非法或参数无效 -> ``False``."""
        number = _positive_id(user_id)
        wanted = self._follow_kind(kind)
        target = _text(key)
        if number is None or not wanted or not target or len(target) > 512:
            return False
        existing = self._one(
            "SELECT 1 FROM follows WHERE user_id = ? AND kind = ? AND key = ?",
            (number, wanted, target),
        )
        if existing is not None:
            self._execute(
                "DELETE FROM follows WHERE user_id = ? AND kind = ? AND key = ?",
                (number, wanted, target),
            )
            return False
        try:
            self._execute(
                "INSERT INTO follows(user_id, kind, key, created_at) VALUES (?,?,?,?)",
                (number, wanted, target, self._now()),
            )
        except sqlite3.IntegrityError:  # pragma: no cover
            return True
        return True

    def is_following(self, user_id: int, kind: str, key: str) -> bool:
        number = _positive_id(user_id)
        wanted = self._follow_kind(kind)
        target = _text(key)
        if number is None or not wanted or not target:
            return False
        return self._one(
            "SELECT 1 FROM follows WHERE user_id = ? AND kind = ? AND key = ?",
            (number, wanted, target),
        ) is not None

    def followers_count(self, kind: Any, key: Any) -> int:
        """某个社团/艺人被多少人关注."""
        kind_text = self._follow_kind(kind)
        key_text = str(key or "").strip()
        if not kind_text or not key_text:
            return 0
        row = self._one(
            "SELECT COUNT(*) AS c FROM follows WHERE kind = ? AND key = ? COLLATE NOCASE",
            (kind_text, key_text))
        return int(row["c"] or 0) if row is not None else 0

    def list_follows(self, user_id: int, *, kind: str | None = None) -> list[dict[str, Any]]:
        number = _positive_id(user_id)
        if number is None:
            return []
        if kind is None:
            rows = self._query(
                "SELECT kind, key, created_at FROM follows WHERE user_id = ?"
                " ORDER BY created_at DESC, kind, key", (number,),
            )
        else:
            wanted = self._follow_kind(kind)
            if not wanted:
                return []
            rows = self._query(
                "SELECT kind, key, created_at FROM follows WHERE user_id = ? AND kind = ?"
                " ORDER BY created_at DESC, key", (number, wanted),
            )
        return [
            {
                "kind": str(row["kind"]),
                "key": str(row["key"]),
                "created_at": float(row["created_at"] or 0.0),
            }
            for row in rows
        ]

    # -- 私信 ---------------------------------------------------------------

    def _username_of(self, user_id: int) -> str:
        """从 profiles 里取展示名(对方没进过个人中心就是空串)."""
        row = self._one("SELECT username FROM profiles WHERE user_id = ?", (int(user_id),))
        return _text(row["username"]) if row is not None else ""

    def list_thread(self, user_id: Any, peer_id: Any, *, limit: int = 200) -> list[dict[str, Any]]:
        """两个用户之间的**全部私信**(双向, 时间正序), 用于会话视图."""
        try:
            me, peer = int(user_id or 0), int(peer_id or 0)
        except (TypeError, ValueError):
            return []
        if me <= 0 or peer <= 0 or me == peer:
            return []
        rows = self._query(
            "SELECT * FROM messages WHERE "
            "((sender_id = ? AND recipient_id = ? AND sender_deleted = 0)"
            " OR (sender_id = ? AND recipient_id = ? AND recipient_deleted = 0))"
            " ORDER BY created_at ASC, id ASC LIMIT ?",
            (me, peer, peer, me, max(1, int(limit))),
        )
        out = []
        for row in rows:
            out.append({
                "id": int(row["id"]),
                "direction": "out" if int(row["sender_id"]) == me else "in",
                "subject": str(row["subject"] or ""),
                "body": str(row["body"] or ""),
                "created_at": float(row["created_at"] or 0.0),
                "read": bool(int(row["read_at"] or 0)),
            })
        return out

    def send_message(
        self,
        sender_id: int,
        recipient_id: int,
        subject: str,
        body: str,
    ) -> tuple[bool, str]:
        """发私信. 返回 ``(是否发出, 原因)``; 非法 id / 空正文 / 超长都只返回原因."""
        sender = _positive_id(sender_id)
        recipient = _positive_id(recipient_id)
        if sender is None:
            return False, "无效的发送者 id"
        if recipient is None:
            return False, "无效的收件人 id"
        if sender == recipient:
            return False, "不能给自己发私信"
        text = str(body if body is not None else "")
        if not text.strip():
            return False, "私信内容不能为空"
        if len(text) > MESSAGE_MAX:
            return False, f"私信最长 {MESSAGE_MAX} 个字符"
        title = _text(subject)
        if len(title) > SUBJECT_MAX:
            return False, f"私信标题最长 {SUBJECT_MAX} 个字符"
        try:
            self._insert(
                "INSERT INTO messages(sender_id, recipient_id, subject, body, created_at)"
                " VALUES (?,?,?,?,?)",
                (sender, recipient, title, text, self._now()),
            )
        except sqlite3.Error as exc:  # pragma: no cover
            logger.warning("发私信失败(%s->%s): %s", sender, recipient, exc)
            return False, f"发送失败: {exc}"
        return True, ""

    @staticmethod
    def _box(value: Any) -> str:
        text = _text(value).lower()
        if text in ("outbox", "out", "sent", "sentbox"):
            return "outbox"
        return "inbox"

    def list_messages(self, user_id: int, *, box: str = "inbox",
                      limit: int = 100) -> list[dict[str, Any]]:
        """列出收件箱 / 发件箱. 每项含 ``direction``(``in``/``out``)与 ``read``."""
        number = _positive_id(user_id)
        if number is None:
            return []
        take = _clamp_limit(limit)
        if self._box(box) == "outbox":
            rows = self._query(
                "SELECT * FROM messages WHERE sender_id = ? AND sender_deleted = 0"
                " ORDER BY created_at DESC, id DESC LIMIT ?", (number, take),
            )
            direction, peer_field = "out", "recipient_id"
        else:
            rows = self._query(
                "SELECT * FROM messages WHERE recipient_id = ? AND recipient_deleted = 0"
                " ORDER BY created_at DESC, id DESC LIMIT ?", (number, take),
            )
            direction, peer_field = "in", "sender_id"
        out: list[dict[str, Any]] = []
        names: dict[int, str] = {}
        for row in rows:
            peer = int(row[peer_field] or 0)
            if peer not in names:
                names[peer] = self._username_of(peer)
            read_at = float(row["read_at"] or 0.0)
            out.append({
                "id": int(row["id"]),
                "direction": direction,
                "peer_id": peer,
                "peer_name": names[peer],
                "subject": str(row["subject"] or ""),
                "body": str(row["body"] or ""),
                "created_at": float(row["created_at"] or 0.0),
                "read": read_at > 0,
            })
        return out

    def mark_messages_read(self, user_id: int, ids: Iterable[Any] | None = None) -> int:
        """把收件箱里的私信标为已读; ``ids=None`` = 全部. 返回改动条数."""
        number = _positive_id(user_id)
        if number is None:
            return 0
        stamp = self._now()
        if ids is None:
            return self._execute(
                "UPDATE messages SET read_at = ? WHERE recipient_id = ? AND read_at = 0",
                (stamp, number),
            )
        wanted: list[int] = []
        for value in ids:
            item = _positive_id(value)
            if item is not None:
                wanted.append(item)
        if not wanted:
            return 0
        updated = 0
        # 分批 IN, 避免 SQLite 参数上限
        for start in range(0, len(wanted), 400):
            chunk = wanted[start:start + 400]
            placeholders = ",".join("?" for _ in chunk)
            updated += self._execute(
                f"UPDATE messages SET read_at = ? WHERE recipient_id = ? AND read_at = 0"
                f" AND id IN ({placeholders})", [stamp, number, *chunk],
            )
        return updated

    def unread_message_count(self, user_id: int) -> int:
        """收件箱未读数(页面小红点用)."""
        number = _positive_id(user_id)
        if number is None:
            return 0
        row = self._one(
            "SELECT COUNT(*) AS n FROM messages WHERE recipient_id = ? AND read_at = 0"
            " AND recipient_deleted = 0", (number,),
        )
        return int(row["n"]) if row is not None else 0

    # -- 通知 ---------------------------------------------------------------

    def notify(self, user_id: int, kind: str, text: str, link: str = "") -> int:
        """给用户塞一条站内通知. 返回新 id; 参数无效返回 ``0``."""
        number = _positive_id(user_id)
        if number is None:
            return 0
        body = str(text if text is not None else "")
        if not body.strip():
            return 0
        try:
            return self._insert(
                "INSERT INTO notifications(user_id, kind, text, link, created_at)"
                " VALUES (?,?,?,?,?)",
                (number, _text(kind), body, _text(link), self._now()),
            )
        except sqlite3.Error as exc:  # pragma: no cover
            logger.warning("写通知失败(%s): %s", number, exc)
            return 0

    def list_notifications(self, user_id: int, limit: int = 100) -> list[dict[str, Any]]:
        number = _positive_id(user_id)
        if number is None:
            return []
        take = _clamp_limit(limit)
        rows = self._query(
            "SELECT * FROM notifications WHERE user_id = ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?", (number, take),
        )
        return [
            {
                "id": int(row["id"]),
                "kind": str(row["kind"] or ""),
                "text": str(row["text"] or ""),
                "link": str(row["link"] or ""),
                "created_at": float(row["created_at"] or 0.0),
                "read": float(row["read_at"] or 0.0) > 0,
            }
            for row in rows
        ]

    def mark_notifications_read(self, user_id: int) -> int:
        number = _positive_id(user_id)
        if number is None:
            return 0
        return self._execute(
            "UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at = 0",
            (self._now(), number),
        )

    def unread_notification_count(self, user_id: int) -> int:
        """未读通知数(页面小红点用)."""
        number = _positive_id(user_id)
        if number is None:
            return 0
        row = self._one(
            "SELECT COUNT(*) AS n FROM notifications WHERE user_id = ? AND read_at = 0",
            (number,),
        )
        return int(row["n"]) if row is not None else 0

    # -- 反馈 ---------------------------------------------------------------

    def add_feedback(self, user_id: int, body: str, contact: str = "") -> tuple[bool, str]:
        """提交一条反馈. 返回 ``(是否受理, 原因)``; 正文必填且 ≤ :data:`FEEDBACK_MAX`."""
        number = _positive_id(user_id)
        if number is None:
            return False, "无效的用户 id"
        text = str(body if body is not None else "")
        if not text.strip():
            return False, "反馈内容不能为空"
        if len(text) > FEEDBACK_MAX:
            return False, f"反馈最长 {FEEDBACK_MAX} 个字符"
        way = _text(contact)
        if len(way) > CONTACT_MAX:
            return False, f"联系方式最长 {CONTACT_MAX} 个字符"
        try:
            self._insert(
                "INSERT INTO feedback(user_id, body, contact, created_at) VALUES (?,?,?,?)",
                (number, text, way, self._now()),
            )
        except sqlite3.Error as exc:  # pragma: no cover
            logger.warning("写反馈失败(%s): %s", number, exc)
            return False, f"提交失败: {exc}"
        return True, ""

    def list_feedback(self, user_id: int, limit: int = 100) -> list[dict[str, Any]]:
        number = _positive_id(user_id)
        if number is None:
            return []
        take = _clamp_limit(limit)
        rows = self._query(
            "SELECT * FROM feedback WHERE user_id = ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?", (number, take),
        )
        return [
            {
                "id": int(row["id"]),
                "body": str(row["body"] or ""),
                "contact": str(row["contact"] or ""),
                "created_at": float(row["created_at"] or 0.0),
                "status": str(row["status"] or "new"),
            }
            for row in rows
        ]

    # -- 贡献 ---------------------------------------------------------------

    def add_contribution(
        self,
        user_id: int,
        kind: str,
        target: str,
        summary: str = "",
    ) -> int:
        """记一条贡献(编辑/上传/新建专辑/补封面). 返回新 id; 参数无效 ``0``."""
        number = _positive_id(user_id)
        wanted = _text(kind).lower()
        goal = _text(target)
        if number is None or wanted not in CONTRIBUTION_KINDS or not goal:
            return 0
        text = _text(summary)
        if len(text) > CONTRIBUTION_SUMMARY_MAX:
            text = text[:CONTRIBUTION_SUMMARY_MAX]
        try:
            return self._insert(
                "INSERT INTO contributions(user_id, kind, target, summary, created_at)"
                " VALUES (?,?,?,?,?)",
                (number, wanted, goal, text, self._now()),
            )
        except sqlite3.Error as exc:  # pragma: no cover
            logger.warning("写贡献失败(%s): %s", number, exc)
            return 0

    def list_contributions(self, user_id: int, limit: int = 100) -> list[dict[str, Any]]:
        number = _positive_id(user_id)
        if number is None:
            return []
        take = _clamp_limit(limit)
        rows = self._query(
            "SELECT * FROM contributions WHERE user_id = ?"
            " ORDER BY created_at DESC, id DESC LIMIT ?", (number, take),
        )
        return [
            {
                "id": int(row["id"]),
                "kind": str(row["kind"] or ""),
                "target": str(row["target"] or ""),
                "summary": str(row["summary"] or ""),
                "created_at": float(row["created_at"] or 0.0),
            }
            for row in rows
        ]

    # -- 汇总(页面侧栏用) ---------------------------------------------------

    def summary(self, user_id: int) -> dict[str, Any]:
        """一次拿到计数(收藏/关注/未读私信/未读通知/贡献). 非法 id -> 全 0."""
        number = _positive_id(user_id)
        if number is None:
            return {
                "favorites": 0, "follows": 0, "unread_messages": 0,
                "unread_notifications": 0, "contributions": 0,
            }

        def count(sql: str) -> int:
            row = self._one(sql, (number,))
            return int(row["n"]) if row is not None else 0

        return {
            "favorites": count("SELECT COUNT(*) AS n FROM favorites WHERE user_id = ?"),
            "follows": count("SELECT COUNT(*) AS n FROM follows WHERE user_id = ?"),
            "unread_messages": self.unread_message_count(number),
            "unread_notifications": self.unread_notification_count(number),
            "contributions": count(
                "SELECT COUNT(*) AS n FROM contributions WHERE user_id = ?"),
        }


def _valid_month_day(month: int, day: int, *, year: int | None) -> bool:
    if not (1 <= month <= 12) or day < 1:
        return False
    days = (31, 29 if (year is None or _is_leap(year)) else 28, 31, 30, 31, 30,
            31, 31, 30, 31, 30, 31)
    return day <= days[month - 1]


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


__all__ = [
    "DEFAULT_PROFILES_FILENAME",
    "AVATAR_DIRNAME",
    "BIO_MAX",
    "BIO_MAX_LINKS",
    "MESSAGE_MAX",
    "SUBJECT_MAX",
    "FEEDBACK_MAX",
    "CONTACT_MAX",
    "CONTRIBUTION_SUMMARY_MAX",
    "AVATAR_MAX_BYTES",
    "AVATAR_TYPES",
    "FOLLOW_KINDS",
    "CONTRIBUTION_KINDS",
    "PRIVACY_KEYS",
    "PRIVACY_DEFAULTS",
    "PROFILE_COLUMNS",
    "ProfileStore",
    "ProfileStoreError",
    "avatar_problem",
    "bio_problem",
    "default_profiles_path",
    "env_profiles_path",
]