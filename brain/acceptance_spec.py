"""S2-4：验收断言 spec 纯模块 —— requirement items → 可执行断言（HTTP probe）。

设计定案见 docs/ACCEPTANCE_DESIGN.md 定案 2 / §5 / 给 task#23/#25 的实现指引：
- 断言挂【任务级 requirement item】（req_id 回指），绝不挂子任务级；
- spec 形状：{id, req_id, kind:"http_probe", request:{method,path,headers?,body_json?},
  expect:{status:[int], body_contains?:[str]}, auth:"none|manual"}；
- fail-closed：auth 缺省 "manual"（默认不执行，显式 "none" 才可执行）；推导不出
  可执行形态 → kind="manual"，绝不硬编断言假绿；
- SSRF 边界：path 必须以 / 开头且无 scheme/host——probe 只打被测应用
  http://127.0.0.1:<port>，绝不外呼；
- expect 判定不在脚本里做：脚本只收割证据（结构化标记 + body 截断 base64），
  判定在 Python 侧纯函数（`evaluate_probe_result`），保持脚本简单 + 判定可单测。

标记族与 runtime_smoke 的 `__SMOKE_*` 同风格、前缀区分（`__ACCEPT_*`）：
- `__ACCEPT_RESULT__<id>__<http_code>`（curl -w %{http_code}；连接失败=000）
- `__ACCEPT_BODY__<id>__<body 头部截断的 base64>`（base64 防换行/管道符破坏标记行，
  与 worker/sandbox.py 的 base64 管道回写先例同族）

本模块纯函数：零网络、零沙箱 IO、不调模型（prompt builder 只产 prompt 文本，
LLM 调用接线是 task#26 的事）。阶段3 ACCEPT phase（task#25）把
`assertion_to_probe_cmd` 产出的片段并进 build_smoke_script 的 assert 段。
"""
from __future__ import annotations

import base64
import json
import re
import shlex
from typing import Any

# ── 单条 probe 预算/截断（curl --max-time 已有 2s 探活先例，断言给宽到 10s） ──
DEFAULT_PROBE_MAX_TIME_SEC = 10
# body 头部收割字节数（大 body 累积可能顶爆 run_command stdout——保守截断）
ACCEPT_BODY_HEAD_BYTES = 512

# ── 结构化输出标记（与 __SMOKE_* 族同风格，前缀区分） ──
MARK_ACCEPT_RESULT = "__ACCEPT_RESULT__"   # __ACCEPT_RESULT__<id>__<http_code>
MARK_ACCEPT_BODY = "__ACCEPT_BODY__"       # __ACCEPT_BODY__<id>__<b64(body 头部)>

# ── 枚举（数据表；阶段2 只有 http_probe 一种可执行 kind） ──
_VALID_KINDS = ("http_probe", "manual")
_VALID_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
_VALID_AUTH = ("none", "manual")

# 断言 id：标记行/文件名安全字符集；禁 "__"（会与标记分隔符歧义）
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_PATH_MAX_LEN = 2048
_EVIDENCE_MAX_LEN = 500
# S2 复核 F8：断言条数/体积上限（对照 requirements_extract.MAX_ITEMS 的抽取失控自觉）。
# 超量截断留痕（脚本体积/输出体积无上限会顶爆 run_command stdout，ACCEPTANCE_DESIGN
# 在线探测项#4）；单条 body_json 超体积 → 该条降级 manual 留痕（不整体拒绝）。
MAX_ASSERTIONS = 30
MAX_BODY_JSON_BYTES = 8192

_RESULT_MARK_RE = re.compile(
    re.escape(MARK_ACCEPT_RESULT) + r"([A-Za-z0-9_.-]+)__(\d{1,3})")
_BODY_MARK_RE = re.compile(
    re.escape(MARK_ACCEPT_BODY) + r"([A-Za-z0-9_.-]+)__([A-Za-z0-9+/=]*)")


# ═══════════════════════ schema 校验（纯函数） ═══════════════════════

def _reject(rejected: list[dict[str, Any]], item: Any, reason: str) -> None:
    rejected.append({"spec": item, "reason": reason})


