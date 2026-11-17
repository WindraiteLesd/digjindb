"""用户端站点(面向访客 / 普通用户 / 认证编辑者).

完全独立的用户端项目: **不与任何管理面板共享代码、URL 或数据文件**。

* 访客与普通用户: 浏览、搜索、详情、社团页、展会页、统计看板、工作队列 —— 只读;
* 认证编辑者(``editor``): 可以在站内直接订正专辑资料(走 POST + CSRF 校验);
* 账号: 存在**独立**的 ``data/public_users.sqlite3``, 与专辑主库无关;
* 页面不引用任何外部资源, 也没有一行 JavaScript。

模块划分::

    dddb/public/data.py       只读查询层(ReadOnlyStore 用 mode=ro 开库)
    dddb/public/templates.py  纯函数 HTML 模板(可单测, 不碰 I/O)
    dddb/public/site.py       路由 / http.server / 静态资源 / CLI 入口
    dddb/public/users.py      账号库与口令哈希(独立 sqlite)
    dddb/public/auth.py       会话 / Cookie / CSRF / 鉴权判定
    dddb/public/static/public.css  暗色主题样式(无 CDN 依赖, 离线可用)

独立起服务::

    python -m dddb.public.site --port 8940

账号管理::

    python -m dddb.public.users add <用户名>
    python -m dddb.public.users invite          # 一次性编辑者邀请码
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "PublicData",
    "PublicSite",
    "PublicHandler",
    "make_server",
    "serve",
    "paginate",
    "parse_route",
    "UserStore",
    "SessionManager",
]

#: 包内重模块按需导入(PEP 562), 这样只想用纯模板函数的调用方
#: 不会被迫连带载入 sqlite3 / http.server。
_LAZY = {
    "PublicData": ("data", "PublicData"),
    "paginate": ("data", "paginate"),
    "parse_route": ("site", "parse_route"),
    "PublicSite": ("site", "PublicSite"),
    "PublicHandler": ("site", "PublicHandler"),
    "make_server": ("site", "make_server"),
    "serve": ("site", "serve"),
    "UserStore": ("users", "UserStore"),
    "SessionManager": ("auth", "SessionManager"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module = import_module(f".{target[0]}", __name__)
    return getattr(module, target[1])


def __dir__() -> list[str]:
    return sorted(__all__)