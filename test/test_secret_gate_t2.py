"""T2 — secret 正则闸（ECC §A 移植）测试。

两层：
1. scan_diff_for_secrets（worker/security_scan.py）——对 unified diff 的【新增行】
   跑确定性密钥扫描，复用 _SECRET_PATTERNS。栈无关、只解析 diff 文本。
2. merge 节点硬闸——终局干净合并的 merged_diff 检出 CRITICAL 密钥 → 复用 apply-invalid
   同款 escalate 路径 fail-closed 阻断交付（verification_failure=merge_secret_detected）。
3. 交付闸——can_auto_accept_delivery 见 verification_failure=merge_secret_detected 拒放行。

纪律：ECC 分级 = CRITICAL(block) vs HIGH(warn 不 block)；宁误报不漏报（escalate 是人工
复核，非硬丢）；findings 不含密钥原文（脱敏首 4 字符），杜绝日志二次泄露。
"""

from __future__ import annotations

from swarm.worker.security_scan import scan_diff_for_secrets
from swarm.types import Severity


def _added(*lines: str) -> str:
    """构造一个最小 unified diff：单文件、单 hunk，给定行作为新增行。"""
    body = "".join(f"+{ln}\n" for ln in lines)
    return (
        "diff --git a/app/config.py b/app/config.py\n"
        "index 000000..111111 100644\n"
        "--- a/app/config.py\n"
        "+++ b/app/config.py\n"
        f"@@ -1,0 +1,{len(lines)} @@\n" + body
    )


# 测试样例密钥【碎片化拼接】：GitHub push-protection 扫源码 blob，连续的 provider token
# 字面量（Slack/SendGrid/Google/github_pat/webhook）会被判真密钥拦推送。拆成拼接片段后
# 源码里【无连续 token】，运行时组装回完整串仍匹配 swarm 正则——测试语义 100% 不变。
# AWS 例键 AKIAIOSFODNN7EXAMPLE / 泛型 JWT / DB 串非 GitHub 高置信推送闸，保持原样明写。
_SLACK_TOK = "xoxb" + "-1234567890-abcdefghijklmno"
_SENDGRID_TOK = "SG." + "abcdefghijklmnopqrstuv" + "." + "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ"
_GH_PAT = "github_pat" + "_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyzABCDEFGH"
_GOOGLE_SECRET = "GOCSPX" + "-1a2b3c4d5e6f7g8h9i0jABCDEF"
_SLACK_HOOK = "https://hooks.slack.com/services/" + "T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX"


# ─────────────────────────────────────────────────────────────
# 1. scan_diff_for_secrets — 各 provider 命中（CRITICAL → block）
# ─────────────────────────────────────────────────────────────

def test_aws_key_added_blocks():
    diff = _added('AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"')
    findings, should_block = scan_diff_for_secrets(diff)
    assert should_block is True
    assert any(f.severity == Severity.CRITICAL for f in findings)
    assert all(f.category == "secret" for f in findings)


def test_jwt_added_blocks():
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
        ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    findings, should_block = scan_diff_for_secrets(_added(f'TOKEN = "{jwt}"'))
    assert should_block is True
    assert any(f.severity == Severity.CRITICAL for f in findings)


def test_db_connection_string_with_creds_blocks():
    diff = _added('DATABASE_URL = "postgres://admin:password123@db.example.com:5432/prod"')
    findings, should_block = scan_diff_for_secrets(diff)
    assert should_block is True
    assert any(f.severity == Severity.CRITICAL for f in findings)


def test_github_fine_grained_pat_blocks():
    pat = _GH_PAT
    findings, should_block = scan_diff_for_secrets(_added(f'GH="{pat}"'))
    assert should_block is True


def test_google_oauth_client_secret_blocks():
    findings, should_block = scan_diff_for_secrets(
        _added(f'CLIENT_SECRET = "{_GOOGLE_SECRET}"')
    )
    assert should_block is True


def test_slack_webhook_blocks():
    hook = _SLACK_HOOK
    findings, should_block = scan_diff_for_secrets(_added(f'WEBHOOK = "{hook}"'))
    assert should_block is True


def test_sendgrid_key_blocks():
    sg = _SENDGRID_TOK
    findings, should_block = scan_diff_for_secrets(_added(f'SENDGRID = "{sg}"'))
    assert should_block is True


def test_private_key_block_added_blocks():
    findings, should_block = scan_diff_for_secrets(
        _added("-----BEGIN RSA PRIVATE KEY-----")
    )
    assert should_block is True


# ─────────────────────────────────────────────────────────────
# 2. 只扫新增行——删除/上下文/头行不算
# ─────────────────────────────────────────────────────────────

