"""R65E6-T1（round65e6 task 3c94e4ea 实锤）：假 baseline_covered 证据闸。

死因（三路取证 + PG checkpointer 取真 reason）：planner 把 **Google 2FA**（req-aaecf423，绑定/解绑/验证）
申报 baseline_covered、reason 逐字="RuoYi扩展版内置2FA：SysUserController…处理Google Authenticator…用户表含
secret字段"——**幻觉能力嫁接真文件**（SysUserController 真存在但无 2FA 方法；基线全库 authenticator|totp|
twofactor|2fa 符号=0）。既有覆盖闸只校验 id∈需求清单 + reason 非空 → 假申报直接过 → 2FA **静默丢出交付**。

治本（Option D，子agent 活体验证 catch 2FA 且 0 误拒）：每条 baseline_covered，extract_req_tokens(req.text)：
- 零 token（纯中文无 ASCII 标识符）→ 豁免（round37 过严教训：会误拒通知公告类合法存量申报）；
- baseline_vocab 空（KB 不可达）→ 全豁免（fail-open，绝不因索引缺失误拒真申报）；
- 有 token 且【全部】token 都不作子串出现在基线符号/文件名索引 → 无证据 → 打回（逼建子任务或改有据 reason）。
"""
from __future__ import annotations

from swarm.brain.baseline_candidates import (
    baseline_claims_missing_evidence,
    build_baseline_vocab,
    extract_evidence_tokens,
)
from swarm.brain.plan_validator import validate_requirement_coverage
from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

REQ_2FA = "req-aaecf423"
REQ_USER = "req-3d93036c"
REQ_NOTICE = "req-9a624b2b"


def _items():
    return [
        {"id": REQ_2FA, "text": "支持Google 2FA双因素认证的绑定、解绑和验证。", "kind": "functional"},
        {"id": REQ_USER, "text": "用户管理支持CRUD、状态切换、密码重置、Excel导入导出和2FA。", "kind": "functional"},
        {"id": REQ_NOTICE, "text": "通知公告支持发布、撤回、已读。", "kind": "functional"},
    ]


def _symbols():
    # 迷你基线索引：RuoYi 有 SysUser/Excel/Notice，但【无】任何 2FA/Authenticator/totp 符号
    return [
        {"file_path": "ruoyi-admin/.../SysUserController.java", "symbol_name": "resetPwd", "class_name": "SysUserController"},
        {"file_path": "ruoyi-common/.../ExcelUtil.java", "symbol_name": "importExcel", "class_name": "ExcelUtil"},
        {"file_path": "ruoyi-system/.../SysNoticeServiceImpl.java", "symbol_name": "insertNotice", "class_name": "SysNoticeServiceImpl"},
    ]


def _files():
    return [
        {"file_path": "ruoyi-admin/.../SysUserController.java", "module_name": "ruoyi-admin"},
        {"file_path": "ruoyi-common/.../ExcelUtil.java", "module_name": "ruoyi-common"},
    ]


def _st(sid, covers=None):
    return SubTask(id=sid, description="do", difficulty=SubTaskDifficulty.MEDIUM,
                   scope=FileScope(writable=["a.java"], readable=[]), covers=list(covers or []))


def _plan(*sts):
    return TaskPlan(subtasks=list(sts), parallel_groups=[[s.id] for s in sts])


# ── 复核整改：数字缩略 token（2fa）+ CamelCase 缩略证据（sso↔SingleSignOn） ──
def test_evidence_tokens_capture_digit_acronyms():
    """★复核 MEDIUM 锁★ extract_evidence_tokens 纳入 2fa（extract_req_tokens 漏它=动机 bug 换措辞复现）。"""
    assert "2fa" in extract_evidence_tokens("支持2FA双因素认证")
    assert "oauth2" in extract_evidence_tokens("OAuth2 登录")
    assert "sha512" in extract_evidence_tokens("密码用SHA512加密")
    assert extract_evidence_tokens("纯数字123 456") == []   # 纯数字无判别力


def test_2fa_without_google_still_flagged():
    """★复核 MEDIUM 锁★ req 措辞只有'2FA'（无 Google）也应判无证据（基线无 2fa 符号）。"""
    vocab = build_baseline_vocab(_files(), _symbols())
    items = [{"id": "req-x2fa", "text": "系统需支持2FA双因素认证的开启与验证。"}]
    bc = [{"id": "req-x2fa", "reason": "内置于 SysUser"}]
    assert baseline_claims_missing_evidence(bc, items, vocab) == ["req-x2fa"]


def test_acronym_matches_expanded_camelcase_name():
    """★复核 HIGH 锁★ SSO（字母缩略）应匹配存量展开命名 SingleSignOnFilter（缩略入 blob）→ 不误拒。"""
    syms = [{"file_path": "x/SingleSignOnFilter.java", "symbol_name": "doFilter", "class_name": "SingleSignOnFilter"}]
    vocab = build_baseline_vocab([], syms)
    items = [{"id": "req-sso", "text": "支持SSO单点登录集成。"}]
    bc = [{"id": "req-sso", "reason": "SingleSignOnFilter 存量"}]
    assert baseline_claims_missing_evidence(bc, items, vocab) == [], \
        f"SSO 应经 CamelCase 缩略匹配展开命名，不该误拒；vocab 片段应含 ssof"


