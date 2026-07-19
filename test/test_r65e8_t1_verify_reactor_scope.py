"""R65E8-T1（round65e8 task b4f2fcda PARTIAL 82/124 实锤）：L1.3.5 验收命令 reactor 归一。

死因（终态 code+log 坐实）：st-3/4/5 代码本身编译通过（l1_2_compile_ok=true）且带 -am 的 reactor
构建成功（l1_2_1_build_ok=true），但 LLM 授的验收命令 `cd ruoyi-framework && mvn compile -q`
（cd 进子模块目录、裸 mvn 无 -pl/-am）解析不到 reactor 兄弟 `com.ruoyi:ruoyi-system` → 假阴性 fail
正确代码 → 烧光重试预算 → abandon → 连坐 38>阈值 31 → 计划覆灭 PARTIAL/REJECT。

根因=不对称：build_cmd/test_cmd 都过 _scope_maven_command（-pl <mod> -am 收窄），唯 verify_commands
裸跑。且 `cd <module> &&` 前缀隔离模块，连 -pl -am 都救不了。

治本 _reactorize_verify_command：检测 `cd <已注册 reactor 模块> && 裸 mvn <goal>` → 改写为工程根的
`mvn -pl <module> -am <goal>`（reactor 感知、连带上游）；无 cd 的裸 mvn 交 _scope_maven_command（与
build/test 对称）；cd 进非注册目录 / 已 scoped(-pl/-f) / 非 Maven → 原样（保守不臆改）。
"""
from __future__ import annotations

from swarm.worker.l1_pipeline import _reactorize_verify_command

_ROOT = """<project>
  <modules>
    <module>ruoyi-framework</module>
    <module>ruoyi-system</module>
    <module>ruoyi-admin</module>
  </modules>
</project>
"""


def _mkproj(tmp_path, extra=None):
    files = {"pom.xml": _ROOT,
             "ruoyi-framework/pom.xml": "<project/>",
             "ruoyi-system/pom.xml": "<project/>",
             "ruoyi-admin/pom.xml": "<project/>"}
    files.update(extra or {})
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return str(tmp_path)


# ── 核心 RED：cd 进子模块 + 裸 mvn → 改写为根级 -pl <mod> -am ──
def test_cd_module_bare_mvn_reactorized(tmp_path):
    proj = _mkproj(tmp_path)
    out = _reactorize_verify_command(
        "cd ruoyi-framework && mvn compile -q", proj,
        ["ruoyi-framework/src/main/java/com/ruoyi/framework/X.java"])
    assert out.startswith("mvn -pl ruoyi-framework -am"), (
        f"cd 子模块裸 mvn 应改写为根级 -pl <mod> -am（reactor 感知），实得 {out!r}")
    assert "cd ruoyi-framework" not in out, f"应剥掉 cd 前缀（否则 -pl 从子目录内失效），实得 {out!r}"
    assert "compile" in out and "-q" in out, f"应保留 goal 与 flags，实得 {out!r}"


def test_cd_module_trailing_slash(tmp_path):
    proj = _mkproj(tmp_path)
    out = _reactorize_verify_command(
        "cd ruoyi-framework/ && mvn -q compile", proj, ["ruoyi-framework/src/main/java/X.java"])
    assert out.startswith("mvn -pl ruoyi-framework -am"), f"尾斜杠应归一，实得 {out!r}"


def test_bare_mvn_no_cd_delegates_scope(tmp_path):
    """无 cd 的裸 mvn → 交 _scope_maven_command（与 build/test 对称补 -pl <mod> -am）。"""
    proj = _mkproj(tmp_path)
    out = _reactorize_verify_command(
        "mvn -q compile", proj, ["ruoyi-admin/src/main/java/A.java"])
    assert "-pl ruoyi-admin" in out and "-am" in out, f"裸 mvn 应经 _scope 收窄，实得 {out!r}"


# ── 保守回归护栏：不臆改 ──
def test_cd_unregistered_dir_untouched(tmp_path):
    """cd 进【非注册 reactor 模块】目录 → 原样（可能是独立工程/脚本目录，绝不臆改）。"""
    proj = _mkproj(tmp_path)
    cmd = "cd scripts/tools && mvn -q compile"
    out = _reactorize_verify_command(cmd, proj, ["ruoyi-admin/src/main/java/A.java"])
    assert out == cmd, f"cd 非注册目录应原样，实得 {out!r}"


