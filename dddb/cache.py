"""HTTP 响应缓存(SQLite).

用于降低对目标站点的压力, 也方便离线重放与调试.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping

_SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    key        TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    method     TEXT NOT NULL,
    status     INTEGER NOT NULL,
    headers    TEXT NOT NULL DEFAULT '{}',
    body       BLOB,
    fetched_at REAL NOT NULL,
    ttl        INTEGER NOT NULL DEFAULT 86400
);
CREATE INDEX IF NOT EXISTS idx_http_cache_fetched ON http_cache(fetched_at);

CREATE TABLE IF NOT EXISTS kv (
    ns         TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (ns, key)
);
"""


def cache_key(method: str, url: str, body: Any = None) -> str:
    h = hashlib.sha256()
    h.update(method.upper().encode())
    h.update(b"\x00")
    h.update(url.encode("utf-8", "replace"))
    if body:
        h.update(b"\x00")
        h.update(repr(body).encode("utf-8", "replace"))
    return h.hexdigest()


class HttpCache:
    """线程安全的 HTTP 缓存."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- HTTP ---------------------------------------------------------------

    def get(self, method: str, url: str, body: Any = None, *, allow_stale: bool = False) -> dict[str, Any] | None:
        key = cache_key(method, url, body)
        with self._lock:
            row = self._conn.execute("SELECT * FROM http_cache WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        age = time.time() - float(row["fetched_at"])
        ttl = int(row["ttl"] or 0)
        if not allow_stale and ttl > 0 and age > ttl:
            return None
        return {
            "status": int(row["status"]),
            "headers": _json_loads(row["headers"], {}),
            "body": bytes(row["body"]) if row["body"] is not None else b"",
            "fetched_at": float(row["fetched_at"]),
            "stale": bool(ttl > 0 and age > ttl),
        }

    def put(
        self,
        method: str,
        url: str,
        status: int,
        headers: Mapping[str, str],
        body: bytes,
        *,
        ttl: int | None = None,
        request_body: Any = None,
    ) -> None:
        import json

        key = cache_key(method, url, request_body)
        with self._lock:
            self._conn.execute(
                "REPLACE INTO http_cache(key, url, method, status, headers, body, fetched_at, ttl)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    key,
                    url,
                    method.upper(),
                    int(status),
                    json.dumps(dict(headers), ensure_ascii=False),
                    body,
                    time.time(),
                    int(ttl if ttl is not None else 86400),
                ),
            )
            self._conn.commit()

    def invalidate(self, url: str | None = None) -> int:
        with self._lock:
            if url is None:
                cur = self._conn.execute("DELETE FROM http_cache")
            else:
                cur = self._conn.execute("DELETE FROM http_cache WHERE url LIKE ?", (f"%{url}%",))
            self._conn.commit()
            return cur.rowcount

    def vacuum_old(self, older_than_days: int = 30) -> int:
        cutoff = time.time() - older_than_days * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM http_cache WHERE fetched_at < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    def stats(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(body)),0) AS bytes,"
                " MIN(fetched_at) AS oldest, MAX(fetched_at) AS newest FROM http_cache"
            ).fetchone()
        return {
            "entries": int(row["n"] or 0),
            "bytes": int(row["bytes"] or 0),
            "oldest": row["oldest"],
            "newest": row["newest"],
        }

    # -- 通用 kv ------------------------------------------------------------

    def set_value(self, ns: str, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "REPLACE INTO kv(ns, key, value, updated_at) VALUES (?,?,?,?)",
                (ns, key, value, time.time()),
            )
            self._conn.commit()

    def get_value(self, ns: str, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE ns = ? AND key = ?", (ns, key)).fetchone()
        return row["value"] if row is not None else default

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _json_loads(text: Any, default: Any) -> Any:
    import json

    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