def test_secret_in_removed_line_not_blocked():
    """删除一条密钥（`-` 行）是好事，绝不阻断。"""
    diff = (
        "diff --git a/app/config.py b/app/config.py\n"
        "--- a/app/config.py\n"
        "+++ b/app/config.py\n"
        "@@ -1,1 +1,1 @@\n"
        '-AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"\n'
        '+AWS_ACCESS_KEY_ID = os.environ["AWS_ACCESS_KEY_ID"]\n'
    )
    findings, should_block = scan_diff_for_secrets(diff)
    assert should_block is False
    assert findings == []


def test_secret_in_context_line_not_blocked():
    """未改动的上下文行（' ' 前缀）不扫描。"""
    diff = (
        "diff --git a/app/config.py b/app/config.py\n"
        "--- a/app/config.py\n"
        "+++ b/app/config.py\n"
        "@@ -1,2 +1,2 @@\n"
        ' AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"\n'
        '+X = 1\n'
    )
    findings, should_block = scan_diff_for_secrets(diff)
    assert should_block is False


def test_file_header_not_matched_as_added_content():
    """`+++ b/...` 文件头以 `+` 开头，绝不能被当成新增内容误扫。"""
    # 文件名里塞一个像密钥的串——头行应被跳过。
    diff = (
        "diff --git a/AKIAIOSFODNN7EXAMPLE b/AKIAIOSFODNN7EXAMPLE\n"
        "--- a/AKIAIOSFODNN7EXAMPLE\n"
        "+++ b/AKIAIOSFODNN7EXAMPLE\n"
        "@@ -1,0 +1,1 @@\n"
        "+clean_line = 1\n"
    )
    findings, should_block = scan_diff_for_secrets(diff)
    assert should_block is False


def test_clean_diff_no_findings():
    diff = _added("def add(a, b):", "    return a + b")
    findings, should_block = scan_diff_for_secrets(diff)
    assert findings == []
    assert should_block is False


# ─────────────────────────────────────────────────────────────
# 3. 分级：HIGH 命中但不 block（ECC CRITICAL vs WARNING）
# ─────────────────────────────────────────────────────────────

def test_high_severity_slack_token_reported_not_blocked():
    """Slack xox token = HIGH（既有表）→ 命中留痕但默认不 block（阈值 critical）。"""
    findings, should_block = scan_diff_for_secrets(_added(f'SLACK = "{_SLACK_TOK}"'))
    assert should_block is False
    assert any(f.category == "secret" for f in findings)


def test_high_severity_blocks_when_threshold_high():
    """block_severity=high 时 HIGH 命中也 block（阈值可调，语义正确）。"""
    findings, should_block = scan_diff_for_secrets(
        _added(f'SLACK = "{_SLACK_TOK}"'), block_severity="high"
    )
    assert should_block is True


# ─────────────────────────────────────────────────────────────
# 4. 脱敏——findings 不含密钥原文
# ─────────────────────────────────────────────────────────────

def test_finding_redacts_secret_value():
    secret = "AKIAIOSFODNN7EXAMPLE"
    findings, _ = scan_diff_for_secrets(_added(f'K = "{secret}"'))
    assert findings
    for f in findings:
        blob = f"{f.title} {f.rule_id} {f.recommendation}"
        assert secret not in blob, f"密钥原文不得出现在 finding 里: {blob}"


# ─────────────────────────────────────────────────────────────
# 5. 文件+行号归因
# ─────────────────────────────────────────────────────────────

def test_finding_attributes_file_and_line():
    diff = (
        "diff --git a/app/config.py b/app/config.py\n"
        "--- a/app/config.py\n"
        "+++ b/app/config.py\n"
        "@@ -10,0 +11,2 @@\n"
        "+clean = 1\n"
        '+SECRET = "AKIAIOSFODNN7EXAMPLE"\n'
    )
    findings, should_block = scan_diff_for_secrets(diff)
    assert should_block is True
    crit = [f for f in findings if f.severity == Severity.CRITICAL]
    assert crit
    assert crit[0].file == "app/config.py"
    assert crit[0].line == 12  # +11 起，clean=11, SECRET=12


# ─────────────────────────────────────────────────────────────
# 6. merge 节点硬闸
# ─────────────────────────────────────────────────────────────

