"""批 W1：知识库入库准入闸（26 号文 S-3 治本）行为测试。

病灶回顾：Swarm 自己的 `.env`（含 5 类当时有效凭据）被向量化进 Qdrant 共享集合。
根因不是某处写错，而是准入面压根不存在 + 三条通道过滤不同源（增量通道比全量还宽，
一致性修复会把全量挡掉的文件重新塞回去）。

本测试锁三件事：① 安全层拦得住（fail-closed，不依赖 git）；② 降噪层遵守 .gitignore；
③ 三条通道同源（任一通道回退到"无过滤"都必须变红）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from swarm.knowledge.ingest_guard import (
    GitignoreFilter,
    is_sensitive_filename,
    reject_reason_by_content,
    reject_reason_by_name,
)


# ── 层1·安全：敏感文件名（fail-closed，与 .gitignore 无关）──

@pytest.mark.parametrize("name", [
    ".env", ".env.local", ".env.bak-round38", "prod.env",
    "id_rsa", "id_ed25519", "deploy.pem", "app.p12", "store.jks",
    ".npmrc", ".netrc", ".git-credentials", "credentials.json",
    ".pgpass", "service-account-x.json",
    # 复核 MEDIUM 补漏：业界头号泄露源
    "kubeconfig", "terraform.tfvars", "prod.tfstate", "AuthKey_ABC.p8", "vpn.ovpn",
    "id_rsa ",   # 复核 LOW：尾随空格绕过
])
def test_sensitive_filenames_rejected(name):
    """凭据类文件必须被挡——它们正是靠"无扩展名"绕过 EXCLUDED_EXTENSIONS 的。"""
    assert reject_reason_by_name(name) == "sensitive_filename", name


@pytest.mark.parametrize("name", [
    # ★W1 复核 HIGH-3 实证的误杀面——这些是一等源码，绝不能被当凭据丢掉★
    "migrations/env.py",        # alembic 每个项目都有
    "src/env.ts", "env.d.ts", "config/env.go",
    "com/x/Credentials.java",   # Java 极常见类名
    "credentials.py",           # google-auth / boto 标准模块名
    "app/secrets.py",           # Python 标准库同名
    "com/x/Secret.java", "jwt_secret.py",
    "i18n/messages.key", "zh_CN.key",   # i18n 资源，不是私钥
    "test_env", "main.tf",
])
def test_source_files_not_misjudged_as_sensitive(name):
    """误杀比漏挡更隐蔽：拒绝后既不索引也不留痕，还会主动删掉已有向量。"""
    assert reject_reason_by_name(name) is None, f"{name} 被误判为敏感文件"


@pytest.mark.parametrize("name", [
    ".env.example", ".env.docker.example", ".env.sample", "conf/app.template",
])
def test_placeholder_samples_allowed(name):
    """样板文件是"项目怎么配置"的有效知识，放行；真值由内容闸兜底。"""
    assert reject_reason_by_name(name) is None, name


@pytest.mark.parametrize("path,reason", [
    (".claude/x.json", "hidden_dir"),          # 增量/一致性通道正是从这漏的
    (".hermes/plans/p.md", "hidden_dir"),
    (".DS_Store", "hidden_file"),
])
def test_hidden_paths_rejected(path, reason):
    assert reject_reason_by_name(path) == reason


@pytest.mark.parametrize("path", [
    "src/main/java/com/x/Foo.java", "pom.xml", ".gitignore", ".editorconfig",
    "ruoyi-admin/src/main/resources/application-druid.yml",
])
def test_normal_sources_allowed(path):
    assert reject_reason_by_name(path) is None, path


# ── 层1·安全：内容密钥扫描（复用交付闸单一事实源）──

def test_content_secret_rejected():
    hit = reject_reason_by_content(
        "conf/a.yml",
        "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
    assert hit and hit.startswith("secret_content:"), hit


def test_content_clean_allowed():
    assert reject_reason_by_content("A.java", "public class A { int x = 1; }") is None


def test_content_gate_only_blocks_critical_not_warn_only_high():
    """★W1 复核 HIGH-2★：`Generic Secret Assignment` 是 HIGH 档，DR-05-F5(#85) 对抗复核
    明确裁定它 warn-only（会把 RuoYi 基线 `CSRF_TOKEN = "csrf_token"` 常量名冤杀）。
    该表的消费契约是"MERGE 命中走 escalate 人工复核（非硬丢）"；入库闸没有人工复核环节，
    照搬全档＝把当年裁定的冤杀重新引入。故只拒 CRITICAL。"""
    csrf = 'private static final String CSRF_TOKEN = "csrf_token";'
    assert reject_reason_by_content("ShiroConstants.java", csrf) is None, \
        "HIGH 档 warn-only 模式绝不能在入库闸硬拒（冤杀实证本尊）"
    aws = "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    assert reject_reason_by_content("a.yml", aws) is not None, "CRITICAL 档必须拒"


def test_severity_filter_keeps_existing_callers_unchanged():
    """min_severity 默认 None＝全档，既有调用方（MERGE/技能导入）行为逐字不变。"""
    from swarm.worker.security_scan import scan_text_for_secrets
    csrf = 'String CSRF_TOKEN = "csrf_token";'
    assert [n for n, _ in scan_text_for_secrets(csrf)], "不传参必须仍返回 HIGH 档命中"
    assert not [n for n, _ in scan_text_for_secrets(csrf, min_severity="critical")]


def test_content_scan_failure_raises_for_retry(monkeypatch):
    """★W1 复核 C2★：扫描器不可用是【暂时故障】，必须抛出走调用方重试语义。
    初版返回拒绝原因串 → 调用方 return 0 不抛 → 不进重试队列，而 Layer A 已刷新
    时间戳 → 一致性巡检既不判 missing 也不判 stale → 该文件向量永久丢失无自愈路径。"""
    import swarm.knowledge.ingest_guard as g
    monkeypatch.setattr(g, "content_secret_hits",
                        lambda _t: (_ for _ in ()).throw(RuntimeError("scanner down")))
    with pytest.raises(g.SecretScanUnavailable):
        g.reject_reason_by_content("a.py", "x = 1")


# ── 层2·降噪：.gitignore（含嵌套仓库发现）──

import shutil

requires_git = pytest.mark.skipif(shutil.which("git") is None,
                                  reason="降噪层依赖 git；缺 git 时该层 fail-open（另有用例覆盖）")


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


@requires_git
def test_gitignore_respected_with_nested_repo(tmp_path):
    """★项目根不一定是 git 仓库根★——实测 test-1 的 root 是 ~/LLM/swarm，
    真仓库在子目录 swarm/。降噪层必须能向下发现嵌套仓库。"""
    root = tmp_path / "outer"                 # 非 git 仓库
    repo = root / "inner"                     # 真仓库在这里
    (repo / "logs").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / ".gitignore").write_text("logs/\n*.log\n")
    (repo / "logs" / "a.log").write_text("x")
    (repo / "b.log").write_text("x")
    (repo / "src" / "Main.java").write_text("class Main {}")
    _git("init", cwd=repo)

    ignored = GitignoreFilter(root).filter_ignored(
        ["inner/logs/a.log", "inner/b.log", "inner/src/Main.java"])
    assert "inner/logs/a.log" in ignored
    assert "inner/b.log" in ignored
    assert "inner/src/Main.java" not in ignored, "真源码绝不能被降噪层误挡"


def test_gitignore_fail_open_on_non_repo(tmp_path):
    """降噪层 fail-open：非 git 仓库整体放行（安全由层1 独立兜底，绝不让 git 缺席=安全缺口）。"""
    (tmp_path / "a.py").write_text("x = 1")
    assert GitignoreFilter(tmp_path).filter_ignored(["a.py"]) == set()


# ── 三通道同源 ──

def test_full_scan_channel_blocks_sensitive(tmp_path):
    """全量通道（preprocess）：敏感文件不进 file_list，且拒绝计数可观测。"""
    from swarm.project.preprocess import _scan_sync
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "A.java").write_text("class A {}")
    (tmp_path / ".env").write_text("SWARM_SECRET_KEY=abcdef0123456789")
    (tmp_path / "id_rsa").write_text("-----BEGIN OPENSSH PRIVATE KEY-----")
    out = _scan_sync(str(tmp_path))
    paths = [f["rel_path"] for f in out["files"]]
    assert ".env" not in paths and "id_rsa" not in paths
    assert "src/A.java" in paths
    assert out["ingest_rejected_by_name"] >= 2, "拒绝数必须上账（always-emit 可观测）"


@requires_git
def test_full_scan_channel_wires_gitignore(tmp_path):
    """★接线级（复核实证假绿：摘掉 _scan_sync 的 gitignore 接线，原测试全绿）★
    降噪层必须真的接进全量通道，且拒绝账要上报。"""
    from swarm.project.preprocess import _scan_sync
    (tmp_path / "logs").mkdir()
    (tmp_path / "src").mkdir()
    (tmp_path / ".gitignore").write_text("logs/\n*.log\n")
    (tmp_path / "logs" / "a.log").write_text("noise")
    (tmp_path / "b.log").write_text("noise")
    (tmp_path / "src" / "A.java").write_text("class A {}")
    _git("init", cwd=tmp_path)
    out = _scan_sync(str(tmp_path))
    paths = [f["rel_path"] for f in out["files"]]
    assert "src/A.java" in paths
    assert "logs/a.log" not in paths and "b.log" not in paths, "gitignore 层未接进全量通道"
    assert out["ingest_ignored_by_gitignore"] >= 2, "降噪账必须上报（否则无人可查）"
    # 统计三项必须同源重算（否则自相矛盾的数字会被拼进 analyze 的 LLM prompt）
    assert out["file_count"] == len(out["files"])
    assert sum(out["language_breakdown"].values()) == len(out["files"])


@requires_git
def test_consistency_channel_same_judgment(tmp_path):
    """一致性通道：必须与全量同源（**含降噪层**），否则被挡文件会被判 missing_index
    反复入队，与入库闸形成【无限重试环】——这正是"每天打满 200 预算从不收敛"的机制。"""
    from swarm.knowledge.consistency import _is_source_file
    from swarm.knowledge.ingest_guard import IngestGate
    (tmp_path / "logs").mkdir()
    (tmp_path / ".gitignore").write_text("logs/\n*.log\n")
    (tmp_path / "logs" / "a.log").write_text("noise")
    (tmp_path / ".env").write_text("K=v")
    (tmp_path / "A.java").write_text("class A {}")
    _git("init", cwd=tmp_path)
    gate = IngestGate(tmp_path)
    assert _is_source_file(tmp_path / ".env", root=tmp_path, gate=gate) is False
    assert _is_source_file(tmp_path / "logs" / "a.log", root=tmp_path, gate=gate) is False, \
        "gitignore 噪声若仍被判入库候选 → 恒 missing → 恒入队 → 恒重嵌（不收敛本体）"
    assert _is_source_file(tmp_path / "A.java", root=tmp_path, gate=gate) is True


@pytest.mark.asyncio
@requires_git
async def test_incremental_channel_gate_wired(monkeypatch, tmp_path):
    """★接线级（复核实证假绿：摘掉 updater._index_file 的闸，全部相关测试仍全绿）★
    增量通道正是审计认定"比全量还宽、准入面倒挂根源"的核心病灶通道。"""
    from swarm.knowledge.updater import ChangeType, FileChange, KnowledgeUpdater
    (tmp_path / ".gitignore").write_text("*.log\n")
    _git("init", cwd=tmp_path)

    # 刻意【不】手动设 _gate_cache：既有测试也用 __new__ 绕过 __init__，闸必须对此健壮
    # （全量闸曾因此 AttributeError 打断索引 2 例）
    up = KnowledgeUpdater.__new__(KnowledgeUpdater)
    monkeypatch.setattr("swarm.knowledge.updater._lookup_project_path",
                        lambda _pid: str(tmp_path), raising=False)
    assert up._reject_for_indexing("p1", ".env") == "sensitive_filename"
    assert up._reject_for_indexing("p1", "noise.log") == "gitignored", \
        "增量通道必须含降噪层（只有名字层＝噪声照旧回流）"
    assert up._reject_for_indexing("p1", "src/A.java") is None


@pytest.mark.asyncio
async def test_semantic_layer_is_single_chokepoint_and_self_heals(monkeypatch):
    """★Layer B 唯一收口点★：全量与增量都经 reindex_file_atomic 写向量。
    拒绝时不仅不写，还要清掉该文件【已有】的向量（断源 + 自愈存量）。"""
    from swarm.knowledge.semantic_index import SemanticIndexer
    idx = SemanticIndexer.__new__(SemanticIndexer)
    called = {"index": 0, "deleted": []}

    async def _fake_index(*a, **k):
        called["index"] += 1
        return 3

    async def _fake_delete(pid, fp):
        called["deleted"].append(fp)

    async def _fake_prune(*a, **k):
        called["pruned"] = True

    monkeypatch.setattr(idx, "index_source_file", _fake_index, raising=False)
    monkeypatch.setattr(idx, "delete_by_file", _fake_delete, raising=False)
    monkeypatch.setattr(idx, "prune_file_stale", _fake_prune, raising=False)

    n = await idx.reindex_file_atomic("p1", "SWARM_SECRET_KEY=xxxx", ".env")
    assert n == 0 and called["index"] == 0, "敏感文件绝不写向量"
    assert called["deleted"] == [".env"], "拒绝时必须清除既有向量（自愈存量）"

    # 对照面：正常源码照常走完 index + prune，闸不得误伤
    n2 = await idx.reindex_file_atomic("p1", "class A {}", "src/A.java")
    assert n2 == 3 and called["index"] == 1 and called.get("pruned") is True


# ── 收口点下沉 + 自保 backstop + 边界（W1 复核 HIGH-4/HIGH-5）──

@pytest.mark.asyncio
async def test_index_chunks_is_the_real_sink(monkeypatch):
    """★HIGH-4：内容闸必须在 index_chunks★——它才是 Layer B 真正唯一 sink。
    KB 文档采集 pipeline（POST /knowledge/ingest，吃用户上传文件）**直调 index_chunks**，
    闸设在 reindex_file_atomic 就漏掉了它。逐 chunk 判定：一个 chunk 有凭据不牵连其余。"""
    from swarm.knowledge.semantic_index import Chunk, SemanticIndexer
    idx = SemanticIndexer.__new__(SemanticIndexer)
    seen = {}

    def _client():
        raise AssertionError("不该走到 embed/upsert——本用例只验闸")

    monkeypatch.setattr(idx, "_client_or_raise", _client, raising=False)
    bad = Chunk(content="aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                chunk_type="free_text", file_path="doc/leak.md")
    # 全部 chunk 都被拒 → 直接返回 0，绝不进 embed（故不会触发上面的 AssertionError）
    assert await idx.index_chunks("p1", [bad]) == 0


def test_gitignore_backstop_refuses_to_wipe_project(tmp_path):
    """★HIGH-5：降噪层是 fail-open 层，"拿不准就不降噪"★
    若 .gitignore 判定要清掉几乎整个项目（项目位于某个忽略它的仓库内 / 写了 `*`），
    下游 _save_file_index 对空 list 是静默早返回 → 项目照常 READY、知识库全空、无人知晓。
    宁可这一轮不降噪。"""
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / ".gitignore").write_text("*\n")      # 忽略一切
    for i in range(30):
        (repo / f"f{i}.java").write_text("class A {}")
    if shutil.which("git"):
        _git("init", cwd=repo)
        ig = GitignoreFilter(repo).filter_ignored([f"f{i}.java" for i in range(30)])
        assert ig == set(), "命中率≥90% 必须整体放弃降噪，绝不清空项目"


@requires_git
def test_repo_lookup_never_escapes_project_root(tmp_path):
    """★HIGH-5：仓库归属查找严格止于项目根★
    初版终止条件是 `cur == self.root.parent` 且 .git 探测在终止判断【之前】，于是会采用
    【项目根的父目录】那层的仓库规则——项目外的 .gitignore 不该管项目内的事。"""
    outer = tmp_path / "outer"
    proj = outer / "proj"
    proj.mkdir(parents=True)
    (outer / ".gitignore").write_text("proj/\n")   # 外层仓库忽略了整个 proj
    (proj / "A.java").write_text("class A {}")
    _git("init", cwd=outer)
    ignored = GitignoreFilter(proj).filter_ignored(["A.java"])
    assert ignored == set(), "外层仓库的忽略规则不得清空本项目"
