"""X-M1（27 号文 B-5 BuildDriver 验收臂）：非 Maven 验收命令归一栈驱动层。

治前（27 号文 X-M1 实测）：`_reactorize_verify_command` 只归一 Maven——
  · `cd <子项目> && ./gradlew test`（wrapper 只在工程根 → 127）
  · workspace 根裸 `npm test`（根 package.json 无该 script → Missing script）
  · go.work 根裸 `go test ./...`（根无 go.mod → 目录不在任何模块内）
全原样返回 → 与 round65e8 同型的假阴性烧正确代码，换栈复发。

本文件锁：①gradle cd+wrapper 形归一（注册表证据=settings include，非注册/带选项/
traversal 全原样）；②npm 根裸 script 形（根无+锚点有才改，锚点=改动文件反查，
绝不枚举成员猜主包）；③go.work 根裸 go test 形（根无 go.mod+锚点模块才改）；
④接线锁（`_reactorize_verify_command` 入口真分派，Maven 臂既有测试锁字节不动）；
⑤驱动注册表单一事实源 + 驱动异常原样放行（绝不炸 L1 主链）。
"""
from __future__ import annotations

import json
import logging

from swarm.worker.l1_pipeline import _reactorize_verify_command
from swarm.worker.l1_verify_drivers import (
    _VERIFY_DRIVERS,
    normalize_verify_command,
)

# ── 夹具 ─────────────────────────────────────────────────────────────────────

def _mk_gradle(tmp_path, settings_name="settings.gradle",
               settings_text="include ':app'\ninclude ':lib:core'\n"):
    (tmp_path / settings_name).write_text(settings_text)
    return str(tmp_path)


def _mk_npm(tmp_path, root_scripts=(), member="packages/web", member_scripts=("test",)):
    (tmp_path / "package.json").write_text(json.dumps(
        {"workspaces": ["packages/*"],
         "scripts": {s: "echo x" for s in root_scripts}}))
    d = tmp_path / member
    d.mkdir(parents=True)
    (d / "package.json").write_text(json.dumps(
        {"scripts": {s: "echo y" for s in member_scripts}}))
    return str(tmp_path), f"{member}/src/index.js"


def _mk_go(tmp_path, module="svc"):
    (tmp_path / "go.work").write_text("go 1.22\n\nuse (\n\t./svc\n)\n")
    d = tmp_path / module
    d.mkdir(parents=True)
    (d / "go.mod").write_text("module example.com/svc\n\ngo 1.22\n")
    return str(tmp_path), f"{module}/main.go"


# ── ① gradle：cd <注册子项目> && ./gradlew <纯任务> → 根 :proj:task ─────────────

def test_gradle_cd_wrapper_rewritten(tmp_path):
    proj = _mk_gradle(tmp_path)
    out = _reactorize_verify_command("cd app && ./gradlew test", proj, [])
    assert out == "./gradlew :app:test", out
    # 多任务逐个挂前缀；嵌套工程路径 lib:core
    out = _reactorize_verify_command("cd lib/core && ./gradlew test build", proj, [])
    assert out == "./gradlew :lib:core:test :lib:core:build", out


def test_gradle_cd_wrapper_kts_settings(tmp_path):
    proj = _mk_gradle(tmp_path, "settings.gradle.kts", 'include(":app")\n')
    assert _reactorize_verify_command("cd app && ./gradlew test", proj, []) \
        == "./gradlew :app:test"


def test_gradle_untouched_shapes(tmp_path):
    """带选项/非注册目录/traversal/无 cd/复合 → 一律原样（绝不臆改）。"""
    proj = _mk_gradle(tmp_path)
    for cmd in ("cd app && ./gradlew test -q",          # 选项不臆改
                "cd app && ./gradlew :app:test",         # 已带工程路径
                "cd tools && ./gradlew test",            # 非注册子项目
                "cd ../app && ./gradlew test",           # traversal
                "./gradlew test",                        # 根裸跑本就合法
                "cd app && ./gradlew test && echo ok",   # 复合
                "cd app && gradle test"):                # 系统 gradle 在子目录能跑
        assert _reactorize_verify_command(cmd, proj, []) == cmd, f"应原样：{cmd!r}"


