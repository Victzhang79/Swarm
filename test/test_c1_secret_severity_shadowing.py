"""#29 C-1 — 密钥闸「CRITICAL 被 HIGH 遮蔽」治本测试。

缺陷：`_SECRET_PATTERNS` 按【来源批次】组织而非 severity 排序（HIGH 档通用规则
#16 Generic Secret Assignment / #24 Unquoted secret assignment 排在 CRITICAL 档
provider token #17-22 / #25 之前），而两条扫描路径都 `break` 于首个匹配、注释却自称
"最强匹配" ⇒ 真 provider key 带引号写法（YAML/JSON/Java 主流）被遮蔽成 HIGH
⇒ should_block=False ⇒ MERGE 交付闸走"仅留痕不阻断"分支【放行真密钥】。

两条 break 路径：
  - scan_diff_for_secrets（MERGE 交付闸消费，brain/nodes/__init__.py）
  - _secret_builtin_regex（AUDIT 项目扫描消费）——原先【零测试覆盖】

为什么原先全绿：26 号文 S-6 新增的 6 个 CRITICAL pattern，测试只走
`scan_text_for_secrets`（该函数**没有 break**，先按 floor 过滤再 search、全档返回），
两条 break 路径从未被这些 pattern 覆盖；且夹具全是无引号形态，连 HIGH 遮蔽都不触发。
＝「机制存在 ≠ 覆盖面完整」。

测试分两层：
  A) 遮蔽回归：8 条实测被遮蔽的样本，断 severity+rule_id+should_block 三维（只断
     "有 finding"零区分力——遮蔽时同样有 finding，只是档位错）。
  B) ★顺序无关锁★：表内 21 个 CRITICAL 全覆盖，且把表【逆序】后结论必须不变。
     这一层锁的是"不依赖表内顺序"这个接线事实——13/21 个 CRITICAL 目前正确纯属
     位置偶然（它们在表位 0-12，早于 HIGH 块），一次表重排就会静默变成同款缺陷。
"""

from __future__ import annotations

import pytest

from swarm.types import Severity
from swarm.worker import security_scan as ss
from swarm.worker.security_scan import (
    _SECRET_PATTERNS,
    _secret_builtin_regex,
    scan_diff_for_secrets,
)


def _added(*lines: str) -> str:
    """最小 unified diff：单文件单 hunk，给定行作为新增行。"""
    body = "".join(f"+{ln}\n" for ln in lines)
    return (
        "diff --git a/app/config.py b/app/config.py\n"
        "index 000000..111111 100644\n"
        "--- a/app/config.py\n"
        "+++ b/app/config.py\n"
        f"@@ -1,0 +1,{len(lines)} @@\n" + body
    )


# ── 每个 CRITICAL 标签一条样本；样本【必须同时命中至少一个 HIGH pattern】，
#    否则夹具不构成"遮蔽"场景（测试会静默退化成普通"检出 critical"）。
#    该前提由 test_every_sample_actually_encodes_the_shadowing_hazard 强制。
_A = "a" * 30

# ── 样例密钥【碎片化拼接】（沿用 test_secret_gate_t2.py 既有约定）──
# ECC pre-commit 闸 + GitHub push-protection 都扫源码 blob，连续的 provider token 字面量
# 会被判真密钥拦提交/拦推送。拆成拼接片段后源码里【无连续 token】，运行时组装回完整串
# 仍逐字相同、仍匹配 swarm 正则 —— 测试语义 100% 不变（有 test_every_sample_actually_
# encodes_the_shadowing_hazard 逐条自证命中，拼错会立刻红）。
_S_OPENAI = "sk-" + "abcdefghij0123456789ABCD"
_S_AWS_ID = "AKIA" + "IOSFODNN7EXAMPLE"
_S_AWS_SECRET = "wJalrXUtnFEMI" + "/K7MDENG/bPxRfiCYEXAMPLEKEY"
_S_JWT = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
          ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
          ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV")
_S_DB_URI = "postgres://u:" + "p4ssw0rd" + "@h:5432/db"
_S_GOOGLE = "GOCSPX" + "-1a2b3c4d5e6f7g8h9i0jABCDEF"
_S_SLACK_HOOK = ("https://hooks.slack.com/services/"
                 + "T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX")