def _validate_path(path: Any) -> str | None:
    """path 合法 → 归一化 path；非法 → None。

    SSRF 面：probe URL 由 Python 侧拼 http://127.0.0.1:<port><path>，path 里
    出现 scheme/host（"://"、协议相对 "//host"）或空白/控制字符一律拒绝。
    """
    if not isinstance(path, str) or not path:
        return None
    if len(path) > _PATH_MAX_LEN:
        return None
    if not path.startswith("/") or path.startswith("//"):
        return None
    if "://" in path:
        return None
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in path):
        return None
    return path


def _validate_status_list(raw: Any) -> list[int] | None:
    """expect.status：int 或 [int...] → 归一化列表；非法码（含 bool）→ None。"""
    values = raw if isinstance(raw, list) else [raw]
    out: list[int] = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, int):
            return None
        if not (100 <= v <= 599):
            return None
        out.append(v)
    return out or None


def _validate_headers(raw: Any) -> dict[str, str] | None:
    """headers：dict[str,str]，禁 CR/LF（header 注入面）与 key 内冒号。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return None
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str) or not k.strip():
            return None
        if "\r" in k or "\n" in k or "\r" in v or "\n" in v or ":" in k:
            return None
        out[k.strip()] = v.strip()
    return out


def _validate_body_contains(raw: Any) -> list[str] | None:
    """body_contains：str 或 [str...] → 归一化列表；空串/非串 → None（非法）。"""
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    for v in values:
        if not isinstance(v, str) or not v:
            return None
        out.append(v)
    return out


def _fold_ws(text: str) -> str:
    """空白归一（F7 grounding 比对用）：去全部空白后比 substring——排版换行/缩进
    不该让真 evidence 被误拒（requirements_extract 的 quote 回指同口径）。"""
    return "".join(ch for ch in str(text) if not ch.isspace())


def validate_assertions(
    raw: Any,
    requirement_items: list[dict[str, Any]] | None,
    context_text: str | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """LLM 产出的断言列表 → (valid_specs, rejected)。逐条校验，拒单条不拒全量。

    fail-closed 规则：
    - auth 缺省/未知取值 → "manual"（默认不执行，显式 "none" 才可执行）；
    - req_id 必须回指真实存在的 requirement item id（悬空 → 剔除记原因）；
    - kind/method 枚举外、path 带 scheme/host、status 非法码、body_json 不可
      序列化、header 含 CR/LF → 剔除记原因；
    - 重复 id 去重（先到者胜，后到剔除记原因）；
    - F8：条数超 MAX_ASSERTIONS 截断留痕；body_json 序列化超 MAX_BODY_JSON_BYTES
      → 该条降级 manual 留痕（保留人工可见，不静默丢）；
    - F7（防臆造落地）：context_text 提供时，http_probe 条目的 evidence 必须非空
      且（空白归一后）是 context_text 的 substring——回指不上 → 降级 manual 留痕
      （prompt 纪律2 的确定性对账面；不提供 context_text 维持旧行为，向后兼容）。

    被拒/被降级条目绝不静默丢：rejected 每条带 {spec, reason}，供 degraded/details 留痕。
    """
    valid: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        _reject(rejected, raw, "断言集合不是列表，整体拒绝")
        return valid, rejected

    known_req_ids = {
        str(item.get("id"))
        for item in (requirement_items or [])
        if isinstance(item, dict) and item.get("id")
    }
    seen_ids: set[str] = set()
    folded_context = _fold_ws(context_text) if context_text is not None else None

    def _coerce_manual(normalized_spec: dict[str, Any], item: Any, reason: str) -> None:
        # 降级保留（非剔除）：条目转 manual 进 valid 供人工核验，同时 rejected 留痕
        # （degraded 计数可观测——"仅条件写无人清"与静默丢弃都是登记在案的 bug 模式）。
        _reject(rejected, item, reason)
        normalized_spec["kind"] = "manual"
        normalized_spec["auth"] = "manual"
        seen_ids.add(str(normalized_spec["id"]))
        valid.append(normalized_spec)

    for item in raw:
        if not isinstance(item, dict):
            _reject(rejected, item, "断言条目不是对象")
            continue

        spec_id = item.get("id")
        if not isinstance(spec_id, str) or not _ID_RE.match(spec_id) \
                or "__" in spec_id:
            _reject(rejected, item, f"id 非法（须匹配 {_ID_RE.pattern} 且不含 '__'）")
            continue
        if spec_id in seen_ids:
            _reject(rejected, item, f"重复 id {spec_id!r}，去重（先到者胜）")
            continue
        if len(valid) >= MAX_ASSERTIONS:
            # F8：超量=生成失控信号，截断留痕（对照 requirements MAX_ITEMS over_limit）
            _reject(rejected, item,
                    f"超出断言条数上限 MAX_ASSERTIONS={MAX_ASSERTIONS}，该条截断")
            continue

        req_id = item.get("req_id")
        if not isinstance(req_id, str) or req_id not in known_req_ids:
            _reject(rejected, item,
                    f"req_id {req_id!r} 悬空：未回指任何真实 requirement item")
            continue

        kind = item.get("kind")
        if kind not in _VALID_KINDS:
            _reject(rejected, item, f"kind {kind!r} 不在枚举 {_VALID_KINDS}")
            continue

        # auth：缺省 manual；未知取值一律 manual（fail-closed 方向，绝不误放行）
        auth_raw = item.get("auth", "manual")
        auth = auth_raw if auth_raw in _VALID_AUTH else "manual"

        normalized: dict[str, Any] = {
            "id": spec_id, "req_id": req_id, "kind": kind, "auth": auth,
        }
        evidence = item.get("evidence")
        if isinstance(evidence, str) and evidence.strip():
            normalized["evidence"] = evidence.strip()[:_EVIDENCE_MAX_LEN]

        if kind == "manual":
            # manual 是"推导不出可执行形态"的如实降级——不要求 request/expect
            normalized["auth"] = "manual"
            seen_ids.add(spec_id)
            valid.append(normalized)
            continue

        # ---- kind == http_probe：request/expect 全量形状校验 ----
        request = item.get("request")
        if not isinstance(request, dict):
            _reject(rejected, item, "http_probe 缺 request 对象")
            continue
        method = str(request.get("method", "") or "").strip().upper()
        if method not in _VALID_METHODS:
            _reject(rejected, item, f"method {method!r} 不在枚举 {_VALID_METHODS}")
            continue
        path = _validate_path(request.get("path"))
        if path is None:
            _reject(rejected, item,
                    "path 非法：必须以 / 开头、无 scheme/host（只打被测应用本机）、"
                    "无空白/控制字符")
            continue
        headers = _validate_headers(request.get("headers"))
        if headers is None:
            _reject(rejected, item, "headers 非法（须 dict[str,str] 且无 CR/LF，header 注入面）")
            continue
        body_json = request.get("body_json")
        if body_json is not None:
            try:
                _payload_probe = json.dumps(body_json, ensure_ascii=False)
            except (TypeError, ValueError):
                _reject(rejected, item, "body_json 不可 JSON 序列化")
                continue
            _payload_bytes = len(_payload_probe.encode("utf-8"))
            if _payload_bytes > MAX_BODY_JSON_BYTES:
                # F8：超体积 body 会顶爆脚本/stdout 预算 → 该条降级 manual 留痕
                _coerce_manual(
                    normalized, item,
                    f"body_json 序列化 {_payload_bytes} 字节超上限 "
                    f"{MAX_BODY_JSON_BYTES} → 降级 manual（留痕，人工验证）")
                continue

        expect = item.get("expect")
        if not isinstance(expect, dict):
            _reject(rejected, item, "http_probe 缺 expect 对象")
            continue
        status = _validate_status_list(expect.get("status"))
        if status is None:
            _reject(rejected, item, "expect.status 非法：须为 100-599 的 int 列表")
            continue
        body_contains = _validate_body_contains(expect.get("body_contains"))
        if body_contains is None:
            _reject(rejected, item, "expect.body_contains 非法：须为非空字符串列表")
            continue

        norm_request: dict[str, Any] = {"method": method, "path": path}
        if headers:
            norm_request["headers"] = headers
        if body_json is not None:
            norm_request["body_json"] = body_json
        norm_expect: dict[str, Any] = {"status": status}
        if body_contains:
            norm_expect["body_contains"] = body_contains
        normalized["request"] = norm_request
        normalized["expect"] = norm_expect

        # F7 grounding：context_text 提供时，evidence 必须非空且回指生成语料
        # （空白归一 substring）。回指不上 = prompt 纪律2 的"臆造路径"确定性信号 →
        # 降级 manual（request/expect 保留供人工核验）。未提供 context 维持旧行为。
        if folded_context is not None:
            folded_evidence = _fold_ws(normalized.get("evidence") or "")
            if not folded_evidence or folded_evidence not in folded_context:
                _coerce_manual(
                    normalized, item,
                    "evidence 缺失或未回指生成语料（防臆造 API 路径）→ 降级 manual"
                    "（留痕，人工验证）")
                continue

        seen_ids.add(spec_id)
        valid.append(normalized)

    return valid, rejected


# ═══════════════════ 生成 prompt builder（只产文本，不调模型） ═══════════════════

_ASSERTION_PROMPT_TEMPLATE = """你是验收断言生成器。\
任务：把下面的需求条目转成对**运行中应用**的 HTTP 黑盒断言。