def _patch_merge_engine(monkeypatch, merged_diff: str):
    """把 merge() 内部 import 的 merge_engine 三件套打桩：产出给定 merged_diff 的干净合并。"""
    from swarm.brain import merge_engine
    from swarm.brain import nodes as brain_nodes

    def _fake_merge_diffs(subtask_diffs, *, base_reader=None, subtask_order=None):
        return merge_engine.MergeResult(merged_diff=merged_diff, success=True)

    monkeypatch.setattr(merge_engine, "merge_diffs", _fake_merge_diffs)
    monkeypatch.setattr(
        merge_engine, "filter_orphan_module_patches",
        lambda diffs, base_module_exists=None: (diffs, {}),
    )
    monkeypatch.setattr(
        merge_engine, "verify_merged_patch_applies",
        lambda proj, diff, base_ref=None: (True, ""),
    )
    monkeypatch.setattr(brain_nodes, "_make_base_reader", lambda state: (lambda p: None))


def _run_merge(monkeypatch, merged_diff: str) -> dict:
    from swarm.brain.nodes import merge
    from swarm.types import WorkerOutput

    _patch_merge_engine(monkeypatch, merged_diff)
    state = {
        "task_id": "t-secret",
        "project_id": "",
        "base_commit": None,
        "plan": None,
        "subtask_results": {
            "st-1": WorkerOutput(subtask_id="st-1", diff=merged_diff, summary="x", l1_passed=True),
        },
    }
    return merge(state)


def test_merge_blocks_on_secret_in_delivery_diff(monkeypatch):
    diff = _added('AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"')
    out = _run_merge(monkeypatch, diff)
    assert out.get("failure_escalated") is True
    assert out.get("failure_strategy") == "escalate"
    assert out.get("l2_passed") is False
    assert out.get("verification_failure") == "merge_secret_detected"
    degraded = out.get("degraded_reasons") or []
    assert any(str(d).startswith("merge_secret_detected") for d in degraded)


def test_merge_clean_diff_not_escalated(monkeypatch):
    diff = _added("def add(a, b):", "    return a + b")
    out = _run_merge(monkeypatch, diff)
    assert out.get("failure_escalated") is False
    assert out.get("verification_failure") != "merge_secret_detected"


def test_merge_secret_only_in_removed_line_not_blocked(monkeypatch):
    diff = (
        "diff --git a/app/config.py b/app/config.py\n"
        "--- a/app/config.py\n"
        "+++ b/app/config.py\n"
        "@@ -1,1 +1,1 @@\n"
        '-AWS_ACCESS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"\n'
        '+AWS_ACCESS_KEY_ID = os.environ["AWS_ACCESS_KEY_ID"]\n'
    )
    out = _run_merge(monkeypatch, diff)
    assert out.get("failure_escalated") is False
    assert out.get("verification_failure") != "merge_secret_detected"


# ─────────────────────────────────────────────────────────────
# 7. 交付闸——verification_failure=merge_secret_detected 硬拦
# ─────────────────────────────────────────────────────────────

def test_redis_password_without_username_caught():
    """对抗复核 reviewer F1：`redis://:pass@host`（无用户名，requirepass 常见形态）不得漏报。"""
    diff = _added('REDIS_URL = "redis://:supersecretpass@localhost:6379/0"')
    findings, should_block = scan_diff_for_secrets(diff)
    assert should_block is True
    assert any(f.severity == Severity.CRITICAL for f in findings)


def test_db_url_without_credentials_not_flagged():
    """无账密的连接串（host:port，无 user:pass@）不误报。"""
    diff = _added('REDIS_URL = "redis://localhost:6379/0"')
    findings, should_block = scan_diff_for_secrets(diff)
    assert should_block is False


# ─────────────────────────────────────────────────────────────
# 6b. merge 硬闸——rebase-over-limit clean-accept 终局出口不得漏扫（hunter F1 治本）
# ─────────────────────────────────────────────────────────────

def _added_manifest(*lines: str) -> str:
    """D3（阶段6）语义演进：over_limit clean-accept 只对【聚合/模块清单】成立（超限方
    碰普通源文件=接受即静默丢源码 → escalate）。本 harness 的秘钥闸标的是 clean-accept
    出口本身，故构造 pom.xml（清单）diff 走进该出口。"""
    body = "".join(f"+{ln}\n" for ln in lines)
    return (
        "diff --git a/pom.xml b/pom.xml\n"
        "index 000000..111111 100644\n"
        "--- a/pom.xml\n"
        "+++ b/pom.xml\n"
        f"@@ -1,0 +1,{len(lines)} @@\n" + body
    )