_S_MAILGUN = "key-" + "0123456789abcdef0123456789abcdef"

CRITICAL_SAMPLES: dict[str, str] = {
    "OpenAI API key": 'api_key = "' + _S_OPENAI + '"',
    "AWS Access Key ID": 'access_key = "' + _S_AWS_ID + '"',
    "AWS Secret Access Key": 'aws_secret_access_key = "' + _S_AWS_SECRET + '"',
    "GitHub PAT": 'token = "ghp_' + "0" * 36 + '"',
    "GitHub fine-grained PAT": 'token = "github_pat_' + "0" * 30 + '"',
    "GitHub OAuth/Server Token": 'token = "gho_' + "0" * 36 + '"',
    "Private Key": 'private_key = "-----BEGIN PRIVATE KEY-----"',
    "JWT": 'token = "' + _S_JWT + '"',
    "DB Connection String with Credentials": 'password = "' + _S_DB_URI + '"',
    "Google OAuth Client Secret": 'secret = "' + _S_GOOGLE + '"',
    "Slack Webhook": 'token = "' + _S_SLACK_HOOK + '"',
    "SendGrid API Key": 'api_key = "SG.' + "a" * 22 + "." + "b" * 43 + '"',
    "Mailgun API Key": 'api_key = "' + _S_MAILGUN + '"',
    # ↓ 26 号文 S-6 补齐的现代 provider key：实测就是被遮蔽的那批
    "OpenAI project key": 'api_key = "sk-proj-' + _A + '"',
    "Anthropic API key": 'secret = "' + "sk-ant-" + "api03-" + "b" * 40 + '"',
    "OpenRouter API key": 'api_key = "sk-or-v1-' + "c" * 32 + '"',
    "Groq API key": 'api_key = "gsk_' + "d" * 44 + '"',
    "HuggingFace token": 'token = "hf_' + "e" * 34 + '"',
    "xAI API key": 'api_key = "xai-' + "f" * 44 + '"',
    "Encrypted private key":
        'private_key = "-----BEGIN ENCRYPTED PRIVATE KEY-----"',
    "DB URL password param":
        "spring.datasource.url=jdbc:mysql://h/db?password=Sup3rS3cret!x",
}

# 实测在【修复前】确实被遮蔽（报 HIGH / should_block=False）的样本 —— A 层回归标的。
# 其余 13 条修复前就正确，但那是【表位偶然】（在 HIGH 块之前），由 B 层顺序无关锁兜住。
SHADOWED_BEFORE_FIX = (
    "OpenAI project key",
    "Anthropic API key",
    "OpenRouter API key",
    "Groq API key",
    "HuggingFace token",
    "xAI API key",
    "Encrypted private key",
    "DB URL password param",
)


def _rule_id(label: str) -> str:
    return f"builtin-secret-{label.lower().replace(' ', '-')}"


# ══════════════════════════════════════════════════════════
# 夹具自证：样本必须真的编码了"遮蔽"这个危害
# ══════════════════════════════════════════════════════════

def test_every_sample_actually_encodes_the_shadowing_hazard():
    """★夹具形状自证★：每条样本必须 ① 命中目标 CRITICAL ② 同时命中至少一个 HIGH。

    缺 ② 的样本根本不构成遮蔽场景 —— 测试会从"CRITICAL 不被 HIGH 遮蔽"静默退化成
    "能检出 CRITICAL"（后者现状也过）。这条前提测试是 A/B 两层区分力的承重墙。
    """
    for label, line in CRITICAL_SAMPLES.items():
        hits = [(lbl, sev) for lbl, pat, sev in _SECRET_PATTERNS if pat.search(line)]
        crit = [lbl for lbl, sev in hits if sev == Severity.CRITICAL]
        high = [lbl for lbl, sev in hits if sev == Severity.HIGH]
        assert label in crit, f"{label}: 样本未命中目标 CRITICAL pattern（夹具失效）"
        assert high, f"{label}: 样本未共命中任何 HIGH pattern ⇒ 测不到遮蔽（夹具失效）"


