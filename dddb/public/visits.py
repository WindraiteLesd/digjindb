"""访问记录 / 服务器负载采集(站务看板用, 用户 m14663).

两个职责:

1. **访问日志**: 每一次请求落一行 ``site_visits``(路径、路由名、状态码、
   用户、IP、UA 前 200 字), 供站务看板做"最近访问""热门页面""访客数"。
   写入必须是**尽力而为**的 —— 记日志失败绝不能影响页面返回, 所以所有
   异常都在这里吞掉。
2. **服务器负载**: 纯 stdlib 读 ``/proc/stat`` / ``/proc/meminfo`` 和
   ``os.statvfs``, 不进 subprocess(线上没有 ``psutil``, 也不该为看板装依赖)。

``site_visits`` 表刻意只保留**最近若干天**: 一次浏览带出 css/js/图片
好几个请求, 不清理会越长越大。``purge()`` 由看板页面触发, 每次最多删
一批, 不做后台线程。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

logger = logging.getLogger("dddb.public.visits")

#: 访问日志保留天数。看板只关心"最近", 老数据没有任何用途。
VISIT_KEEP_DAYS = 30

#: 一次喷删的上限, 防止某次清理卡住请求。
PURGE_BATCH = 5000

#: 页面里"最近访问"列表的默认条数。
RECENT_LIMIT = 120

#: UA 只存前这么多字符 —— 完整 UA 动辄 150+ 字, 存了也没人看。
UA_MAX = 200

PATH_MAX = 300

_SCHEMA = """
CREATE TABLE IF NOT EXISTS site_visits (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        REAL    NOT NULL DEFAULT 0,
    path      TEXT    NOT NULL DEFAULT '',
    route     TEXT    NOT NULL DEFAULT '',
    method    TEXT    NOT NULL DEFAULT 'GET',
    status    INTEGER NOT NULL DEFAULT 0,
    user_id   INTEGER NOT NULL DEFAULT 0,
    username  TEXT    NOT NULL DEFAULT '',
    client_ip TEXT    NOT NULL DEFAULT '',
    ua        TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_visits_at ON site_visits(at);
CREATE INDEX IF NOT EXISTS idx_visits_route ON site_visits(route);
CREATE INDEX IF NOT EXISTS idx_visits_user ON site_visits(user_id);
"""


def _clip(value: Any, limit: int) -> str:
    text = str(value or "")
    return text[:limit] if len(text) > limit else text


class VisitStore:
    """``site_visits`` 的最小读写封装. 只读打开失败就整体降级成 no-op."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path or "")
        self._ok = False
        if not self.path:
            return
        try:
            parent = Path(self.path).parent
            parent.mkdir(parents=True, exist_ok=True)
        except OSError:                       # pragma: no cover - 目录建不出来
            return
        try:
            with self._session() as conn:
                conn.executescript(_SCHEMA)
            self._ok = True
        except sqlite3.Error as exc:         # pragma: no cover
            logger.warning("访问日志库初始化失败 %s: %s", self.path, exc)

    # -- 基础设施 -----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:                 # pragma: no cover
            pass
        return conn

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """打开 -> 提交/回滚 -> **一定关闭**.

        ``with sqlite3.connect(...)`` 只提交事务、**不关闭连接**(sqlite3 的
        ``Connection.__exit__`` 就这么实现的), 于是每次 record/purge 都会漏掉
        一个句柄 —— 一次浏览就漏几个, 服务跑一阵子就 file descriptor 用光。
        """
        conn = self._connect()
        try:
            with conn:                        # 事务语义: 异常回滚
                yield conn
        finally:
            conn.close()

    @property
    def enabled(self) -> bool:
        return self._ok

    def close(self) -> None:                  # 兼容 Store 的调用习惯
        return None

    # -- 写 -----------------------------------------------------------------

    def record(
        self,
        *,
        path: str,
        route: str = "",
        method: str = "GET",
        status: int = 0,
        user_id: int = 0,
        username: str = "",
        client_ip: str = "",
        ua: str = "",
        now: float | None = None,
    ) -> bool:
        """落一行访问记录. 失败返回 ``False``, **永不抛异常**."""
        if not self._ok:
            return False
        try:
            with self._session() as conn:
                conn.execute(
                    "INSERT INTO site_visits"
                    "(at, path, route, method, status, user_id, username, client_ip, ua)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (float(time.time() if now is None else now),
                     _clip(path, PATH_MAX), _clip(route, 80),
                     _clip(method, 8).upper(), int(status or 0),
                     int(user_id or 0), _clip(username, 60),
                     _clip(client_ip, 64), _clip(ua, UA_MAX)),
                )
            return True
        except (sqlite3.Error, OSError, ValueError) as exc:   # pragma: no cover
            logger.debug("写访问记录失败: %s", exc)
            return False

    def purge(self, *, keep_days: int = VISIT_KEEP_DAYS,
              now: float | None = None) -> int:
        """删掉 ``keep_days`` 天以前的记录. 返回删除行数(最多 PURGE_BATCH)."""
        if not self._ok:
            return 0
        cutoff = float(now if now is not None else time.time()) - keep_days * 86400.0
        try:
            with self._session() as conn:
                cur = conn.execute(
                    "DELETE FROM site_visits WHERE id IN ("
                    " SELECT id FROM site_visits WHERE at < ? ORDER BY at LIMIT ?)",
                    (cutoff, PURGE_BATCH),
                )
                return int(cur.rowcount or 0)
        except sqlite3.Error as exc:          # pragma: no cover
            logger.debug("清理访问记录失败: %s", exc)
            return 0

    # -- 读 -----------------------------------------------------------------

    def recent(self, limit: int = RECENT_LIMIT) -> list[dict[str, Any]]:
        if not self._ok:
            return []
        try:
            with self._session() as conn:
                rows = conn.execute(
                    "SELECT at, path, route, method, status, user_id, username,"
                    " client_ip, ua FROM site_visits"
                    " ORDER BY at DESC, id DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
        except sqlite3.Error:                 # pragma: no cover
            return []
        return [dict(row) for row in rows]

    def totals(self, *, hours: int = 24, now: float | None = None) -> dict[str, Any]:
        """看板头部数字: 近 N 小时访问数 / 独立访客 / 登录用户数 / 全部."""

        def one(sql: str, params: tuple[Any, ...]) -> int:
            try:
                with self._session() as conn:
                    row = conn.execute(sql, params).fetchone()
                return int(row[0] or 0) if row is not None else 0
            except sqlite3.Error:             # pragma: no cover
                return 0

        since = float(now if now is not None else time.time()) - hours * 3600.0
        return {
            "views": one("SELECT COUNT(*) FROM site_visits WHERE at >= ?", (since,)),
            "visitors": one(
                "SELECT COUNT(DISTINCT client_ip) FROM site_visits WHERE at >= ?",
                (since,)),
            "members": one(
                "SELECT COUNT(DISTINCT user_id) FROM site_visits"
                " WHERE at >= ? AND user_id > 0", (since,)),
            "total": one("SELECT COUNT(*) FROM site_visits", ()),
        }

    def top_routes(self, *, hours: int = 24, limit: int = 15,
                   now: float | None = None) -> list[dict[str, Any]]:
        since = float(now if now is not None else time.time()) - hours * 3600.0
        if not self._ok:
            return []
        try:
            with self._session() as conn:
                rows = conn.execute(
                    "SELECT route, COUNT(*) AS n FROM site_visits"
                    " WHERE at >= ? AND route != '' GROUP BY route"
                    " ORDER BY n DESC, route LIMIT ?",
                    (since, max(1, int(limit))),
                ).fetchall()
        except sqlite3.Error:                 # pragma: no cover
            return []
        return [{"route": str(r["route"]), "count": int(r["n"])} for r in rows]

    def top_paths(self, *, hours: int = 24, limit: int = 15,
                  now: float | None = None) -> list[dict[str, Any]]:
        since = float(now if now is not None else time.time()) - hours * 3600.0
        if not self._ok:
            return []
        try:
            with self._session() as conn:
                rows = conn.execute(
                    "SELECT path, COUNT(*) AS n FROM site_visits"
                    " WHERE at >= ? GROUP BY path ORDER BY n DESC, path LIMIT ?",
                    (since, max(1, int(limit))),
                ).fetchall()
        except sqlite3.Error:                 # pragma: no cover
            return []
        return [{"path": str(r["path"]), "count": int(r["n"])} for r in rows]


# ---------------------------------------------------------------------------
# 服务器负载(纯 stdlib)
# ---------------------------------------------------------------------------


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def cpu_snapshot() -> tuple[int, int] | None:
    """``(idle, total)`` 抖动值; 取不到返回 ``None``."""
    text = _read_text("/proc/stat")
    if not text:
        return None
    for line in text.splitlines():
        if not line.startswith("cpu "):
            continue
        parts = line.split()[1:]
        try:
            values = [int(p) for p in parts]
        except ValueError:
            return None
        if len(values) < 4:
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        return idle, total
    return None


def cpu_percent(interval: float = 0.12) -> float:
    """两次采样的差值算 CPU 占用率; 采样不到返回 ``-1``."""
    first = cpu_snapshot()
    if first is None:
        return -1.0
    time.sleep(max(0.0, min(0.5, interval)))
    second = cpu_snapshot()
    if second is None:
        return -1.0
    idle_delta = second[0] - first[0]
    total_delta = second[1] - first[1]
    if total_delta <= 0:
        return -1.0
    busy = 100.0 * (total_delta - idle_delta) / total_delta
    return max(0.0, min(100.0, busy))


def memory_info() -> dict[str, float]:
    """``{total_mb, available_mb, used_mb, percent}``; 读不到全 0."""
    text = _read_text("/proc/meminfo")
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        chunk = rest.strip().split()
        if not chunk:
            continue
        try:
            values[key.strip()] = int(chunk[0])
        except ValueError:
            continue
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable") or values.get("MemFree", 0)
    if total <= 0:
        return {"total_mb": 0.0, "available_mb": 0.0, "used_mb": 0.0,
                "percent": 0.0}
    used = max(0, total - available)
    return {
        "total_mb": round(total / 1024.0, 1),
        "available_mb": round(available / 1024.0, 1),
        "used_mb": round(used / 1024.0, 1),
        "percent": round(100.0 * used / total, 1),
    }


def load_average() -> tuple[float, float, float]:
    try:
        one, five, fifteen = os.getloadavg()
    except (OSError, AttributeError):        # pragma: no cover - Windows 没有
        return (-1.0, -1.0, -1.0)
    return (round(one, 2), round(five, 2), round(fifteen, 2))


def disk_info(path: str | Path = "/") -> dict[str, float]:
    """``{total_gb, used_gb, free_gb, percent}``; statvfs 不可用就全 0."""
    try:
        usage = os.statvfs(str(path))
    except (OSError, AttributeError):        # pragma: no cover
        return {"total_gb": 0.0, "used_gb": 0.0, "free_gb": 0.0, "percent": 0.0}
    block = usage.f_frsize or usage.f_bsize or 0
    total = float(usage.f_blocks * block)
    free = float(usage.f_bavail * block)
    if total <= 0:
        return {"total_gb": 0.0, "used_gb": 0.0, "free_gb": 0.0, "percent": 0.0}
    used = max(0.0, total - free)
    return {
        "total_gb": round(total / (1024 ** 3), 1),
        "used_gb": round(used / (1024 ** 3), 1),
        "free_gb": round(free / (1024 ** 3), 1),
        "percent": round(100.0 * used / total, 1),
    }


def uptime_text() -> str:
    text = _read_text("/proc/uptime")
    chunk = text.split()
    if not chunk:
        return ""
    try:
        seconds = float(chunk[0])
    except ValueError:
        return ""
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} 天 {hours} 小时 {minutes} 分"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分"


def server_stats(*, disk_path: str | Path = "/") -> dict[str, Any]:
    """看板"服务器"卡片要的全部数字, 一次采完."""
    one, five, fifteen = load_average()
    return {
        "cpu_percent": cpu_percent(),
        "memory": memory_info(),
        "disk": disk_info(disk_path),
        "load": {"one": one, "five": five, "fifteen": fifteen},
        "uptime": uptime_text(),
    }


__all__ = [
    "VISIT_KEEP_DAYS",
    "PURGE_BATCH",
    "RECENT_LIMIT",
    "VisitStore",
    "cpu_percent",
    "cpu_snapshot",
    "disk_info",
    "load_average",
    "memory_info",
    "server_stats",
    "uptime_text",
]