# ── ② npm：根裸 script，根无+锚点有 → --prefix ───────────────────────────────

def test_npm_bare_script_rewritten(tmp_path):
    proj, mod = _mk_npm(tmp_path)
    out = _reactorize_verify_command("npm test", proj, [mod])
    assert out == "npm --prefix packages/web run test", out
    out = _reactorize_verify_command("npm run lint", proj, [mod])
    assert out == "npm run lint", out  # 锚点包也没有 lint → 原样


def test_npm_untouched_shapes(tmp_path):
    proj, mod = _mk_npm(tmp_path, root_scripts=("test",))
    # 根有该 script → 裸跑本就合法
    assert _reactorize_verify_command("npm test", proj, [mod]) == "npm test"


def test_npm_untouched_shapes_2(tmp_path):
    proj, mod = _mk_npm(tmp_path)
    for cmd in ("npm run",                      # 无 script 名
                "npm test -- --grep x",          # 带额外参数
                "npm ci",                        # 非 test/run 形
                "cd packages/web && npm test"):  # 带 cd（旧行为锁不变）
        assert _reactorize_verify_command(cmd, proj, [mod]) == cmd, f"应原样：{cmd!r}"
    # pl_basis 空 → 锚点不定 → 原样
    assert _reactorize_verify_command("npm test", proj, []) == "npm test"


def test_npm_corrupt_root_manifest_untouched(tmp_path, caplog):
    (tmp_path / "package.json").write_text('{"workspaces": [')
    d = tmp_path / "packages" / "web"
    d.mkdir(parents=True)
    (d / "package.json").write_text(json.dumps({"scripts": {"test": "x"}}))
    with caplog.at_level(logging.WARNING):
        out = _reactorize_verify_command("npm test", str(tmp_path), ["packages/web/a.js"])
    assert out == "npm test", "根清单不可解析=「不确定」→ 原样（fail-closed，绝不臆改）"
    # ★R1 hunter F4★ 「证据坏了」与「形态不适用」分档：损坏必须 WARNING 可辨
    assert any("JSON 解析失败" in r.getMessage() for r in caplog.records), caplog.text


# ── ③ go：go.work 根（无根 go.mod）裸 go test ./... → cd 锚点模块 ────────────

def test_go_workspace_root_test_rewritten(tmp_path):
    proj, mod = _mk_go(tmp_path)
    out = _reactorize_verify_command("go test ./...", proj, [mod])
    assert out == "cd svc && go test ./...", out
    out = _reactorize_verify_command("go test -race ./...", proj, [mod])
    assert out == "cd svc && go test -race ./...", out


def test_go_value_flags_rewritten_verbatim(tmp_path):
    """★R1 reviewer F-1 锁★ 空格取值 flag（-run/-bench/-timeout/-count 等生产常见
    形态）同样归一，且原命令【逐字】保留（不臆排 flag 顺序/间距）。"""
    proj, mod = _mk_go(tmp_path)
    for bare in ("go test -run TestX ./...",
                 "go test -race -run TestX ./...",
                 "go test -bench BenchmarkX -benchtime 2s ./...",
                 "go test -timeout 30s -count 1 ./...",
                 "go test -tags integration -v ./...",
                 "go test -coverprofile=c.out -covermode=atomic ./..."):
        out = _reactorize_verify_command(bare, proj, [mod])
        assert out == f"cd svc && {bare}", f"{bare!r} 实得 {out!r}"


def test_go_quoted_value_flags_rewritten_verbatim(tmp_path):
    """★R2 reviewer F-5 锁★ 带引号空格的取值（-ldflags/-tags/-gcflags 生产常见
    写法）shlex 分词后正确归一，输出仍逐字保留原命令（含引号）。"""
    proj, mod = _mk_go(tmp_path)
    for bare in ("go test -ldflags '-X main.v=1' ./...",
                 'go test -tags "integration e2e" ./...',
                 'go test -gcflags="all=-N -l" ./...'):
        out = _reactorize_verify_command(bare, proj, [mod])
        assert out == f"cd svc && {bare}", f"{bare!r} 实得 {out!r}"
    # 引号不配对=畸形 → 原样（绝不臆改）
    bad = "go test -tags 'unclosed ./..."
    assert _reactorize_verify_command(bad, proj, [mod]) == bad