def test_critical_samples_cover_every_critical_pattern_in_table():
    """★覆盖面派生自单一事实源★：样本键集必须【逐字等于】表内 CRITICAL 标签集。

    手抄枚举会随表增删漂移（新增 CRITICAL pattern 无人为它写样本＝零覆盖且无信号）。
    用相等断言而非 `>=`：下界守卫恒绿（[[swarm-fix-must-reach-production]] P-C3 教训）。
    """
    table_crit = {lbl for lbl, _p, sev in _SECRET_PATTERNS if sev == Severity.CRITICAL}
    assert set(CRITICAL_SAMPLES) == table_crit, (
        f"样本集与表内 CRITICAL 集不一致：缺样本={sorted(table_crit - set(CRITICAL_SAMPLES))} "
        f"多余键={sorted(set(CRITICAL_SAMPLES) - table_crit)}"
    )


# ══════════════════════════════════════════════════════════
# A) 遮蔽回归 —— 修复前这 8 条报 HIGH / should_block=False
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize("label", SHADOWED_BEFORE_FIX)
def test_diff_path_critical_not_shadowed_by_high(label):
    """交付 diff 路径：真 provider key 带引号写法不得被 HIGH 档通用规则遮蔽。

    三维断言（只断"有 finding"零区分力——遮蔽时同样有 finding，只是档位/规则错）：
    severity=CRITICAL + rule_id=目标规则 + should_block=True（后者才是交付闸的实际判据）。
    """
    line = CRITICAL_SAMPLES[label]
    findings, should_block = scan_diff_for_secrets(_added(line))
    assert findings, f"{label}: 未检出任何 finding"
    assert len(findings) == 1, f"{label}: 一行应只报一个最强匹配，实得 {len(findings)}"
    f = findings[0]
    assert f.severity == Severity.CRITICAL, (
        f"{label}: 报成 {f.severity}（rule_id={f.rule_id}）⇒ 被 HIGH 档遮蔽"
    )
    assert f.rule_id == _rule_id(label), f"{label}: rule_id 错位 → {f.rule_id}"
    assert should_block is True, f"{label}: should_block=False ⇒ MERGE 交付闸会放行真密钥"


@pytest.mark.parametrize("label", SHADOWED_BEFORE_FIX)
def test_audit_path_critical_not_shadowed_by_high(label, tmp_path):
    """AUDIT 落盘扫描路径（_secret_builtin_regex）——原先【零测试覆盖】的那条 break。

    与 diff 路径同源缺陷、同源修复；两条都要有独立测试，否则修一条漏一条不可见。
    """
    line = CRITICAL_SAMPLES[label]
    f = tmp_path / "config.properties"
    f.write_text(line + "\n", encoding="utf-8")
    findings = _secret_builtin_regex(str(tmp_path))
    mine = [x for x in findings if x.file == "config.properties"]
    assert mine, f"{label}: AUDIT 路径未检出"
    assert len(mine) == 1, f"{label}: 一行应只报一个最强匹配，实得 {len(mine)}"
    assert mine[0].severity == Severity.CRITICAL, f"{label}: AUDIT 路径报成 {mine[0].severity}"
    assert mine[0].rule_id == _rule_id(label), f"{label}: rule_id 错位 → {mine[0].rule_id}"


