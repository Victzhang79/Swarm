"""Swarm API 包 — 暴露 FastAPI app 实例"""

try:
    from swarm.api.app import app  # noqa: F401
except ImportError:
    # app.py 尚未就绪时，允许包正常导入
    app = None  # type: ignore[assignment]

__all__ = ["app"]
