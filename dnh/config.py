"""配置加载(上游精简版).

线上站点只需要 ``[public]`` 这一段配置; 采集端的其它配置项不在本仓库内。
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError

DEFAULT_CONFIG_RELPATH = "config/config.toml"


@dataclass
class PublicConfig:
    """线上站点的部署旋钮."""

    sessions_db: str = "data/sessions.sqlite3"
    base_url: str = ""
    behind_proxy: bool = True
    rate_limit_enabled: bool = True
    rate_limit_rate: float = 30.0
    rate_limit_burst: int = 60
    rate_limit_heavy_burst: int = 10
    rate_limit_write_burst: int = 5


@dataclass
class Config:
    """站点配置(只有线上需要的部分)."""

    root: Path = Path(".")
    db_path: Path = Path("data/dnh.sqlite3")
    covers_dir: Path = Path("data/covers")
    public: PublicConfig = None            # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.public is None:
            self.public = PublicConfig()

    def ensure_dirs(self) -> None:
        """建好运行期目录(data / covers)."""
        for path in (self.root / "data", self.covers_dir):
            try:
                Path(path).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    @classmethod
    def default(cls, root: Path | str | None = None) -> "Config":
        """不带配置文件时的默认值(测试与首次启动用)."""
        return cls.load(root)

    @classmethod
    def load(cls, root: Path | str | None = None) -> "Config":
        base = Path(root).resolve() if root else Path.cwd().resolve()
        path = base / DEFAULT_CONFIG_RELPATH
        data: Mapping[str, Any] = {}
        if path.is_file():
            try:
                with path.open("rb") as fh:
                    data = tomllib.load(fh)
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise ConfigError(f"配置文件读不了: {path}: {exc}") from exc
        section = data.get("public") if isinstance(data, Mapping) else None
        pub = PublicConfig()
        if isinstance(section, Mapping):
            for field_name in pub.__dataclass_fields__:
                if field_name in section:
                    setattr(pub, field_name, section[field_name])
        # 环境变量优先
        env_map = {
            "DNH_PUBLIC_SESSIONS_DB": "sessions_db",
            "DNH_PUBLIC_BASE_URL": "base_url",
        }
        for env_name, field_name in env_map.items():
            value = os.environ.get(env_name)
            if value:
                setattr(pub, field_name, value)
        if os.environ.get("DNH_PUBLIC_BEHIND_PROXY"):
            pub.behind_proxy = True
        return cls(root=base, db_path=base / "data" / "dnh.sqlite3",
                   covers_dir=base / "data" / "covers", public=pub)


def write_example_config(path: Path | str) -> None:
    """写一份配置样例(上游只保留 [public] 段)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "[public]\n"
        'sessions_db = "data/sessions.sqlite3"\n'
        'base_url = ""\n'
        "behind_proxy = true\n"
        "rate_limit_enabled = true\n",
        encoding="utf-8",
    )


__all__ = ["Config", "PublicConfig", "DEFAULT_CONFIG_RELPATH",
           "write_example_config"]
