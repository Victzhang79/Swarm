"""批次6：安全/依赖簇治本（19号文 D4-D14 + 21号文残留）的的行为级测试。

D4：scanner_ran per-category 粒度——单布尔时代任一类任一工具跑成即解除哨兵
（java 有 spotbugs 无 dependency-check → 依赖漏洞 0 覆盖照常放行）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import swarm.worker.security_scan as ss  # noqa: E402


def _only_bandit_env(monkeypatch):
    """模拟：bandit 在且跑成（干净 JSON 零发现），其余外部工具全缺。"""
    monkeypatch.setattr(
        ss.shutil, "which", lambda name: "/usr/bin/bandit" if name == "bandit" else None
    )
    monkeypatch.setattr(ss, "_run_tool", lambda *a, **k: (1, '{"results": []}', ""))


def test_d4_sast_only_dep_zero_coverage_blocks(monkeypatch, tmp_path):
    """D4 核心：sast 有工具跑成但 dep 类 0 覆盖 → 阻断模式必须 fail-closed。

    单布尔时代：bandit 一跑成 scanner_ran=True，依赖漏洞 0 覆盖被掩盖照常放行。
    """
    _only_bandit_env(monkeypatch)
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")

    findings, should_block = ss.run_security_scan(str(tmp_path), "python", block_severity="critical")

    assert should_block is True, "dep 类 0 覆盖必须 fail-closed（不得被 sast 工具掩盖）"
    dep_sentinels = [f for f in findings if f.rule_id == "fail-closed-no-dep-scanner"]
    assert len(dep_sentinels) == 1, "dep 类 0 覆盖须注独立哨兵"
    assert dep_sentinels[0].category == "dep"
    assert dep_sentinels[0].severity == ss.Severity.CRITICAL
    # sast 有覆盖 → 不得误注 sast 哨兵
    assert not any(f.rule_id == "fail-closed-no-sast-scanner" for f in findings)
    # secret 由内置正则兜底覆盖 → 不得误注 secret 哨兵
    assert not any(f.rule_id == "fail-closed-no-secret-scanner" for f in findings)


def test_d4_all_external_absent_sast_and_dep_sentinels(monkeypatch, tmp_path):
    """全外部工具缺失：sast+dep 两哨兵各一条；secret 内置兜底覆盖不注。"""
    monkeypatch.setattr(ss.shutil, "which", lambda name: None)
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")

    findings, should_block = ss.run_security_scan(str(tmp_path), "python", block_severity="critical")

    assert should_block is True
    cats = {f.category for f in findings if f.rule_id.startswith("fail-closed-no-")}
    assert cats == {"sast", "dep"}, f"哨兵应恰好覆盖 0 覆盖的类: {cats}"


def test_d4_full_coverage_no_sentinel(monkeypatch, tmp_path):
    """sast+dep 都真跑成且干净 → 无任何哨兵不阻断（secret 走内置兜底）。"""
    monkeypatch.setattr(
        ss.shutil,
        "which",
        lambda name: "/usr/bin/" + name if name in ("bandit", "pip-audit") else None,
    )
    monkeypatch.setattr(ss, "_run_tool", lambda *a, **k: (1, '{"results": []}', ""))
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")

    findings, should_block = ss.run_security_scan(str(tmp_path), "python", block_severity="critical")

    assert should_block is False
    assert not any(f.rule_id.startswith("fail-closed-no-") for f in findings)


def test_d4_builtin_regex_marks_secret_ran_not_aggregate(tmp_path):
    """内置正则兜底：置 secret_ran（对 secret 类是真实覆盖），不置聚合 scanner_ran
    （A-P0-2 原旨：不掩盖 SAST/依赖工具全缺）。"""
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")  # L-1：置位绑定实扫≥1 文件
    ctx = ss._ScanContext()
    ss._run_secret_scan(str(tmp_path), files=None, ctx=ctx)
    assert ctx.secret_ran is True
    assert ctx.scanner_ran is False, "内置兜底绝不可置聚合位（会掩盖 SAST/dep 全缺）"
    assert ctx.sast_ran is False and ctx.dep_ran is False


def test_d4_builtin_regex_zero_files_no_secret_ran(tmp_path, monkeypatch):
    """L-1（批次6 R1 hunter）：0 文件可扫绝不置 secret_ran——无条件置位会让
    secret 类 per-category 哨兵永久失效（假覆盖）。"""
    monkeypatch.setattr(ss.shutil, "which", lambda name: None)  # 排除环境恰好装了 gitleaks
    ctx = ss._ScanContext()
    ss._run_secret_scan(str(tmp_path), files=None, ctx=ctx)
    assert ctx.secret_ran is False, "空项目无可扫文件=secret 类未覆盖，哨兵必须能火"


def test_d4_report_mode_aggregate_semantics_unchanged(monkeypatch, tmp_path):
    """report-only 模式行为不变：无真实外部扫描器 → INFO coverage-zero 信号，不阻断。"""
    monkeypatch.setattr(ss.shutil, "which", lambda name: None)
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")

    findings, should_block = ss.run_security_scan(str(tmp_path), "python", block_severity="none")

    assert should_block is False
    assert not any(f.rule_id.startswith("fail-closed-no-") for f in findings)
    cov = [f for f in findings if f.rule_id == "scan-coverage-zero"]
    assert len(cov) == 1 and cov[0].severity == ss.Severity.INFO


# ─── D5：DB 连接串密码段排除占位形态 ───


def _secret_hits(text: str) -> list[str]:
    return [name for name, _frag in ss.scan_text_for_secrets(text)]


def test_d5_placeholder_password_not_flagged():
    """D5 核心：密码段为占位形态（本闸 recommendation 教的安全写法）不得命中 CRITICAL。"""
    for uri in (
        "postgres://user:${DB_PASSWORD}@db.internal:5432/app",
        "postgres://user:$DB_PASSWORD@db.internal:5432/app",
        "mysql://root:{{ db_password }}@mysql:3306/app",
        "redis://:%s@redis:6379/0",
        "postgres://user:${DB_PASSWORD:-changeme}@db:5432/app",
    ):
        assert "DB Connection String with Credentials" not in _secret_hits(uri), uri


def test_d5_real_password_still_flagged():
    """真账密连接串仍 CRITICAL 命中（含无用户名的 redis 形态回归）。"""
    for uri in (
        "postgres://user:s3cretP@ssw0rd@db.internal:5432/app",
        "redis://:requirepass123@redis:6379/0",
        "mongodb+srv://admin:Hunter2x@cluster0.example.net/db",
    ):
        assert "DB Connection String with Credentials" in _secret_hits(uri), uri


# ─── D6：OpenAI key 正则左边界 ───


def test_d6_embedded_word_prefix_not_flagged():
    """D6 核心：disk-/task- 等词干后随长字母数字 id/hash 不得内嵌命中 sk- CRITICAL。"""
    for text in (
        "device_id = 'task-a1b2c3d4e5f6a7b8c9d0e1f2'",
        "volume = 'disk-Z9Y8X7W6V5U4T3S2R1Q0P9O8N7'",
    ):
        assert "OpenAI API key" not in _secret_hits(text), text


def test_d6_real_openai_key_still_flagged():
    """真实 sk- key（引号/等号/空白等左界）仍命中。"""
    for text in (
        'API_KEY = "sk-abc123def456ghi789jkl012mno345"',
        'key=sk-abc123def456ghi789jkl012mno345',
        '"sk-ABC123DEF456GHI789JKL012MNO345"',
    ):
        assert "OpenAI API key" in _secret_hits(text), text


# ─── D7：未知/缺失 severity 缺省语义显式化（有意 FP 控制，非疏漏）───


def test_d7_all_mappers_unknown_severity_fall_back_to_named_default():
    """D7：七个 _map_*_severity 对未知/空输入统一落 _UNKNOWN_SEVERITY_DEFAULT（=MEDIUM）。

    有意性核验结论：缺省 MEDIUM=ECC 分级契约（DR-05-F5(#85) 裁定提级过激已撤销）的
    FP 控制设计；收紧走 security_block_severity 配置，绝不在映射层静默提级。
    """
    mappers = (
        ss._map_bandit_severity,
        ss._map_semgrep_severity,
        ss._map_gosec_severity,
        ss._map_spotbugs_severity,
        ss._map_pip_audit_severity,
        ss._map_npm_severity,
        ss._map_vuln_severity,
    )
    for m in mappers:
        for junk in ("", "unknown-value", "CRITICAAL"):
            assert m(junk) is ss._UNKNOWN_SEVERITY_DEFAULT, f"{m.__name__}({junk!r})"
    assert ss._UNKNOWN_SEVERITY_DEFAULT is ss.Severity.MEDIUM


def test_d7_known_severities_unaffected():
    """已知 severity 映射不变（显式化重构不得改变既有契约）。"""
    assert ss._map_bandit_severity("HIGH") is ss.Severity.HIGH
    assert ss._map_pip_audit_severity("critical") is ss.Severity.CRITICAL
    assert ss._map_npm_severity("info") is ss.Severity.INFO
    assert ss._map_spotbugs_severity("1") is ss.Severity.HIGH
    assert ss._map_vuln_severity("low") is ss.Severity.LOW


# ─── D8：SAST files 限定（工具支持处）───


def _capture_tool_cmd(monkeypatch, tool_name, json_out):
    """让指定工具"存在且跑成"，捕获实际执行命令。"""
    monkeypatch.setattr(
        ss.shutil, "which", lambda name: f"/usr/bin/{name}" if name == tool_name else None
    )
    seen: dict = {}

    def fake_run(cmd, *, cwd, timeout=120):
        seen["cmd"] = list(cmd)
        return (1, json_out, "")

    monkeypatch.setattr(ss, "_run_tool", fake_run)
    return seen


def test_d8_semgrep_honors_files(monkeypatch, tmp_path):
    """semgrep：files 提供时扫描目标=scope 文件清单，不再全树（baseline 不连坐）。"""
    seen = _capture_tool_cmd(monkeypatch, "semgrep", '{"results": []}')
    ss._sast_node(str(tmp_path), files=["src/a.js", "src/b.ts"], ctx=None)
    assert seen["cmd"][-2:] == ["src/a.js", "src/b.ts"]
    assert str(tmp_path) not in seen["cmd"]


def test_d8_semgrep_no_files_scans_tree(monkeypatch, tmp_path):
    """semgrep：files=None 保持全树（行为不变）。"""
    seen = _capture_tool_cmd(monkeypatch, "semgrep", '{"results": []}')
    ss._sast_node(str(tmp_path), files=None, ctx=None)
    assert seen["cmd"][-1] == str(tmp_path)


def test_h2_semgrep_empty_files_vacuous_skip(monkeypatch, tmp_path):
    """H-2（批次6 R1 hunter）：files=[]（空集）≠None——绝不回退全树（baseline 连坐
    误杀方向）；vacuous 覆盖置 sast_ran（哨兵不误伤），不起进程。"""
    seen = _capture_tool_cmd(monkeypatch, "semgrep", '{"results": []}')
    ctx = ss._ScanContext()
    out = ss._sast_node(str(tmp_path), files=[], ctx=ctx)
    assert out == []
    assert "cmd" not in seen, "空集回退全树=H-2 误杀源，绝不应起进程"
    assert ctx.sast_ran is True, "vacuous 覆盖必须置位——不置则哨兵把无可扫对象误报成未扫"


def test_h2_bandit_empty_files_vacuous_skip(monkeypatch, tmp_path):
    """H-2 sibling：bandit 同款（兄弟面一并捞）。"""
    seen = _capture_tool_cmd(monkeypatch, "bandit", '{"results": []}')
    ctx = ss._ScanContext()
    out = ss._sast_python(str(tmp_path), files=[], ctx=ctx)
    assert out == []
    assert "cmd" not in seen
    assert ctx.sast_ran is True


def test_d8_gosec_files_map_to_package_patterns(monkeypatch, tmp_path):
    """gosec：files 的 .go 父目录去重 → ./dir/... 包模式（gosec 不收文件清单）。"""
    seen = _capture_tool_cmd(monkeypatch, "gosec", '{"Issues": []}')
    ss._sast_go(str(tmp_path), files=["cmd/srv/main.go", "cmd/srv/util.go", "internal/x/y.go"], ctx=None)
    assert seen["cmd"][2:] == ["./cmd/srv/...", "./internal/x/..."]


def test_d8_gosec_scope_without_go_files_skips(monkeypatch, tmp_path):
    """gosec：scope 无 .go 文件=对本子任务无对象，诚实跳过（不拿全树旧账连坐）。"""
    seen = _capture_tool_cmd(monkeypatch, "gosec", '{"Issues": []}')
    out = ss._sast_go(str(tmp_path), files=["README.md", "configs/app.yaml"], ctx=None)
    assert out == []
    assert "cmd" not in seen, "无 .go 对象时绝不应起 gosec 进程"


def test_d8_gosec_no_files_scans_all(monkeypatch, tmp_path):
    """gosec：files=None 保持 ./...（行为不变）。"""
    seen = _capture_tool_cmd(monkeypatch, "gosec", '{"Issues": []}')
    ss._sast_go(str(tmp_path), files=None, ctx=None)
    assert seen["cmd"][2:] == ["./..."]


# ─── D9：dep_legality 缺命名空间独立判定（不再落入 registry 不可达 fail-open）───


def _dl():
    import swarm.worker.dep_legality as dl
    return dl


def test_d9_missing_namespace_nonmember_prunes_not_failopen():
    """D9 核心：缺 groupId + 非成员 + 本栈强制 → prune（旧码='仓库不可达' fail-open legal）。"""
    dl = _dl()
    for ver in (None, "1.2.3"):
        v, why = dl.classify(
            {"namespace": "", "name": "some-lib", "version": ver, "block": ""},
            namespace="com.ruoyi", workspace_members={"ruoyi-common"},
            managed={"some-lib"}, managed_unknown=False,
            registry_versions=lambda *_: [],  # 即便受管/有版本也救不了：解析期即崩
            namespace_mandatory=True,
        )
        assert v == "prune", (ver, why)
        assert "缺命名空间" in why


def test_d9_missing_namespace_member_fixed():
    """D9：成员缺 groupId → fix_namespace 确定性补工程命名空间（非 prune 非放行）。"""
    dl = _dl()
    v, why = dl.classify(
        {"namespace": "", "name": "ruoyi-common", "version": None, "block": ""},
        namespace="com.ruoyi", workspace_members={"ruoyi-common"},
        managed=set(), managed_unknown=False, registry_versions=lambda *_: None,
        namespace_mandatory=True,
    )
    assert v == "fix_namespace", why


def test_d9_optional_namespace_stack_keeps_failopen():
    """栈中立：namespace_mandatory=False（如 npm 无 scope 包属常态）→ 旧 fail-open 路径不变。"""
    dl = _dl()
    v, why = dl.classify(
        {"namespace": "", "name": "lodash", "version": "4.17.21", "block": ""},
        namespace=None, workspace_members={"x"}, managed=set(), managed_unknown=False,
        registry_versions=lambda *_: None,  # 缺省 False：ns 空 → vers=None → 仓库不可达 fail-open
    )
    assert v == "legal" and "仓库不可达" in why


def test_d9_enforce_end_to_end(tmp_path):
    """enforce 全链路（MavenDriver）：非成员缺 groupId 的依赖被剪除；成员缺 groupId 被补上。"""
    dl = _dl()
    pom = """<project>
  <dependencies>
    <dependency>
      <artifactId>ruoyi-common</artifactId>
    </dependency>
    <dependency>
      <artifactId>ghost-lib</artifactId>
      <version>9.9.9</version>
    </dependency>
  </dependencies>
</project>
"""
    new_texts, actions = dl.enforce(
        {"pom.xml": pom}, root_text=pom, namespace="com.ruoyi",
        workspace_members={"ruoyi-common"}, registry_versions=lambda *_: [],
        driver=dl.DRIVERS["maven"],
    )
    out = new_texts["pom.xml"]
    assert "ghost-lib" not in out, "非成员缺 groupId → 剪除"
    assert "<groupId>com.ruoyi</groupId>" in out, "成员缺 groupId → 补工程命名空间"
    assert any(a.startswith("[prune]") for a in actions)
    assert any(a.startswith("[fix_namespace]") for a in actions)


# ─── D10：fix_namespace 三面同步（硬编码版本 → 工程版本引用）───


def _d10_pom(version_line: str) -> str:
    return f"""<project>
  <dependencies>
    <dependency>
      <groupId>com.company.external</groupId>
      <artifactId>ruoyi-common</artifactId>
      {version_line}
    </dependency>
  </dependencies>
</project>
"""


def _d10_enforce(pom: str):
    dl = _dl()
    return dl.enforce(
        {"pom.xml": pom}, root_text=pom, namespace="com.ruoyi",
        workspace_members={"ruoyi-common"}, registry_versions=lambda *_: [],
        driver=dl.DRIVERS["maven"],
    )


def test_d10_fix_namespace_syncs_hardcoded_version():
    """D10 核心：成员+外部 groupId+硬编码版本 → groupId 修复同时版本同步 ${project.version}。"""
    new_texts, actions = _d10_enforce(_d10_pom("<version>1.0.0</version>"))
    out = new_texts["pom.xml"]
    assert "<groupId>com.ruoyi</groupId>" in out
    assert "<version>${project.version}</version>" in out, "臆造版本必须与 reactor 同步"
    assert "1.0.0" not in out


def test_d10_var_ref_version_untouched():
    """变量引用版本由工程自身承接，不动。"""
    new_texts, _ = _d10_enforce(_d10_pom("<version>${ruoyi.version}</version>"))
    out = new_texts["pom.xml"]
    assert "<groupId>com.ruoyi</groupId>" in out
    assert "<version>${ruoyi.version}</version>" in out


def test_d10_absent_version_untouched():
    """无版本（上游受管）→ 只修 groupId，绝不臆造版本标签。"""
    new_texts, _ = _d10_enforce(_d10_pom(""))
    out = new_texts["pom.xml"]
    assert "<groupId>com.ruoyi</groupId>" in out
    assert "<version>" not in out


# ─── D14：LOW 汇总六组 ───


def test_d14_diff_no_newline_marker_not_counted():
    """`\\ No newline at end of file` 是元数据行——不得推进新文件行号（归因不偏移）。"""
    diff = (
        "+++ b/a.txt\n"
        "@@ -1,2 +1,2 @@\n"
        " context\n"
        "+sk-abc123def456ghi789jkl012mno345\n"
        "\\ No newline at end of file\n"
        "+sk-zzz123def456ghi789jkl012mno999\n"
    )
    findings, _ = ss.scan_diff_for_secrets(diff)
    assert len(findings) == 2
    assert findings[0].line == 2 and findings[1].line == 3, \
        "无 \\ 行号偏移：第二条命中行号必须紧邻第一条"


def test_d14_clippy_only_primary_spans():
    """clippy：非 primary span（note/related）不产 finding（一条诊断一条 finding）。"""
    import json as _json
    diag = _json.dumps({
        "reason": "compiler-message",
        "message": {
            "level": "warning", "message": "this is an issue",
            "code": {"code": "clippy::foo"},
            "spans": [
                {"file_name": "src/note.rs", "line_start": 99, "is_primary": False},
                {"file_name": "src/main.rs", "line_start": 10, "is_primary": True},
                {"file_name": "src/rel.rs", "line_start": 50, "is_primary": False},
            ],
            "children": [],
        },
    })
    monkeypatch_ctx = ss._ScanContext()
    import swarm.worker.security_scan as _m
    orig_which, orig_run = _m.shutil.which, _m._run_tool
    _m.shutil.which = lambda n: "/usr/bin/cargo" if n == "cargo" else None
    _m._run_tool = lambda *a, **k: (101, diag + "\n", "")
    try:
        findings = _m._sast_rust("/tmp/x", ctx=monkeypatch_ctx)
    finally:
        _m.shutil.which, _m._run_tool = orig_which, orig_run
    assert len(findings) == 1
    assert findings[0].file == "src/main.rs" and findings[0].line == 10


def test_d14_gitleaks_report_outside_project(monkeypatch, tmp_path):
    """gitleaks 报告写项目树外临时文件（绝不污染交付 diff）。"""
    seen: dict = {}

    def fake_run(cmd, *, cwd, timeout=120):
        seen["cmd"] = list(cmd)
        rp = cmd[cmd.index("--report-path") + 1]
        Path(rp).write_text("[]", encoding="utf-8")
        return (0, "", "")

    monkeypatch.setattr(ss.shutil, "which", lambda n: "/usr/bin/gitleaks" if n == "gitleaks" else None)
    monkeypatch.setattr(ss, "_run_tool", fake_run)
    ss._secret_gitleaks(str(tmp_path), ctx=ss._ScanContext())
    rp = seen["cmd"][seen["cmd"].index("--report-path") + 1]
    assert not str(rp).startswith(str(tmp_path)), "报告路径绝不在项目树内"
    assert not Path(rp).exists(), "临时报告用后已清理"


def test_d14_py_compile_no_positional_not_rewritten():
    """cmd_normalize：无位置参数的 py_compile 不改写（旧码→无参 compileall 编译整个 sys.path）。"""
    from swarm.worker.cmd_normalize import normalize_python_cmd
    cmd = "python -m py_compile"
    out = normalize_python_cmd(cmd)
    assert "compileall" not in out and "py_compile" in out, out
    # 有目录位置参数仍改写（回归）
    assert "compileall" in normalize_python_cmd("python -m py_compile src/")


def test_d14_format_truncation_honest_skipped(tmp_path):
    """format_gate：超 50 上限的尾巴如实进 skipped（status 不谎报 ok）。"""
    from swarm.worker import format_gate as fg
    files = [f"src/f{i:03d}.py" for i in range(55)]
    for f in files:
        (tmp_path / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f).write_text("x=1\n", encoding="utf-8")
    import swarm.worker.format_gate as _fg
    monkey = _fg._which
    _fg._which = lambda name: None  # 无格式化器 → 全 skipped，只看记账
    try:
        res = fg.format_files(str(tmp_path), files)
    finally:
        _fg._which = monkey
    assert res["status"] == "skipped" and len(res["skipped"]) == 55

    # 有格式化器路径：tail 必须进 skipped
    calls: list = []
    import subprocess as _sp
    real_run = _sp.run

    def fake_run(cmd, **kw):
        calls.append(cmd)
        class R:
            returncode = 0
            stderr = ""
        return R()
    _fg._which = lambda name: "/usr/bin/ruff" if name == "ruff" else None
    fg.subprocess.run = fake_run
    try:
        res2 = fg.format_files(str(tmp_path), files)
    finally:
        fg.subprocess.run = real_run
        _fg._which = monkey
    assert len(calls) == 50
    assert res2["status"] == "partial"
    assert len(res2["skipped"]) == 5, "第 51+ 文件必须如实记 skipped"


def test_d14_kt_not_fed_to_java_formatter():
    """.kt 不映射 java（google-java-format 不支持 Kotlin，喂入=恒失败空转）。"""
    from swarm.worker.format_gate import _EXT_TO_LANG
    assert ".kt" not in _EXT_TO_LANG
    assert _EXT_TO_LANG[".java"] == "java"


def test_d14_rust_edition_from_cargo_toml(tmp_path):
    """rustfmt edition 取最近 Cargo.toml；无字段=2015；找不到=2021。"""
    from swarm.worker.format_gate import _rust_edition
    (tmp_path / "crates" / "a" / "src").mkdir(parents=True)
    (tmp_path / "crates" / "a" / "Cargo.toml").write_text(
        '[package]\nedition = "2018"\n', encoding="utf-8")
    assert _rust_edition(str(tmp_path), "crates/a/src/lib.rs") == "2018"
    (tmp_path / "crates" / "b").mkdir(parents=True)
    (tmp_path / "crates" / "b" / "Cargo.toml").write_text('[package]\nname = "b"\n', encoding="utf-8")
    assert _rust_edition(str(tmp_path), "crates/b/lib.rs") == "2015"
    assert _rust_edition(str(tmp_path), "elsewhere/x.rs") == "2021"


def test_d14_enforce_requires_driver():
    """dep_legality.enforce：driver 必填（缺省落 maven 是地雷）。"""
    dl = _dl()
    import pytest
    with pytest.raises(TypeError):
        dl.enforce({}, root_text="", namespace=None, workspace_members=set(),
                   registry_versions=lambda *_: None)


def test_d14_driver_for_absent_warns_once(caplog):
    """无 driver 的栈 → None + warn-once（零覆盖不与已校验混同）。"""
    dl = _dl()
    dl._driver_absent_warned.discard("cobol")
    import logging
    with caplog.at_level(logging.WARNING, logger="swarm.worker.dep_legality"):
        assert dl.driver_for("cobol") is None
        assert dl.driver_for("cobol") is None
    warns = [r for r in caplog.records if "cobol" in r.getMessage()]
    assert len(warns) == 1


def test_d14_plugin_deps_not_judged():
    """<build><plugins> 内 plugin 的 dependency 不参与工程依赖判定。"""
    dl = _dl()
    pom = """<project>
  <build><plugins><plugin>
    <groupId>org.apache.maven.plugins</groupId>
    <artifactId>maven-x-plugin</artifactId>
    <dependencies>
      <dependency>
        <groupId>com.ruoyi</groupId><artifactId>ghost-plugin-dep</artifactId><version>1.0</version>
      </dependency>
    </dependencies>
  </plugin></plugins></build>
  <dependencies>
    <dependency>
      <groupId>com.ruoyi</groupId><artifactId>ghost-real-dep</artifactId><version>1.0</version>
    </dependency>
  </dependencies>
</project>
"""
    new_texts, actions = dl.enforce(
        {"pom.xml": pom}, root_text=pom, namespace="com.ruoyi",
        workspace_members={"ruoyi-common"}, registry_versions=lambda *_: [],
        driver=dl.DRIVERS["maven"],
    )
    assert any("ghost-real-dep" in a for a in actions), "工程依赖照常判定"
    assert not any("ghost-plugin-dep" in a for a in actions), "plugin 依赖绝不被工程规则误剪"


def test_d14_zh_locale_javac_symbols():
    """中文 locale javac 输出同样解析出符号（机制不再静默失效）。"""
    from swarm.worker.symbol_resolver import parse_missing_symbols, parse_missing_methods
    out = parse_missing_symbols(
        "Bar.java:3: 错误: 找不到符号\n  符号:   类 FooService\n  位置: 类 com.x.Bar\n")
    assert [(m.kind, m.name) for m in out] == [("class", "FooService")]
    out2 = parse_missing_methods(
        "A.java:5: 错误: 找不到符号\n  符号:   方法 encodeToByte(byte[])\n"
        "  位置: 类 java.util.Base64.Encoder\n")
    assert out2 == [("encodeToByte", "java.util.Base64.Encoder")]
    # 英文回归
    out3 = parse_missing_methods(
        "A.java:5: error: cannot find symbol\n  symbol:   method encodeToByte(byte[])\n"
        "  location: class java.util.Base64.Encoder\n")
    assert out3 == [("encodeToByte", "java.util.Base64.Encoder")]