def test_go_unidentifiable_flags_untouched(tmp_path):
    """未知 bare flag / -args / 非 flag token / 取值缺值 → 原样（fail-closed，
    绝不臆判 flag 语义错位改写）。"""
    proj, mod = _mk_go(tmp_path)
    for cmd in ("go test -somefutureflag ./...",      # 未知 bare flag
                "go test -args ./...",                 # -args 后交测试二进制
                "go test -v -args -test.run X ./...",  # 同上
                "go test foo ./...",                   # 包模式不臆改
                "go test -run ./..."):                 # 取值缺值（go 自身会报错）
        assert _reactorize_verify_command(cmd, proj, [mod]) == cmd, f"应原样：{cmd!r}"


def test_go_root_has_gomod_untouched(tmp_path):
    """根有 go.mod → 单模块工程顺手带 go.work，裸跑本就合法。"""
    (tmp_path / "go.work").write_text("go 1.22\n\nuse ./svc\n")
    (tmp_path / "go.mod").write_text("module example.com/root\n")
    d = tmp_path / "svc"
    d.mkdir()
    (d / "go.mod").write_text("module example.com/svc\n")
    assert _reactorize_verify_command(
        "go test ./...", str(tmp_path), ["svc/main.go"]) == "go test ./..."


def test_go_untouched_shapes(tmp_path):
    proj, _ = _mk_go(tmp_path)
    # 锚点不定（pl_basis 空）→ 原样；非 go test 形 → 原样
    assert _reactorize_verify_command("go test ./...", proj, []) == "go test ./..."
    assert _reactorize_verify_command("go build ./...", proj, []) == "go build ./..."


# ── ④ 接线锁：入口真分派（Maven 臂由 test_r65e8 系列锁字节不动）────────────────

def test_entry_dispatches_non_maven_to_drivers(tmp_path):
    """★硬检查②★ 断「被接上了」：把 l1_verify_drivers 整块摘掉，本测试必须红——
    它走的就是生产入口 `_reactorize_verify_command`（而非直接调驱动函数）。"""
    proj, mod = _mk_npm(tmp_path)
    assert _reactorize_verify_command("npm test", proj, [mod]).startswith("npm --prefix"), \
        "入口未分派到 npm 驱动（机制存在≠接线覆盖）"


def test_entry_really_delegates_to_driver_registry(monkeypatch, tmp_path):
    """★R1 hunter F3★ 证「被接上了」而非「实现正确」：注册表换成哨兵驱动，
    生产入口必须返回哨兵值（若入口内联实现/绕开注册表，本测试红）。"""
    import swarm.worker.l1_verify_drivers as vd

    class _Sentinel:
        name = "sentinel"

        def try_normalize(self, command, *a, **k):
            return f"SENTINEL::{command}"

    monkeypatch.setattr(vd, "_VERIFY_DRIVERS", (_Sentinel(),))
    out = _reactorize_verify_command("npm test", str(tmp_path), [])
    assert out == "SENTINEL::npm test", out


# ── ⑤ 注册表单一事实源 + 异常原样放行 ─────────────────────────────────────────

def test_driver_registry_is_single_source():
    names = {d.name for d in _VERIFY_DRIVERS}
    assert names == {"gradle_cd_wrapper", "npm_bare_script", "go_workspace_test"}, names


def test_driver_exception_fails_open_to_original(monkeypatch, caplog):
    """驱动内部故障=「不确定」→ 原样 + WARNING（缺席可辨），绝不炸 L1 主链。"""
    import swarm.worker.l1_verify_drivers as vd

    class _Boom:
        name = "boom"

        def try_normalize(self, *a, **k):
            raise RuntimeError("爆炸")

    monkeypatch.setattr(vd, "_VERIFY_DRIVERS", (_Boom(),))
    with caplog.at_level(logging.WARNING):
        out = normalize_verify_command("npm test", "/nonexistent", [], None)
    assert out == "npm test"
    assert any("boom" in r.getMessage() for r in caplog.records)
