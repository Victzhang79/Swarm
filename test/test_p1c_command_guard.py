"""P1-C 回归：run_command 规范化/守卫——堵住本地小模型烧预算的无效命令。

996db614 实测：模型反射性敲 `mvn compile <module>`（无效生命周期，必报 Unknown lifecycle
phase）+ 在烤源沙箱（无 .git）狂敲 `git diff/log`（128/129）→ 白烧几十步 → 喂大 900s 超时。
"""
from swarm.tools.build_tools import (
    _normalize_maven_module_command,
    _guard_unhelpful_command,
)


# ── mvn 模块语法误用 → 改写为正确 -pl 形式 ──

def test_normalize_mvn_compile_module():
    assert _normalize_maven_module_command("mvn compile ruoyi-alarm") == (
        "mvn -pl ruoyi-alarm -am compile", True)


def test_normalize_keeps_skipflags():
    out, changed = _normalize_maven_module_command("mvn compile ruoyi-alarm -DskipTests")
    assert changed
    assert out.startswith("mvn -pl ruoyi-alarm -am") and "-DskipTests" in out and "compile" in out


def test_normalize_noop_on_correct_and_normal():
    # 已正确（含 -pl）→ 不动
    assert _normalize_maven_module_command("mvn -pl ruoyi-alarm -am compile")[1] is False
    # 正常多阶段 → 不动
    assert _normalize_maven_module_command("mvn clean compile")[1] is False
    assert _normalize_maven_module_command("mvn compile")[1] is False
    # -f 单 pom → 不动
    assert _normalize_maven_module_command("mvn -f ruoyi-alarm/pom.xml compile")[1] is False
    # 带 goal(冒号) → 不动
    assert _normalize_maven_module_command("mvn versions:set-property -Dx=y")[1] is False
    # 非 mvn → 不动
    assert _normalize_maven_module_command("python -m pytest")[1] is False


# ── git 查看类 → 拦截返回提示（不执行） ──

def test_guard_blocks_git_inspection():
    for c in ("git diff HEAD", "git log --oneline", "git status", "git show", "git rev-parse HEAD"):
        msg = _guard_unhelpful_command(c)
        assert msg is not None and "git" in msg


def test_guard_allows_non_git_and_git_ops():
    assert _guard_unhelpful_command("mvn -pl ruoyi-alarm -am compile") is None
    assert _guard_unhelpful_command("ls -la") is None
    # git apply / add 不在拦截子集（真正写操作，虽然 worker 通常用不到）
    assert _guard_unhelpful_command("git apply x.patch") is None