# ── build_baseline_vocab ──
def test_build_vocab_contains_symbols_and_stems():
    vocab = build_baseline_vocab(_files(), _symbols()).lower()
    assert "sysusercontroller" in vocab      # class 名
    assert "excelutil" in vocab               # 文件 stem + class
    assert "google" not in vocab and "totp" not in vocab and "authenticator" not in vocab


# ── baseline_claims_missing_evidence（核心） ──
def test_2fa_false_claim_flagged_missing_evidence():
    """★RED 核★ Google 2FA 申报 baseline_covered，但基线索引无 google/totp/2fa 证据 → 列入无证据。"""
    vocab = build_baseline_vocab(_files(), _symbols())
    bc = [{"id": REQ_2FA, "reason": "RuoYi扩展版内置2FA：SysUserController处理Google Authenticator"}]
    missing = baseline_claims_missing_evidence(bc, _items(), vocab)
    assert REQ_2FA in missing, f"2FA 假申报应判无证据；实得: {missing}"


def test_legit_excel_claim_partial_hit_bounces_then_evidence_passes():
    """R67-T6 语义升级（round67 R67-7）：REQ_USER 文本 token={crud,excel,2fa}，excel 命中但
    2fa 零命中——旧 any 语义放行=同款"泛词掩护缺失能力"（JWT 案 token/redis 掩护 jwt）。
    新语义：无 evidence 的部分命中申报弹一轮；补 evidence 引文（基线真实标识符）实证后放行。"""
    vocab = build_baseline_vocab(_files(), _symbols())
    bc = [{"id": REQ_USER, "reason": "SysUserController + ExcelUtil"}]
    assert REQ_USER in baseline_claims_missing_evidence(bc, _items(), vocab)
    bc_ev = [{"id": REQ_USER, "reason": "SysUserController + ExcelUtil",
              "evidence": "SysUserController ExcelUtil"}]
    assert baseline_claims_missing_evidence(bc_ev, _items(), vocab) == []


def test_fabricated_evidence_rejected():
    """evidence 引文捏造（基线无 GoogleAuthFilter）→ 拒（R67-15 isFrame 死型）。"""
    vocab = build_baseline_vocab(_files(), _symbols())
    bc = [{"id": REQ_USER, "reason": "r", "evidence": "GoogleAuthFilter"}]
    assert REQ_USER in baseline_claims_missing_evidence(bc, _items(), vocab)


def test_pure_chinese_req_exempt():
    """通知公告纯中文无 ASCII token → 豁免（绝不误拒合法存量申报，round37 过严教训）。"""
    vocab = build_baseline_vocab(_files(), _symbols())
    bc = [{"id": REQ_NOTICE, "reason": "SysNotice 存量"}]
    assert baseline_claims_missing_evidence(bc, _items(), vocab) == []


def test_empty_vocab_fail_open():
    """KB 不可达/vocab 空 → 全豁免（fail-open，不因索引缺失误拒）。"""
    bc = [{"id": REQ_2FA, "reason": "x"}]
    assert baseline_claims_missing_evidence(bc, _items(), "") == []
    assert baseline_claims_missing_evidence(bc, _items(), None) == []


def test_dangling_id_not_flagged_here():
    """悬空 id（不在 requirement_items）→ 由 dangling_baseline 另管，本闸不重复判。"""
    vocab = build_baseline_vocab(_files(), _symbols())
    bc = [{"id": "req-ghost", "reason": "x"}]
    assert baseline_claims_missing_evidence(bc, _items(), vocab) == []


# ── validate_requirement_coverage 集成 ──
def test_validate_rejects_2fa_false_baseline_with_evidence_gate():
    """★集成 RED★ 传入 baseline_vocab → 假 2FA baseline_covered 令 plan 不 valid + 点名无证据。"""
    p = _plan(_st("st-1", covers=[REQ_USER]), _st("st-2", covers=[REQ_NOTICE]))
    vocab = build_baseline_vocab(_files(), _symbols())
    res = validate_requirement_coverage(
        p, _items(), baseline_covered=[{"id": REQ_2FA, "reason": "内置2FA于SysUserController"}],
        baseline_vocab=vocab)
    assert not res.valid, "假 2FA baseline_covered 应令覆盖校验失败"
    joined = "; ".join(res.issues)
    assert REQ_2FA in joined and ("证据" in joined or "零命中" in joined), \
        f"应点名 2FA 缺基线证据；实得: {res.issues}"


def test_validate_backward_compat_without_vocab():
    """不传 baseline_vocab（既有调用点/老 checkpoint/测试）→ 证据闸不启用，行为不变。"""
    p = _plan(_st("st-1", covers=[REQ_USER]), _st("st-2", covers=[REQ_NOTICE]))
    res = validate_requirement_coverage(
        p, _items(), baseline_covered=[{"id": REQ_2FA, "reason": "内置2FA"}])
    assert res.valid, f"无 vocab 时不启用证据闸（向后兼容）；实得: {res.issues}"


def test_validate_grounded_baseline_passes_with_vocab():
    """带 evidence 引文实证的 baseline_covered 在证据闸下通过（R67-T6 分层语义）。"""
    p = _plan(_st("st-1", covers=[REQ_2FA]), _st("st-2", covers=[REQ_NOTICE]))
    vocab = build_baseline_vocab(_files(), _symbols())
    res = validate_requirement_coverage(
        p, _items(), baseline_covered=[{"id": REQ_USER, "reason": "ExcelUtil 存量",
                                        "evidence": "ExcelUtil SysUserController"}],
        baseline_vocab=vocab)
    assert res.valid, f"带实证 evidence 的申报不该被误拒；实得: {res.issues}"
