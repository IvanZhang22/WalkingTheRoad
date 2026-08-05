"""Vercel Python Function 入口。

Vercel 会将这个 ASGI app 暴露为站点根路径；业务路由仍由 app.main 统一维护。
"""

from app.main import app

__all__ = ["app"]
