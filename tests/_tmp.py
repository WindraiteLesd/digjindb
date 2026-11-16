"""测试用的临时目录工具.

两个坑, 都在这里绕开:

1. Windows 沙箱下系统 ``TEMP`` 目录不可创建子目录(``PermissionError``), 所以临时目录
   统一放到仓库内的 ``.tmp/`` 下。
2. ``tempfile.mkdtemp`` 在 Windows 上会创建一个写不进去的目录(它继承到的权限项里
   不含沙箱令牌), 因此这里用 ``Path.mkdir()`` + uuid 自己造目录。
"""

from __future__ import annotations

import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

TMP_ROOT = Path(__file__).resolve().parent.parent / ".tmp"


def temp_path(name: str = "case") -> Path:
    """创建一个可写的临时目录并返回路径."""
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TMP_ROOT / f"{name}-{uuid.uuid4().hex[:10]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


@contextmanager
def temp_dir(prefix: str = "case") -> Iterator[Path]:
    path = temp_path(prefix.rstrip("-") or "case")
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