## 需求条目（req_id 必须逐字回指这里的 id）
{items_block}

## 设计/接口上下文（断言路径的唯一证据来源）
{design_context}

## 输出格式：JSON 数组，每条：
{{"id": "a1", "req_id": "<条目id>", "kind": "http_probe", \
"request": {{"method": "GET", "path": "/...", "headers": {{}}, "body_json": null}}, \
"expect": {{"status": [200], "body_contains": []}}, "auth": "none", \
"evidence": "<设计/需求原文中出现该路径的逐字引用>"}}

## 硬性纪律（违反即被确定性校验剔除）
1. **只为无需登录/鉴权即可验证的条目**生成 kind="http_probe" 且 auth="none"；
   任何需要登录态/token/会话/权限的验证一律 kind="manual"（阶段内不自动执行，
   留给人工验证）。拿不准就标 manual——绝不为了产出断言而假设鉴权可绕过。
2. **绝不臆造不存在的 API 路径**：每条断言必须带 "evidence" 字段，逐字引用上面
   设计/需求文本里出现该路径（或能确定推出该路径）的原文依据；给不出证据的
   条目 → kind="manual"。宁缺毋滥。
3. path 必须以 / 开头，禁止 scheme/host（断言只打被测应用本机）。
4. expect.status 用**列表**（如创建类 [200, 201]）；body_contains 保守少写——
   响应形状是应用自定的，写错即误杀。
