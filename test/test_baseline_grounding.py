"""专项2 · #2 假覆盖 baseline 接地核验红测试（round67 深审定案 E 类静默毒交付）。

死型：planner 把新特性谎报 baseline_covered，reason 把幻觉能力嫁接到真实文件路径
（"SysUserController 已实现 2FA"）→ 需求静默蒸发→假 DONE。T6 证据闸从不看 reason 引用的
【具体文件】、且是全库 blob 命中（token 命中无关文件即放行）→ 结构上覆盖不了 reason 接地。

接地核验两子闸（均基于 per-file 索引，非全库 blob）：
  A 路径∈基线索引：evidence_files/reason 引用的路径必须是真实基线文件（捏造路径=最强作假信号，
    fail-CLOSED REJECT）。path 是 ASCII 与 req 语言无关→纯中文 req 也走 A。
  B 符号命中 req token：该 path 的符号 blob 命中 req 判别 token（嫁接无能力文件=fail-CLOSED REJECT）。
    req 零判别 token（纯中文）→ 豁免 B（防 round37 过严误杀），但 A 已挡捏造路径。
"""
from __future__ import annotations

from swarm.brain.baseline_candidates import (
    baseline_claims_unground,
    build_baseline_file_index,
)
from swarm.brain.plan_validator import normalize_baseline_covered

# 基线索引：SysUserController 真实存在但【无 2FA 能力符号】
_SUC = "ruoyi-system/src/main/java/com/ruoyi/system/controller/SysUserController.java"
_FILES = [
    {"file_path": _SUC, "module_name": "ruoyi-system"},
    {"file_path": "ruoyi-common/src/main/java/com/ruoyi/common/utils/StringUtils.java",
     "module_name": "ruoyi-common"},
]
_SYMBOLS = [
    {"file_path": _SUC, "symbol_name": "list", "class_name": "SysUserController"},
    {"file_path": _SUC, "symbol_name": "add", "class_name": "SysUserController"},
    {"file_path": _SUC, "symbol_name": "edit", "class_name": "SysUserController"},
]


def _index():
    return build_baseline_file_index(_FILES, _SYMBOLS)


def _req(rid, text):
    return {"id": rid, "text": text}


# ── build_baseline_file_index 结构 ────────────────────────────────────────────
def test_index_has_files_and_symbols_by_file():
    idx = _index()
    assert _SUC in idx["files"]
    assert "sysusercontroller" in idx["symbols_by_file"][_SUC].lower()
    assert "list" in idx["symbols_by_file"][_SUC].lower()


def test_index_empty_inputs():
    idx = build_baseline_file_index([], [])
    assert idx["files"] == set()
    assert idx["symbols_by_file"] == {}


# ── 子闸 B：reason 嫁接无能力真文件（path∈索引但符号不含 req token）→ REJECT ────
def test_b_grafted_capability_rejected():
    bc = [{"id": "req-2fa", "reason": "SysUserController 已实现 Google 2FA 双因子认证",
           "evidence_files": [_SUC]}]
    reqs = [_req("req-2fa", "Google 2FA authenticator 双因子登录")]
    out = baseline_claims_unground(bc, reqs, _index())
    assert "req-2fa" in out, "嫁接无能力真文件未被 B 拦截"


# ── ★复核 Reviewer HIGH 整改★：子闸 B all() 语义——泛 CRUD 词不得掩护缺失判别 token ──
def test_b_generic_crud_token_does_not_mask_missing_specific():
    """req{edit,sms,otp}：文件有 edit（真实 CRUD 符号）但无 sms/otp→any() 会被 edit 掩护放行，
    all() 必须 REJECT（正是 R67-7 泛词掩护死型，本专项要堵的嫁接真实文件）。"""
    bc = [{"id": "req-sms", "reason": "SysUserController 已实现短信 edit 校验", "evidence_files": [_SUC]}]
    reqs = [_req("req-sms", "用户 edit 时需要校验短信验证码 sms otp")]
    out = baseline_claims_unground(bc, reqs, _index())
    assert "req-sms" in out, "泛 CRUD 词 edit 掩护了缺失的 sms/otp（any 语义漏洞未堵）"


