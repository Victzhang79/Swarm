#!/usr/bin/env python3
"""30 号文批11 D-2：全站安全响应头中间件（原全仓 grep 零命中，WebUI 零缓解层）。

- CSP `script-src 'self'`：依赖 D-1② 同批完成的内联 handler 全量迁移
  （`on*=` 属性 → `data-on-*` 事件委托，见 `api/static/js/core/delegate.js`）——
  两者必须同批上，缺迁移则浏览器整批禁掉内联事件属性，UI 全哑。
- `style-src 'unsafe-inline'` 保留：全站 `style="..."` 属性数百处，禁内联样式需
  整站改写且 style 注入风险远低于 script；其余指令收紧。
- /docs、/redoc 走放宽档：FastAPI 默认 Swagger/ReDoc 页面从 cdn.jsdelivr.net 加载
  且含内联初始化 <script>，收紧档会打断开发调试页（仅这两页放宽，API/静态资源不变）。
- 刻意不引入 CORSMiddleware：全仓无 CORS 层 ⇒「allow_origins=["*"] 配 credentials」
  类问题不存在，加响应头时顺手引入 CORS 反而新造攻击面（30 号文 D-2 负面结论）。
- setdefault 语义：端点若自行设置同名头则尊重（不覆盖），防未来特例被中间件压平。
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

_CSP_STRICT = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
    "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
# Swagger/ReDoc 默认页：CDN 脚本/样式 + 内联初始化 script（仅 /docs、/redoc 两页放宽）
_CSP_DOCS = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data:; connect-src 'self'; "
    "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
_DOCS_PATHS = ("/docs", "/redoc")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """给所有 HTTP 响应统一补安全响应头（含认证拒绝的 401/403——注册在鉴权中间件外侧）。"""

    async def dispatch(self, request: Request, call_next):
        resp = await call_next(request)
        h = resp.headers
        is_docs = request.url.path in _DOCS_PATHS
        h.setdefault("Content-Security-Policy", _CSP_DOCS if is_docs else _CSP_STRICT)
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "no-referrer")
        return resp