def test_already_scoped_pl_untouched(tmp_path):
    """已含 -pl 的命令原样（已 reactor 感知，勿重复注入）。"""
    proj = _mkproj(tmp_path)
    cmd = "cd ruoyi-framework && mvn -pl ruoyi-framework -am compile"
    out = _reactorize_verify_command(cmd, proj, ["ruoyi-framework/src/main/java/X.java"])
    assert out == cmd, f"已 -pl 原样，实得 {out!r}"


def test_dash_f_scoped_untouched(tmp_path):
    """已用 -f <pom> 的命令原样（脚手架 validate 口径，勿改）。"""
    proj = _mkproj(tmp_path)
    cmd = "cd ruoyi-framework && mvn -f ruoyi-framework/pom.xml validate"
    out = _reactorize_verify_command(cmd, proj, ["ruoyi-framework/pom.xml"])
    assert out == cmd, f"已 -f 原样，实得 {out!r}"


def test_non_maven_command_untouched(tmp_path):
    """非 Maven 命令原样（不误伤 npm/pytest 等其它栈的验收命令）。"""
    proj = _mkproj(tmp_path)
    for cmd in ("cd ruoyi-admin && npm test", "pytest -q", "cd web && npm run build"):
        assert _reactorize_verify_command(cmd, proj, ["web/src/App.js"]) == cmd, f"非 Maven 应原样：{cmd}"


# ── 复核 MED/LOW 整改回归锁：极保守，绝不破坏复合命令语法 / 误映射 traversal ──
def test_compound_multi_mvn_untouched(tmp_path):
    """★复核 MED1 锁★ 复合/多 mvn 命令原样——子串替换只改第一个 mvn 会半 scope 或破坏语法。"""
    proj = _mkproj(tmp_path)
    for cmd in ("cd ruoyi-framework && mvn -q clean && mvn -q test",
                "cd ruoyi-framework && chmod +x mvnw; mvn -q compile",
                "mvn -q clean && mvn -q test"):
        assert _reactorize_verify_command(cmd, proj, ["ruoyi-framework/src/main/java/X.java"]) == cmd, \
            f"复合/多 mvn 应原样（防语法破坏）：{cmd!r}"


def test_maven_wrapper_untouched(tmp_path):
    """★复核 MED1 锁★ Maven wrapper(mvnw/./mvnw) 原样——裸 .replace('mvn') 会把 mvnw 改烂。"""
    proj = _mkproj(tmp_path)
    for cmd in ("cd ruoyi-framework && ./mvnw compile", "./mvnw -q compile", "cd ruoyi-framework && mvnw test"):
        assert _reactorize_verify_command(cmd, proj, ["ruoyi-framework/src/main/java/X.java"]) == cmd, \
            f"mvnw wrapper 应原样：{cmd!r}"


def test_noncanonical_cd_semicolon_untouched(tmp_path):
    """★复核 MED2 锁★ 非规范 cd（`;` 分隔）原样——交 _scope 会 -pl 错配已 cd 的 cwd。"""
    proj = _mkproj(tmp_path)
    cmd = "cd ruoyi-framework; mvn -q compile"
    assert _reactorize_verify_command(cmd, proj, ["ruoyi-admin/src/main/java/A.java"]) == cmd, \
        f"`;` 分隔的非规范 cd 应原样，实得改写"


def test_parent_traversal_untouched(tmp_path):
    """★复核 LOW 锁★ `cd ../mod` traversal 原样——绝不把 .. 误映射进同名 in-repo 模块。"""
    proj = _mkproj(tmp_path)
    cmd = "cd ../ruoyi-system && mvn compile"
    assert _reactorize_verify_command(cmd, proj, ["ruoyi-system/src/main/java/X.java"]) == cmd, \
        f"`..` traversal 应原样，不得映射进 ruoyi-system"


def test_nested_module_path(tmp_path):
    """cd 进多级模块路径（ruoyi-modules/ruoyi-x）→ 若注册则归一。"""
    proj = _mkproj(tmp_path, extra={
        "pom.xml": _ROOT.replace("<module>ruoyi-admin</module>",
                                 "<module>ruoyi-admin</module>\n    <module>ruoyi-modules/ruoyi-x</module>"),
        "ruoyi-modules/ruoyi-x/pom.xml": "<project/>"})
    out = _reactorize_verify_command(
        "cd ruoyi-modules/ruoyi-x && mvn compile", proj,
        ["ruoyi-modules/ruoyi-x/src/main/java/X.java"])
    assert out.startswith("mvn -pl ruoyi-modules/ruoyi-x -am"), f"多级模块应归一，实得 {out!r}"