def test_b_package_name_token_grounds_via_path():
    """all() 后：能力体现在【包/目录名】而非符号名的合法申报不被误杀（路径段进 blob）。"""
    go_file = "svc-user/internal/handler/user.go"
    files = [{"file_path": go_file, "module_name": "svc-user"}]
    symbols = [{"file_path": go_file, "symbol_name": "ListUsers", "class_name": ""}]
    idx = build_baseline_file_index(files, symbols)
    bc = [{"id": "req-h", "reason": "done", "evidence_files": [go_file]}]
    # "handler" 来自目录名、"listusers" 来自符号——all() 二者都须命中
    assert baseline_claims_unground(bc, [_req("req-h", "listusers handler")], idx) == [], \
        "包/目录名 token 未经路径段接地，all() 误杀合法申报"


# ── 子闸 A：reason 引用不存在路径（捏造）→ REJECT ─────────────────────────────
def test_a_fabricated_path_rejected():
    fake = "ruoyi-system/src/main/java/com/ruoyi/system/TwoFactorService.java"
    bc = [{"id": "req-2fa", "reason": "已由 TwoFactorService 实现", "evidence_files": [fake]}]
    reqs = [_req("req-2fa", "2FA authenticator")]
    out = baseline_claims_unground(bc, reqs, _index())
    assert "req-2fa" in out, "捏造路径未被 A 拦截"


# ── 合法 baseline（path∈索引 + 符号命中 req token）→ 放行 ─────────────────────
def test_grounded_claim_passes():
    bc = [{"id": "req-userlist", "reason": "SysUserController 已提供用户列表", "evidence_files": [_SUC]}]
    reqs = [_req("req-userlist", "user list add edit 用户列表增删改")]
    out = baseline_claims_unground(bc, reqs, _index())
    assert out == [], f"合法 baseline 被误杀: {out}"


# ── ★纯中文 req + 捏造路径 → A 仍 REJECT（不因纯中文豁免放水）★ ────────────────
def test_a_chinese_req_fabricated_path_still_rejected():
    fake = "ruoyi-system/src/main/java/com/ruoyi/system/SmsService.java"
    bc = [{"id": "req-sms", "reason": "已由短信服务类实现", "evidence_files": [fake]}]
    reqs = [_req("req-sms", "支持短信验证码登录")]  # 纯中文无 ASCII token
    out = baseline_claims_unground(bc, reqs, _index())
    assert "req-sms" in out, "纯中文 req 的捏造路径未被 A 拦截（A 不应因纯中文豁免）"


# ── 纯中文 req + 真实路径 + 零 ASCII token → A 过 B 豁免 → 放行（不误杀）────────
def test_chinese_req_real_path_passes():
    bc = [{"id": "req-userlist", "reason": "用户控制器已提供列表", "evidence_files": [_SUC]}]
    reqs = [_req("req-userlist", "提供用户列表查询")]  # 纯中文无判别 token
    out = baseline_claims_unground(bc, reqs, _index())
    assert out == [], f"纯中文 req + 真实路径被误杀（B 应豁免）: {out}"


# ── KB 索引不可达（空索引）→ 全豁免 fail-open（不静默由 node record_degrade）────
def test_empty_index_fails_open():
    empty = build_baseline_file_index([], [])
    bc = [{"id": "req-2fa", "reason": "捏造", "evidence_files": ["fake/Path.java"]}]
    reqs = [_req("req-2fa", "2FA")]
    assert baseline_claims_unground(bc, reqs, empty) == [], "空索引应 fail-open 全豁免"