5. 每个 req_id 必须回指真实存在的条目 id；一个条目可对应多条断言，
   推导不出任何可执行断言的条目输出一条 kind="manual" 记录。
6. 断言相互独立：避免对同一唯一资源重复写入；读断言不得依赖某条写断言先执行
   （断言执行顺序不作保证）。
7. 只输出 JSON 数组本体，不要输出其他文字。"""


def build_assertion_generation_prompt(
    requirement_items: list[dict[str, Any]],
    design_context: str = "",
) -> str:
    """requirement items + 设计上下文文本 → 断言生成 prompt（纯文本，不调模型）。

    纪律内嵌：只为可无鉴权验证的条目产 http_probe，其余标 manual；绝不臆造
    不存在的 API 路径——每条断言须给 evidence（引用设计/需求里的路径证据）。
    """
    lines: list[str] = []
    for item in requirement_items or []:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("id", "") or "").strip()
        text = str(item.get("text", "") or "").strip()
        if not rid or not text:
            continue
        quote = str(item.get("source_quote", "") or "").strip()
        suffix = f"（原文依据: {quote[:200]}）" if quote else ""
        lines.append(f"- [{rid}] {text}{suffix}")
    items_block = "\n".join(lines) if lines else "（无条目）"
    ctx = (design_context or "").strip() or "（无额外设计上下文——更要严守纪律2，宁标 manual）"
    return _ASSERTION_PROMPT_TEMPLATE.format(
        items_block=items_block, design_context=ctx)


# ═══════════════════ 执行片段生成器（spec → curl 命令片段） ═══════════════════

def assertion_to_probe_cmd(spec: dict[str, Any], port: int | str) -> str:
    """单条可执行断言 → 自包含 curl 片段（供 build_smoke_script assert 段拼接）。

    - fail-closed：kind != "http_probe" 或 auth != "none" → ValueError（阶段内
      绝不为 manual 项生成可执行命令）；
    - shell 注入安全：全部动态值 shlex.quote；body_json 经 base64 写临时文件再
      --data @file（防引号地狱，worker/sandbox.py base64 管道回写同族先例）；
    - 脚本只收割证据（`__ACCEPT_RESULT__`/`__ACCEPT_BODY__` 标记 + body 头部
      截断 base64），expect 判定在 Python 侧 `evaluate_probe_result`；
    - curl 连接失败时 -w 输出 000，标记仍完整（infra≠断言失败，判定侧兜底）。
    """
    if not isinstance(spec, dict) or spec.get("kind") != "http_probe":
        raise ValueError(f"仅 kind=http_probe 可生成执行片段，得到 {spec.get('kind')!r}"
                         if isinstance(spec, dict) else "spec 必须是 dict")
    if spec.get("auth") != "none":
        raise ValueError(
            f"auth={spec.get('auth')!r} != 'none'：fail-closed，不生成执行片段（标 manual）")
    try:
        port_num = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"port 非法: {port!r}") from exc
    if isinstance(port, bool) or not (1 <= port_num <= 65535):
        raise ValueError(f"port 越界: {port!r}")

    spec_id = str(spec["id"])
    if not _ID_RE.match(spec_id) or "__" in spec_id:
        raise ValueError(f"断言 id 非法（未过 validate_assertions？）: {spec_id!r}")
    request = spec.get("request") or {}
    method = str(request.get("method", "GET")).upper()
    path = _validate_path(request.get("path"))
    if path is None:
        raise ValueError(f"path 非法（未过 validate_assertions？）: {request.get('path')!r}")

    url = f"http://127.0.0.1:{port_num}{path}"
    body_file = f".swarm_accept_{spec_id}.body"
    payload_file = f".swarm_accept_{spec_id}.payload"

    headers = dict(request.get("headers") or {})
    body_json = request.get("body_json")
    lines: list[str] = []
    # F6：HEAD 用 --head（curl 语义正确关闭响应体读取）而非 -X HEAD——后者只改
    # 请求行动词、curl 仍等 body 直到 --max-time 干等超时（000 误判面之一）。
    method_flag = "--head" if method == "HEAD" else f"-X {method}"
    curl_parts: list[str] = [
        "ACCEPT_CODE=\"$(curl -s -o", shlex.quote(body_file),
        "-w '%{http_code}'", method_flag,
        f"--max-time {DEFAULT_PROBE_MAX_TIME_SEC}",
    ]
    if body_json is not None:
        payload = json.dumps(body_json, ensure_ascii=False)
        b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        lines.append(
            f"printf %s {shlex.quote(b64)} | base64 -d > {shlex.quote(payload_file)}")
        if not any(k.lower() == "content-type" for k in headers):
            headers["Content-Type"] = "application/json"
        curl_parts.append(f"--data @{shlex.quote(payload_file)}")
    for k, v in headers.items():
        curl_parts.append(f"-H {shlex.quote(f'{k}: {v}')}")
    curl_parts.append(shlex.quote(url))
    curl_parts.append(")\"")
    lines.append(" ".join(curl_parts))
    lines.append('ACCEPT_CODE="${ACCEPT_CODE:-000}"')
    lines.append(f'echo "{MARK_ACCEPT_RESULT}{spec_id}__${{ACCEPT_CODE}}"')
    lines.append(
        f'echo "{MARK_ACCEPT_BODY}{spec_id}__$(head -c {ACCEPT_BODY_HEAD_BYTES} '
        f'{shlex.quote(body_file)} 2>/dev/null | base64 | tr -d \'\\n\')"')
    return "\n".join(lines)


def parse_probe_output(output: str) -> dict[str, dict[str, Any]]:
    """沙箱 stdout → {断言id: {http_code: int|None, body_text: str}}（纯函数）。

    b64 解码失败 → body_text=""（降级不抛）；同 id 多次出现取首次。
    F9（回显注入面）：首个标记即占位——真标记 b64 为空串也占位（body_text=""），
    后到的同 id 行（应用日志/回显里伪造的标记行）一律不覆盖。旧口径
    `if entry["body_text"]` 对"真标记空 body + 伪造行带内容"会被伪造行接管。
    """
    out = output or ""
    parsed: dict[str, dict[str, Any]] = {}
    seen_code: set[str] = set()
    seen_body: set[str] = set()
    for spec_id, code in _RESULT_MARK_RE.findall(out):
        parsed.setdefault(spec_id, {"http_code": None, "body_text": ""})
        if spec_id in seen_code:
            continue
        seen_code.add(spec_id)
        parsed[spec_id]["http_code"] = int(code)
    for spec_id, b64 in _BODY_MARK_RE.findall(out):
        entry = parsed.setdefault(spec_id, {"http_code": None, "body_text": ""})
        if spec_id in seen_body:
            continue  # 首标记占位（含空 b64），后到伪造行不覆盖
        seen_body.add(spec_id)
        if b64:
            try:
                entry["body_text"] = base64.b64decode(
                    b64, validate=True).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 — 证据坏了降级空串，绝不抛
                entry["body_text"] = ""
    return parsed


# ═══════════════════════ 判定纯函数 ═══════════════════════

def evaluate_probe_result(
    spec: dict[str, Any],
    http_code: Any,
    body_text: str | None,
) -> dict[str, Any]:
    """单条断言证据 → {passed, reason}（纯函数，脚本侧不做判定）。三值：

    - True=结论性通过；False=结论性失败（拿到真实 HTTP 应答且不符期待）；
    - None=inconclusive（F6：无有效 HTTP 应答——curl 000/连接失败/超时是 infra
      不确定形态，infra≠断言失败，绝不冤枉成确定性 False；docstring 承诺兑现）。
    fail-closed：kind != http_probe / auth != "none" 的 spec 绝不判 pass
    （manual 项不该被执行，误喂进来也不给假绿）。
    """
    if not isinstance(spec, dict) or spec.get("kind") != "http_probe":
        return {"passed": False,
                "reason": f"kind={spec.get('kind') if isinstance(spec, dict) else None!r} "
                          "非 http_probe，不可自动判定（manual）"}
    if spec.get("auth") != "none":
        return {"passed": False,
                "reason": f"auth={spec.get('auth')!r} != 'none'，阶段内不自动判定（manual）"}
    try:
        code = int(http_code)
    except (TypeError, ValueError):
        code = 0
    if code <= 0:
        return {"passed": None,
                "reason": f"未得到有效 HTTP 应答（http_code={http_code!r}，连接失败/超时"
                          "——infra 不确定，非断言失败）"}

    expect = spec.get("expect") or {}
    expected_status = list(expect.get("status") or [])
    if code not in expected_status:
        return {"passed": False,
                "reason": f"HTTP 状态不符：实得 {code}，期待 {expected_status}"}
    body = body_text or ""
    for needle in expect.get("body_contains") or []:
        if needle not in body:
            return {"passed": False,
                    "reason": f"body_contains 未命中：期待包含 {needle!r}（body 头部截断内未见）"}
    parts = [f"HTTP {code} ∈ {expected_status}"]
    if expect.get("body_contains"):
        parts.append(f"body_contains {len(expect['body_contains'])} 项全命中")
    return {"passed": True, "reason": "；".join(parts)}