# ══════════════════════════════════════════════════════════
# B) ★顺序无关锁★ —— 防未来 append/重排静默重新引入
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize("label", sorted(CRITICAL_SAMPLES))
def test_every_critical_survives_table_reversal(label, monkeypatch):
    """★把 _SECRET_PATTERNS 逆序后，21 个 CRITICAL 的结论必须【逐字不变】★

    逆序把全部 HIGH 档规则挪到全部 CRITICAL 之前 —— 在原 `break` 实现下 21 条会全红。
    锁的是"判定不依赖表内顺序"这个接线事实（非实现字面量，不违纪律 6）：13/21 个
    CRITICAL 目前正确纯属表位偶然（位于 HIGH 块之前），一次批次重排就会静默变成
    与 C-1 同款的放行缺陷。
    """
    line = CRITICAL_SAMPLES[label]

    # 前提自证：逆序确实改变了"首个匹配"是谁，否则本条锁 vacuous
    # （[[swarm-half-landed-fix-and-probe-width]] §2c：遍历型探针必须自证真的检查了）
    def _first_hit(table):
        for lbl, pat, sev in table:
            if pat.search(line):
                return lbl, sev
        return None, None

    first_fwd = _first_hit(_SECRET_PATTERNS)
    first_rev = _first_hit(list(reversed(_SECRET_PATTERNS)))
    assert first_fwd != first_rev, (
        f"{label}: 逆序未改变首个匹配（{first_fwd}）⇒ 本条顺序无关锁 vacuous，夹具需重造"
    )

    monkeypatch.setattr(ss, "_SECRET_PATTERNS", list(reversed(_SECRET_PATTERNS)))
    # 自证 monkeypatch 生效（打在 re-export 上会 vacuous 绿）
    assert ss._SECRET_PATTERNS[0][0] == _SECRET_PATTERNS[-1][0]

    findings, should_block = scan_diff_for_secrets(_added(line))
    assert findings, f"{label}: 逆序后未检出"
    assert findings[0].severity == Severity.CRITICAL, (
        f"{label}: 逆序后报成 {findings[0].severity} ⇒ 判定依赖表内顺序"
    )
    assert findings[0].rule_id == _rule_id(label), (
        f"{label}: 逆序后 rule_id 漂移 → {findings[0].rule_id}"
    )
    assert should_block is True, f"{label}: 逆序后 should_block=False"


# ══════════════════════════════════════════════════════════
# C) 反向安全性 —— 刻意 HIGH 的 FP 控制设计不得被提级
# ══════════════════════════════════════════════════════════

@pytest.mark.parametrize("line,desc", [
    ('public static final String CSRF_TOKEN = "csrf_token";', "RuoYi 基线常量名（冤杀实证本尊）"),
    ('private String password = "changeme";', "占位默认值"),
    ('api_key = "' + "your-api-key" + '-here"', "文档占位"),
    ('token: "placeholder"', "模板占位"),
    ('SECRET = "abcdefghijk"', "纯小写无结构"),
])
def test_deliberate_high_forms_are_not_promoted_to_critical(line, desc):
    """★DR-05-F5(#85) 分级契约不得被本次修复破坏★

    CRITICAL=block / HIGH=warn 是【刻意的 FP 控制设计】（对抗双复核裁定"提级 CRITICAL
    处方过激"已撤销，实证 RuoYi 基线 `CSRF_TOKEN = "csrf_token"` 会被冤杀阻断）。
    本次修的是"CRITICAL 被 HIGH 遮蔽"，绝不动任何 pattern 的档位 ——
    改共享表前先问：新消费者的后果和老消费者一样吗（[[swarm-reuse-contract-not-just-source]]）。
    """
    findings, should_block = scan_diff_for_secrets(_added(line))
    for f in findings:
        assert f.severity != Severity.CRITICAL, f"{desc}: 被提级 CRITICAL ⇒ 冤杀阻断合法基线"
    assert should_block is False, f"{desc}: should_block=True ⇒ 误阻断"


@pytest.mark.parametrize("line", [
    "password=password,",
    "api_key = resolve_credential(",
    "DB_PASSWORD=${SECRET}",
    "api_key: {{ vault_key }}",
    "# password: changeme",
])
def test_normal_code_still_not_flagged(line):
    """扫全表（不再 break）不得放大误报：正常代码形态仍然零命中。"""
    findings, should_block = scan_diff_for_secrets(_added(line))
    assert not findings, f"正常代码被误报: {line} → {[f.rule_id for f in findings]}"
    assert should_block is False


def test_scan_text_for_secrets_contract_unchanged():
    """`scan_text_for_secrets` 本就无 break（全档返回全部命中），本次修复不得改其契约。

    它是知识库入库闸 / 技能导入准入闸的消费源，语义＝"返回全部命中"而非"最强命中"
    —— 与两条 break 路径的"一行一个最强"是【不同的消费契约】，不可合并。
    """
    line = CRITICAL_SAMPLES["OpenAI project key"]
    hits = ss.scan_text_for_secrets(line)
    names = {n for n, _ in hits}
    assert "OpenAI project key" in names
    assert "Generic Secret Assignment" in names, "全档返回契约被改窄"
    assert len(hits) >= 2, "scan_text_for_secrets 应返回全部命中（非最强单条）"
