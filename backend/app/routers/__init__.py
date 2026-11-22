"""业务路由汇总：专用路由先注册，通用资源路由兜底，避免动态路径抢先匹配。"""
from __future__ import annotations

from app.routers import reconciliation, resource

ROUTERS = [reconciliation, resource]