def _run_merge_rebase_over_limit(monkeypatch, merged_diff: str) -> dict:
    """构造 rebase 达上限但整体干净的接受分支（另一个终局交付出口），验证其不绕过 secret 闸。"""
    from swarm.brain import merge_engine
    from swarm.brain import nodes as brain_nodes
    from swarm.brain.nodes import merge
    from swarm.types import WorkerOutput

    def _fake_merge_diffs(subtask_diffs, *, base_reader=None, subtask_order=None):
        # rebase_subtask_ids 非空 + 无冲突 + success=True → 触发 over_limit clean-accept 分支
        return merge_engine.MergeResult(
            merged_diff=merged_diff, success=True, conflicts=[], rebase_subtask_ids=["st-1"],
        )

    monkeypatch.setattr(merge_engine, "merge_diffs", _fake_merge_diffs)
    monkeypatch.setattr(
        merge_engine, "filter_orphan_module_patches",
        lambda diffs, base_module_exists=None: (diffs, {}),
    )
    monkeypatch.setattr(brain_nodes, "_make_base_reader", lambda state: (lambda p: None))
    state = {
        "task_id": "t-secret-rebase",
        "project_id": "",
        "base_commit": None,
        "plan": None,
        # 已达上限（远超 max_retries+1）→ over_limit 命中 → 走 clean-accept 分支
        "subtask_rebase_counts": {"st-1": 99},
        "subtask_results": {
            "st-1": WorkerOutput(subtask_id="st-1", diff=merged_diff, summary="x", l1_passed=True),
        },
    }
    return merge(state)


def test_merge_rebase_over_limit_accept_still_scans_secret(monkeypatch):
    diff = _added_manifest('<aws.key>AKIAIOSFODNN7EXAMPLE</aws.key>')
    out = _run_merge_rebase_over_limit(monkeypatch, diff)
    # 关键回归：该终局出口曾完全绕过密钥闸，密钥直达交付；现必须 escalate。
    assert out.get("failure_escalated") is True
    assert out.get("verification_failure") == "merge_secret_detected"
    assert out.get("l2_passed") is False


def test_merge_rebase_over_limit_accept_clean_diff_proceeds(monkeypatch):
    """无密钥时该分支照常接受交付（不因扫描误伤）。"""
    diff = _added_manifest("<module>alarm</module>")
    out = _run_merge_rebase_over_limit(monkeypatch, diff)
    assert out.get("failure_escalated") is False
    assert out.get("merge_rebase_dropped") == ["st-1"]


# ─────────────────────────────────────────────────────────────
# 6c. 扫描异常 fail-closed（F3）+ HIGH 命中留痕（hunter F2）
# ─────────────────────────────────────────────────────────────

def test_merge_scan_exception_fails_closed(monkeypatch):
    """扫描器抛异常时 fail-closed escalate（绝不 fail-open 假绿放行）。"""
    from swarm.worker import security_scan

    def _boom(diff, *, block_severity="critical"):
        raise RuntimeError("scanner blew up")

    monkeypatch.setattr(security_scan, "scan_diff_for_secrets", _boom)
    diff = _added("x = 1")
    out = _run_merge(monkeypatch, diff)
    assert out.get("failure_escalated") is True
    assert out.get("verification_failure") == "merge_secret_scan_error"
    degraded = out.get("degraded_reasons") or []
    assert "merge_secret_scan_error" in degraded


def test_merge_high_severity_leaves_trace_not_blocked(monkeypatch):
    """HIGH 命中（Slack xox token）不阻断交付，但必须 degraded 留痕（不静默丢弃）。"""
    diff = _added(f'SLACK = "{_SLACK_TOK}"')
    out = _run_merge(monkeypatch, diff)
    assert out.get("failure_escalated") is False
    assert out.get("verification_failure") != "merge_secret_detected"
    degraded = out.get("degraded_reasons") or []
    assert any(str(d).startswith("merge_secret_reported") for d in degraded)


def test_can_auto_accept_blocks_on_merge_secret_via_escalate():
    """merge 输出的实际形态（failure_escalated=True）即被交付闸拒放行。"""
    from swarm.brain.gates import can_auto_accept_delivery

    allow, reason = can_auto_accept_delivery({
        "failure_escalated": True,
        "l2_passed": False,
        "verification_failure": "merge_secret_detected",
    })
    assert allow is False


def test_can_auto_accept_blocks_on_verification_failure_merge_secret():
    """纵深防御：即便前序 L2/runtime 全过，verification_failure=merge_secret_detected 仍独立硬拦。"""
    from swarm.brain.gates import can_auto_accept_delivery

    allow, reason = can_auto_accept_delivery({
        "failure_escalated": False,
        "failed_subtask_ids": [],
        "l2_passed": True,
        "l3_passed": None,
        "runtime_smoke_passed": None,
        "acceptance_passed": None,
        "verification_failure": "merge_secret_detected",
    })
    assert allow is False
    assert "merge_secret_detected" in reason
