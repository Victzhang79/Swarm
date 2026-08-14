#!/usr/bin/env python3
"""30 号文批11 D-1②+D-2 锁：安全响应头中间件 + 内联 handler 事件委托迁移。

- D-2：SecurityHeadersMiddleware 全站补 CSP/nosniff/frame-ancestors/Referrer-Policy
  （原全仓 grep 零命中=零缓解层，D-1 的 XSS 无 CSP 兜底、删除类按钮可点击劫持）。
- D-1②：内联 `on*=` handler 全量迁 `data-on-*` 事件委托（core/delegate.js）——
  ① CSP script-src 'self' 的前置（否则浏览器整批禁掉内联事件属性）；
  ② 消除 `onclick="fn('${x}')"` JS 字符串上下文注入面；配 escapeAttr 属性分档转义器。
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC_ROOT = Path(__file__).resolve().parent.parent / "api" / "static"
EXPECTED_HEADERS = {
    "content-security-policy": None,  # 值单独断言
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
}


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient
    from swarm.api.app import app
    return TestClient(app)


# ─── D-2：响应头行为锁（真 app 真请求） ───

def test_security_headers_on_public_page(client):
    """D-2 主锁：公开页（/）响应必须带全组安全头，且 CSP 为收紧档。"""
    resp = client.get("/")
    for name, want in EXPECTED_HEADERS.items():
        assert name in resp.headers, f"缺安全响应头 {name}（D-2 原病：全站零安全头）"
        if want is not None:
            assert resp.headers[name] == want
    csp = resp.headers["content-security-policy"]
    # 按指令名解析（hunter M 折入：split(";")[1] 依赖指令顺序，顺序一变即 false pass）
    directives = {}
    for part in csp.split(";"):
        tokens = part.strip().split(None, 1)
        if tokens:
            directives[tokens[0]] = tokens[1] if len(tokens) > 1 else ""
    assert directives.get("default-src") == "'self'"
    assert directives.get("script-src") == "'self'", (
        f"CSP script-src 不得含 unsafe-inline（内联 handler 已全迁委托）: {directives.get('script-src')}")
    assert "frame-ancestors" in directives and "none" in directives["frame-ancestors"]


def test_security_headers_on_error_response(client):
    """外层覆盖锁：错误响应（404）同样带头——中间件在鉴权外侧，非 2xx 不落空。
    （测试环境 RBAC 关闭拿不到 401，用 404 验「非成功响应也带头」这一性质；
    401 覆盖由 ordering 锁 test_middleware_registered_outermost 保证。）"""
    resp = client.get("/api/definitely-not-a-route")
    assert resp.status_code == 404
    assert resp.headers.get("x-content-type-options") == "nosniff"
    assert "content-security-policy" in resp.headers


def test_docs_pages_get_relaxed_csp():
    """/docs、/redoc 放宽档（Swagger/ReDoc 默认页走 CDN+内联初始化脚本）——
    收紧档会打断开发调试页；放宽仅限这两页。用迷你 app 单测中间件分支，
    不依赖 docs_public 环境配置。"""
    from fastapi.testclient import TestClient
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    from swarm.api.security_headers import SecurityHeadersMiddleware

    async def ok(request):
        return PlainTextResponse("ok")

    mini = Starlette(routes=[Route("/docs", ok), Route("/api/x", ok)])
    mini.add_middleware(SecurityHeadersMiddleware)
    c = TestClient(mini)
    docs_csp = c.get("/docs").headers["content-security-policy"]
    assert "cdn.jsdelivr.net" in docs_csp, "docs 页必须放行 Swagger CDN"
    api_csp = c.get("/api/x").headers["content-security-policy"]
    assert "cdn.jsdelivr.net" not in api_csp, "非 docs 路径必须保持收紧档"


def test_middleware_registered_outermost(client):
    """接线锁（断接线事实非实现细节）：SecurityHeadersMiddleware 必须在
    user_middleware 栈最外层——Starlette add_middleware 是 insert(0)，
    最后注册=names[0]=最外层（请求先进、响应后出，401/403 也带头）。"""
    from swarm.api.app import app
    names = [m.cls.__name__ for m in app.user_middleware]
    assert names and names[0] == "SecurityHeadersMiddleware", (
        f"安全头中间件不在最外层: {names}")


def test_no_cors_middleware():
    """负面结论锁：全仓不得引入 CORSMiddleware（无 CORS 层 ⇒ allow_origins=*
    配 credentials 类问题不存在，顺手引入=新造攻击面）。"""
    from swarm.api.app import app
    names = [m.cls.__name__ for m in app.user_middleware]
    assert "CORSMiddleware" not in names


# ─── D-1②：委托迁移与转义分档的静态闸（枚举门式，D-3 处方同型） ───

# reviewer L 折入：覆盖单引号/无引号形态；lookbehind 排掉 data-on-* 属性名自身
_INLINE_HANDLER_RE = re.compile(r"(?<![\w-])on[a-z]+\s*=\s*[\"']", re.IGNORECASE)
_DATA_ON_RE = re.compile(r'data-on-[a-z]+="([^"]+)"')


def test_zero_inline_event_handlers():
    """内联 on*= 事件属性必须为零——CSP script-src 'self' 下同批禁掉，
    残留即死按钮；同时它是 D-1 的 JS 字符串注入面。"""
    offenders = []
    for path in sorted(STATIC_ROOT.rglob("*")):
        if path.suffix not in (".html", ".js"):
            continue
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if _INLINE_HANDLER_RE.search(line):
                offenders.append(f"{path.relative_to(STATIC_ROOT)}:{lineno}")
    assert not offenders, "残留内联事件 handler：\n" + "\n".join(offenders[:20])


def test_delegate_core_loaded_before_tabs():
    """接线锁：index.html 必须加载 core/delegate.js（委托内核缺席=全部
    data-on-* 按钮静默失效——静态闸只数 on*= 数不到这个方向）。"""
    html = (STATIC_ROOT / "index.html").read_text()
    assert '<script src="/static/js/core/delegate.js"></script>' in html


def test_no_attribute_context_escape_html_left():
    """属性上下文必须 escapeAttr（escapeHtml 不转引号=属性逃逸，D-1 原病）：
    `xxx="${escapeHtml(` / `xxx='${escapeHtml(` 形态全仓清零。"""
    offenders = []
    for path in sorted(STATIC_ROOT.rglob("*.js")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if '="${escapeHtml(' in line or "='${escapeHtml(" in line:
                offenders.append(f"{path.relative_to(STATIC_ROOT)}:{lineno}")
    assert not offenders, "属性上下文仍用 escapeHtml：\n" + "\n".join(offenders[:20])


def test_all_delegated_handler_names_resolvable():
    """双复核 R1 hunter M 折入闸：每个 data-on-* 引用的 handler 名必须可被
    delegate.js 的 window 解析——简单名要有全局 function 声明，点路径的命名空间
    要显式挂 window（const 词法绑定不在 window 上，PlanningInteraction 五个按钮
    曾因此全哑）。委托派发失败只 console.error=按钮哑巴零机读信号，本闸是唯一
    生产前防线。"""
    names = set()
    for path in sorted(STATIC_ROOT.rglob("*")):
        if path.suffix not in (".html", ".js"):
            continue
        names.update(_DATA_ON_RE.findall(path.read_text()))
    assert names, "闸前提：必须真扫到 data-on-* 引用（扫不到=闸 vacuous）"
    js_all = ""
    for path in sorted(STATIC_ROOT.rglob("*.js")):
        js_all += path.read_text() + "\n"
    bad = []
    for name in sorted(names):
        if "." in name:
            ns = name.split(".", 1)[0]
            # 行首锚定：// 注释里的字面量不算（MU7 实证：注释行会骗过非锚定匹配）
            if not re.search(rf"^\s*window\.{re.escape(ns)}\s*=", js_all, re.M):
                bad.append(f"{name}（命名空间 {ns} 未挂 window）")
        elif not re.search(rf"^(?:async\s+)?function\s+{re.escape(name)}\s*\(",
                           js_all, re.M):
            bad.append(f"{name}（无全局 function 声明）")
    assert not bad, "不可解析的委托 handler：\n" + "\n".join(bad)


def test_escape_attr_defined_alongside_escape_html():
    """分档锁：escapeAttr 必须存在且与 escapeHtml 并存（否决「在 escapeHtml
    里补引号转义」——文本上下文占多数，全局改=可见回归）。"""
    src = (STATIC_ROOT / "js" / "core" / "utils.js").read_text()
    assert "function escapeAttr(" in src
    assert "function escapeHtml(" in src


# ─── escapeAttr 行为锁（node 可用时跑真函数，非复刻） ───

@pytest.mark.skipif(shutil.which("node") is None, reason="node 不可用")
def test_escape_attr_behavior_real_function():
    """从 utils.js 提取真 escapeAttr 函数体在 node 里执行——逐字断言引号/反引号/
    尖括号/和号全转义（非子串近似，非 Python 复刻）。"""
    src = (STATIC_ROOT / "js" / "core" / "utils.js").read_text()
    m = re.search(r"function escapeAttr\(text\) \{.*?\n\}", src, re.S)
    assert m, "escapeAttr 函数提取失败（结构变化=本锁需同步）"
    script = m.group(0) + """
const cases = [
  [`x'onfocus=alert(1)`, `x&#39;onfocus=alert(1)`],
  [`a"b`, `a&quot;b`],
  ['<img>', '&lt;img&gt;'],
  ['a&b', 'a&amp;b'],
  ['`tick`', '&#96;tick&#96;'],
  [null, ''],
  [123, '123'],
];
for (const [inp, want] of cases) {
  const got = escapeAttr(inp);
  if (got !== want) { console.error('MISMATCH', JSON.stringify(inp), got, 'want', want); process.exit(1); }
}
console.log('escapeAttr OK');
"""
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"escapeAttr 行为不符: {proc.stderr}"
    assert "escapeAttr OK" in proc.stdout