# ── evidence_files 缺失 → 从 reason 正则提路径兜底 ─────────────────────────────
def test_reason_path_fallback_when_no_evidence_files():
    fake = "ruoyi-system/src/main/java/com/ruoyi/system/TwoFactorService.java"
    bc = [{"id": "req-2fa", "reason": f"已由 {fake} 实现 2FA"}]  # 无 evidence_files
    reqs = [_req("req-2fa", "2FA authenticator")]
    out = baseline_claims_unground(bc, reqs, _index())
    assert "req-2fa" in out, "evidence_files 缺失时未从 reason 正则兜底提捏造路径"


def test_no_path_reference_skipped():
    """无任何路径引用（纯能力口述，无 evidence_files 无路径 token）→ 跳过 A（交 T6 token 接地）。"""
    bc = [{"id": "req-x", "reason": "此能力存量已满足"}]
    reqs = [_req("req-x", "some feature 功能")]
    out = baseline_claims_unground(bc, reqs, _index())
    assert out == [], "无路径引用的申报不应被接地核验 REJECT（无路径可验，交 T6）"


# ── 多栈：Go/Python plan 同逻辑走通（无栈特判）─────────────────────────────────
def test_multistack_go_grounding():
    go_file = "svc-user/internal/handler/user.go"
    files = [{"file_path": go_file, "module_name": "svc-user"}]
    symbols = [{"file_path": go_file, "symbol_name": "ListUsers", "class_name": ""}]
    idx = build_baseline_file_index(files, symbols)
    # 捏造 Go 路径 → A REJECT
    bc_bad = [{"id": "req-g", "reason": "done", "evidence_files": ["svc-user/internal/handler/twofa.go"]}]
    assert "req-g" in baseline_claims_unground(bc_bad, [_req("req-g", "2fa totp")], idx)
    # 真实 Go 路径 + 符号命中 → 放行
    bc_ok = [{"id": "req-g", "reason": "done", "evidence_files": [go_file]}]
    assert baseline_claims_unground(bc_ok, [_req("req-g", "listusers handler")], idx) == []


# ── ★复核 Hunter#1 整改★：_baseline_file_index_for 空 fetch 必返 {}（假值）────────
async def test_file_index_for_empty_fetch_returns_falsy(monkeypatch):
    """索引未建/连接失败时 fetch 返 ([],[])→helper 必返 {}（假值），否则 node 的 `if _bl_findex:`
    误进可用分支、"有申报却索引空"的 record_degrade 永不触发（可观测漏洞）。"""
    import swarm.knowledge.service as ksvc

    async def _empty(*a, **k):
        return [], []

    monkeypatch.setattr(ksvc, "fetch_structure_inventory", _empty)
    from swarm.brain.nodes import _baseline_file_index_for
    idx = await _baseline_file_index_for({"project_id": "proj-1"})
    assert idx == {}, "空 fetch 未返 {} → node 真值判断误进可用分支（Hunter#1 漏洞）"
    assert not idx, "返回值必须为假值（与兄弟 _baseline_vocab_for 返 '' 对称）"


async def test_file_index_for_failopen_on_exception(monkeypatch):
    import swarm.knowledge.service as ksvc

    async def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(ksvc, "fetch_structure_inventory", _boom)
    from swarm.brain.nodes import _baseline_file_index_for
    assert await _baseline_file_index_for({"project_id": "proj-1"}) == {}


# ── P2-T1：normalize_baseline_covered 保留 evidence_files 字段 ─────────────────
def test_normalize_preserves_evidence_files():
    raw = [{"id": "req-1", "reason": "r", "evidence_files": [_SUC, "x/Y.java"]}]
    out = normalize_baseline_covered(raw)
    assert out[0].get("evidence_files") == [_SUC, "x/Y.java"]


def test_normalize_evidence_files_absent_defaults_empty():
    out = normalize_baseline_covered([{"id": "req-1", "reason": "r"}])
    assert out[0].get("evidence_files") == []


def test_normalize_evidence_files_non_list_dropped():
    out = normalize_baseline_covered([{"id": "req-1", "reason": "r", "evidence_files": "notalist"}])
    assert out[0].get("evidence_files") == []
