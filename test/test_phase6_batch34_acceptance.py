"""阶段6 批3+批4（登记册 §五/§七）：验收有牙（D8①②③/D6）+ 抽取预处理（F4/F5/F7/F9）。"""

from __future__ import annotations


def test_d8_1_bearer_assertion_generates_auth_header(monkeypatch):
    from swarm.brain.acceptance_spec import assertion_to_probe_cmd
    spec = {"id": "acc-1", "req_id": "req-00000001", "kind": "http_probe",
            "auth": "bearer",
            "request": {"method": "GET", "path": "/api/user/list"},
            "expect": {"status": [200]}}
    cmd = assertion_to_probe_cmd(spec, 8080)
    assert 'Authorization: Bearer ${SWARM_SMOKE_TOKEN}' in cmd, (
        "auth!=none 断言全 manual 永不执行=鉴权系统行为验证结构性为零；"
        "bearer 断言必须带登录 token 头")


def test_d8_1_login_cmd_env_gated_and_no_token_echo(monkeypatch):
    from swarm.brain.acceptance_spec import smoke_login_cmd
    monkeypatch.delenv("SWARM_SMOKE_LOGIN_PATH", raising=False)
    assert smoke_login_cmd() is None, "未配置=不启用（bearer 照旧 manual 语义）"
    monkeypatch.setenv("SWARM_SMOKE_LOGIN_PATH", "/api/login")
    monkeypatch.setenv("SWARM_SMOKE_LOGIN_BODY_JSON", '{"u":"e2e","p":"x"}')
    cmd = smoke_login_cmd()
    assert cmd and "SWARM_SMOKE_TOKEN" in cmd
    assert "${SWARM_SMOKE_TOKEN:+" not in cmd and "echo ok || echo empty" in cmd, (
        "绝不 echo token 本体（敏感值不入冒烟输出/日志）")


def test_d8_2_assertion_cap_bucketed_by_req():
    from swarm.brain.acceptance_spec import MAX_ASSERTIONS, validate_assertions
    req_items = [{"id": f"req-{i:08x}", "text": f"需求{i}"} for i in range(3)]
    items = []
    n = 0
    for rid in sorted(r["id"] for r in req_items):
        for _ in range(20):  # 每 req 20 条，共 60 > 帽 30
            items.append({"id": f"acc-{n:04d}", "req_id": rid, "kind": "manual"})
            n += 1
    valid, rejected = validate_assertions(items, req_items)
    assert len(valid) == MAX_ASSERTIONS
    per_req = {}
    for it in valid:
        per_req[it["req_id"]] = per_req.get(it["req_id"], 0) + 1
    assert min(per_req.values()) >= MAX_ASSERTIONS // 3 - 1, (
        f"到达序硬截会把晚到 req 的断言整批丢弃（验收面偏斜）——分桶轮转必须均衡: {per_req}")
    assert len(per_req) == 3, "每个被断言的 req 至少保底"


def test_f4_cjk_budget_weighting():
    from swarm.brain.ingest import _budget_chars_for
    ascii_budget = _budget_chars_for("a" * 20000, 1000)
    cjk_budget = _budget_chars_for("需" * 20000, 1000)
    assert ascii_budget == 4000
    assert cjk_budget < ascii_budget, (
        "CJK ~1.5 char/token——按 4 估算把中文 PRD 预算高估 ~2.7 倍（欠切爆预算）")
    assert 1400 <= cjk_budget <= 1600


def test_f7_key_files_include_jvm_manifests(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    from swarm.project.preprocess import _build_analysis_input
    info = _build_analysis_input(str(tmp_path), {"files": []})
    assert "pom.xml" in (info.get("key_files") or []), (
        "内联表缺 pom.xml/build.gradle/csproj——Maven/Gradle/.NET 探不到核心清单")


def test_f9_driver_dispatch_unknown_stack_loud_noop():
    from swarm.brain.nodes.maven_repair import inject_missing_deps_for_stack
    out = inject_missing_deps_for_stack({"build_system": "cargo"}, "/tmp/x", {}, {})
    assert out == {}, "未覆盖栈=显式 no-op（loud 留痕），绝不误跑 Maven 逻辑"


def test_f9_driver_dispatch_maven_routes(monkeypatch):
    import swarm.brain.nodes.maven_repair as mr
    called = {}
    def _drv(p, g, r):
        called["hit"] = True
        return {"ok": 1}
    monkeypatch.setitem(mr._DEP_REPAIR_DRIVERS, "maven", _drv)
    out = mr.inject_missing_deps_for_stack({"build_system": "maven"}, "/tmp/x", {}, {})
    assert called.get("hit") and out == {"ok": 1}
