"""27 号文 X-C1：Maven 命令改写的裸子串判据——**当前就在打死用 wrapper 的工程**。

不是多栈问题，是 Maven 侧的活 bug：`./mvnw` 是 Spring Boot 生态默认形态，
RuoYi 基线恰好不用 wrapper，所以这条从未暴露。

★不对称铁证★：兄弟函数 `_reactorize_verify_command` 早已显式挡掉 mvnw，其注释更是明写
"_scope 的裸 .replace 会破坏语法（放大既有缺陷）"——**知道病灶在哪，却只在自己这半边绕开**。
治法不是再加一处守卫，是把判据与改写收敛成单一事实源，三个调用面共用。
"""
from __future__ import annotations

import pytest

from swarm.worker.l1_pipeline import _sub_mvn_token, is_plain_mvn_command


# ══════════════════════════════════════════════
# 判据：什么是"可安全改写的裸 mvn 调用"
# ══════════════════════════════════════════════

@pytest.mark.parametrize("cmd", [
    "mvn -q compile",
    "cd m && mvn test",
    "MAVEN_OPTS=-Xmx1g mvn -q compile",
    "/usr/bin/mvn -q compile",              # 带路径的合法调用：替换只动 mvn 词元
    'bash -lc "mvn -q compile"',
])
def test_plain_mvn_is_rewritable(cmd):
    """真正的 mvn 调用必须仍能被模块收窄——治本不能把有效面也关掉。"""
    assert is_plain_mvn_command(cmd)


@pytest.mark.parametrize("cmd,why", [
    ("./mvnw -q compile", "Maven wrapper：它不是 mvn，替换必毁"),
    ("mvnw -q test", "wrapper 无路径前缀"),
    ("sh mvnw verify", "wrapper 经 sh 调用"),
    ("npm run build:mvn", "脚本名里嵌 mvn（前置冒号）"),
    ("yarn mvn:check", "任务名里嵌 mvn（后置冒号）"),
    ("mvn.cmd -q compile", "Windows 批处理（后置点）"),
    ("TOOL=mvn compile", "赋值右值，不是要执行 mvn"),
    ("docker run mvn-builder compile", "镜像名里嵌 mvn（后置连字符）"),
    ("mvn -q clean && mvn -q compile", "多个 mvn 词元：count=1 会改错那一个"),
    # ★这一条是 _MVNW_RE 在本函数里的【唯一】判别面（突变检验实证）★
    # 词元正则本身已能挡 `./mvnw`（w 是 \w，后瞻直接不成立），所以单看 wrapper 命令
    # 删掉 mvnw 守卫测试也不会红。真正只有它能挡的是**混合命令**：词元计数=1 通过，
    # 但另一半是 wrapper——此时该工程明显以 wrapper 为准，裸 mvn 未必装/未必同版本，
    # 收窄谁都是猜。fail-closed：原样。
    ("mvn -q clean && ./mvnw -q test", "混合：一半裸 mvn 一半 wrapper，收窄谁都是猜"),
])
def test_non_plain_mvn_is_left_alone(cmd, why):
    """★原样返回比改烂强得多★
    改烂是 exit 127 → `_is_infra_failure` 判 True → BLOCKED **无限退避、每轮同死、
    零诊断线索** → 烧穿子任务预算后 abandon 连坐；不改只是少一层模块收窄。"""
    assert not is_plain_mvn_command(cmd), why


def test_the_exact_corruption_that_started_this():
    """★原实现的实测输出（每一条都会让命令不存在或参数畸形）★
        ./mvnw -q compile       → ./mvn -pl m -amw -q compile   ← 命令不存在 + 垃圾参数
        sh mvnw verify          → sh mvn -pl m -amw verify
        npm run build:mvn       → npm run build:mvn -pl m -am
        docker run mvn-builder  → docker run mvn -pl m -am-builder
    """
    for cmd in ("./mvnw -q compile", "sh mvnw verify",
                "npm run build:mvn", "docker run mvn-builder compile"):
        assert cmd.replace("mvn", "mvn -pl m -am", 1) != cmd, "旧口径确实会改写它"
        assert not is_plain_mvn_command(cmd), "新口径必须挡住"


# ══════════════════════════════════════════════
# 分档：两条判据问的是两个问题，后果不同 → 契约不同
# ══════════════════════════════════════════════

@pytest.mark.parametrize("cmd", [
    "./mvnw -q compile -pl mod-a",
    "mvnw compile -pl mod-a",
    "sh mvnw verify -pl mod-a",
    "mvn -q clean && mvn compile -pl mod-a",   # 多词元：不可替换词元，但补 -am 安全
])
def test_am_repair_survives_when_token_rewrite_is_unsafe(cmd):
    """★本文件曾把这条【回归】写成期望值——比没测试更坏（对抗复核 HIGH）★

    `_ensure_reactor_am` 只做 `-pl <targets>` → `-pl <targets> -am`，**不碰命令头**，
    对 wrapper 完全安全；而 `is_plain_mvn_command` 问的是"能不能替换 mvn 词元"。
    拿后者门控前者 = 复用了单一事实源、却没复用其消费契约 → wrapper 工程丢掉 -am
    → 解析不到 reactor 兄弟 `com.<proj>:*` → **假阴性烧正确代码**（round65e10 st-1
    死因① 原样复活），且恰好砸在这个 patch 本要拯救的人群头上。

    判据分档后：`is_plain_mvn_command` 仍为 False（不改写词元），`-am` 照补。
    """
    from swarm.worker.l1_pipeline import _ensure_reactor_am
    assert not is_plain_mvn_command(cmd), "前提：这些形态确实不可安全替换词元"
    out = _ensure_reactor_am(cmd)
    assert " -am" in out, f"-am 补齐必须与词元可改写性解耦，实得 {out!r}"
    assert out.startswith(cmd.split(" -pl", 1)[0]), "命令头必须逐字不动"


def test_scope_routes_wrapper_pl_commands_to_am_repair(tmp_path):
    """★分档必须在 `_scope_maven_command` 的【编排】里也成立，不只在 `_ensure_reactor_am` 里★

    突变实验：把 `_scope` 里"先 -pl 后 is_plain 门"的顺序删掉 → `_ensure_reactor_am`
    本身仍是对的，但 wrapper 命令根本走不到它（is_plain 门先把它原样弹回）→ -am 照样丢。
    判据正确 ≠ 编排正确，两处都要有锁。
    """
    from swarm.worker import l1_pipeline as L
    (tmp_path / "pom.xml").write_text(
        "<project><modules><module>mod-a</module></modules></project>")
    out = L._scope_maven_command("./mvnw compile -pl mod-a", str(tmp_path),
                                 ["mod-a/src/A.java"])
    assert out == "./mvnw compile -pl mod-a -am", out


def test_maven_gates_reach_wrapper_projects(tmp_path, monkeypatch):
    """★R58-2 parent 字面量闸 + R56-5 依赖合法性闸不得因命令写法而整类缺席★

    旧门控 `build_cmd.lstrip().startswith("mvn")` 对 `./mvnw …`（Spring Boot 生态默认
    形态）恒 False → wrapper 工程**少两道确定性闸**且零留痕。两道闸都只读改工程 pom、
    与命令怎么写无关，唯一正确的门控是"这是不是一次 Maven 构建"。
    """
    from swarm.types import FileScope, SubTask, SubTaskDifficulty
    from swarm.worker import l1_pipeline as L

    (tmp_path / "pom.xml").write_text("<project/>")
    (tmp_path / "A.java").write_text("class A {}")
    called: list[str] = []
    monkeypatch.setattr(L, "_enforce_parent_version_literals",
                        lambda *a, **k: (called.append("parent"), (0, []))[1])
    monkeypatch.setattr(L, "_enforce_dep_legality",
                        lambda *a, **k: (called.append("deplegality"), (0, []))[1])
    monkeypatch.setattr(L, "_run_l1_command", lambda *a, **k: (0, ""))
    monkeypatch.setattr(L, "_build_cmd_applicable", lambda *a, **k: True)

    # ★wrapper 形态★ 从确定性派生口注入（brain 未下发 build_command 时走这条）。
    # 顺带记一笔：`_derive_full_build_command` 目前**从不产 `./mvnw`**，哪怕工程根就有
    # mvnw——那是 B-5 BuildDriver 的活，不在本批范围内。
    monkeypatch.setattr(L, "_derive_full_build_command",
                        lambda *a, **k: "./mvnw -q compile")

    st = SubTask(id="st-1", description="x", difficulty=SubTaskDifficulty.MEDIUM,
                 scope=FileScope(writable=["A.java"]),
                 acceptance_criteria=["ok"])
    diff = ("diff --git a/A.java b/A.java\n--- a/A.java\n+++ b/A.java\n"
            "@@ -1 +1,2 @@\n class A {}\n+// x\n")
    L.run_l1_pipeline(str(tmp_path), st, diff, timeout=5)
    assert "parent" in called and "deplegality" in called, (
        f"wrapper 工程必须同样过两道 Maven 确定性闸，实得 {called}")


def test_am_repair_still_respects_its_own_boundaries():
    """对照面：别把分档做成"什么都补"——非 Maven / 无 -pl / 已 -am / 非 upstream 目标仍原样。"""
    from swarm.worker.l1_pipeline import _ensure_reactor_am
    for cmd in (
        "npm run build -pl x",                    # 非 Maven 系
        "./mvnw -q compile",                      # 无 -pl
        "./mvnw compile -pl mod-a -am",           # 已 -am
        "./mvnw validate -pl mod-a",              # validate 不需上游产物（守 P0-B 不连坐）
    ):
        assert _ensure_reactor_am(cmd) == cmd, cmd


def test_maven_family_covers_wrapper_but_token_judgement_does_not():
    """两档判据的分界线本身（改坏任一档，下面必红）。"""
    from swarm.worker.l1_pipeline import is_maven_family_command
    assert is_maven_family_command("./mvnw -q compile")
    assert not is_plain_mvn_command("./mvnw -q compile")
    assert is_maven_family_command("cd m && mvn test")
    # 两档都必须挡住非 Maven 命令——分档不是放宽
    for cmd in ("npm run build:mvn", "go build ./...", "docker run mvn-builder x"):
        assert not is_maven_family_command(cmd), cmd
        assert not is_plain_mvn_command(cmd), cmd


# ══════════════════════════════════════════════
# 改写：词边界感知
# ══════════════════════════════════════════════

@pytest.mark.parametrize("cmd,expect", [
    ("mvn -q compile", "mvn -pl m -am -q compile"),
    ("cd m && mvn test", "cd m && mvn -pl m -am test"),
    ("/usr/bin/mvn -q compile", "/usr/bin/mvn -pl m -am -q compile"),
    # ★以下四条是本组【唯一】能区分词元替换与裸 .replace 的夹具（复核 HIGH-2 实证）★
    # 上面三条里第一个 `mvn` 子串恰好就是词元，两种实现输出逐字相同 —— 把
    # `_sub_mvn_token` 退回 `command.replace("mvn", ..., 1)`（病灶本尊）全组仍绿。
    # 判据（纪律 10②）：把被测机制整块改坏，这条测试会不会红。下面这些会。
    ("/opt/mvn-3.9.6/bin/mvn -q compile", "/opt/mvn-3.9.6/bin/mvn -pl m -am -q compile"),
    ("cd build-mvn-out && mvn test", "cd build-mvn-out && mvn -pl m -am test"),
    ("docker exec mvn-ctr mvn -q compile", "docker exec mvn-ctr mvn -pl m -am -q compile"),
    ("TOOL=mvnx mvn compile", "TOOL=mvnx mvn -pl m -am compile"),
])
def test_rewrite_targets_the_token_only(cmd, expect):
    assert _sub_mvn_token(cmd, "mvn -pl m -am") == expect
    # 这些形态确实会走到改写（否则夹具是空转的）
    assert is_plain_mvn_command(cmd)


# ══════════════════════════════════════════════
# 三个调用面同源（治本的核心：不再各自维护一套判据）
# ══════════════════════════════════════════════

def test_all_three_call_sites_share_one_judgement(tmp_path):
    """★病灶不是"少了一处守卫"，是"判据有两套"★
    `_scope_maven_command`（主路径 build+test）、`_ensure_reactor_am`、
    `_reactorize_verify_command`（verify）此前各用各的：前两个裸子串 `"mvn" in`，
    第三个自建 `\\bmvnw\\b` 守卫——于是同一条 wrapper 命令在 verify 面被挡住、
    在 build 面被改烂。

    ★行为级★（本文件前两版都是文本扫描，被自己的解释性注释坑红——
      注释里写着病灶原文，`'replace("mvn"' not in src` 必然失败。
      文本从来不是行为，这正是"禁结构焊死测试"要防的。）
    三个函数喂同一条 wrapper 命令，必须都原样返回。
    """
    from swarm.worker import l1_pipeline as L
    (tmp_path / "pom.xml").write_text(
        "<project><modules><module>mod-a</module></modules></project>")
    (tmp_path / "mod-a").mkdir()
    (tmp_path / "mod-a" / "pom.xml").write_text("<project/>")

    wrapper = "./mvnw -q compile"
    assert L._scope_maven_command(wrapper, str(tmp_path), ["mod-a/src/A.java"]) == wrapper
    # ★第三面必须打在 cd-branch 上（复核 MEDIUM-1）★
    # `cd <mod> && ./mvnw …` 走的是**既有的 cd 守卫**就 return 了，压根不经 X-C1 的判据——
    # 拿它当"第三面已收敛"的证据是空头承诺（突变实验：把 reactorize 的守卫整块删掉仍全绿）。
    # cd-branch 真正的判据面在下面这条。
    assert L._reactorize_verify_command(
        "cd mod-a && ./mvnw -q test", str(tmp_path),
        ["mod-a/src/A.java"]) == "cd mod-a && ./mvnw -q test"


@pytest.mark.parametrize("cmd", [
    "cd mod-a && mvn.cmd -q compile",      # Windows 批处理
    "cd mod-a && mvn:check compile",       # 任务名嵌 mvn
    "cd mod-a && mvn-wrapper compile",     # 自建包装脚本
])
def test_cd_branch_never_silently_drops_the_cd_prefix(cmd, tmp_path):
    """★复核 HIGH-1 的回归锁：第四处自建判据（`\\bmvn\\b` 计数）造出的静默错命令★

    `\\bmvn\\b` 认 `mvn.cmd`/`mvn:check`/`mvn-wrapper` 是完整词，`_MVN_TOKEN_RE` 不认。
    旧码据前者判"可改写"进了改写分支，`_sub_mvn_token` 却 no-op → 函数返回**剥掉 cd 的
    `_rest`**：`cd mod-a && mvn.cmd -q compile` → `mvn.cmd -q compile`，作用域从模块目录
    悄悄变成工程根，日志还宣称"reactor 归一"成功。

    ★这比原病灶更毒★：原病灶产 `./mvn … -amw` 会 127 炸出来；这个"看起来合法"。
    不可改写 → **原样**（含 cd 前缀），是本条唯一可接受的结果。
    """
    from swarm.worker import l1_pipeline as L
    (tmp_path / "pom.xml").write_text(
        "<project><modules><module>mod-a</module></modules></project>")
    (tmp_path / "mod-a").mkdir()
    (tmp_path / "mod-a" / "pom.xml").write_text("<project/>")

    out = L._reactorize_verify_command(cmd, str(tmp_path), ["mod-a/src/A.java"])
    assert out == cmd, f"不可改写的命令必须原样返回，实得 {out!r}"
    assert out.startswith("cd "), "绝不允许静默剥掉 cd 前缀（作用域会从模块目录跳到工程根）"


def test_cd_branch_still_reactorizes_the_real_thing(tmp_path):
    """对照面：cd-branch 的正常归一必须还在（别把 HIGH-1 修成"整条分支关掉"）。"""
    from swarm.worker import l1_pipeline as L
    (tmp_path / "pom.xml").write_text(
        "<project><modules><module>mod-a</module></modules></project>")
    (tmp_path / "mod-a").mkdir()
    (tmp_path / "mod-a" / "pom.xml").write_text("<project/>")

    out = L._reactorize_verify_command(
        "cd mod-a && mvn -q test", str(tmp_path), ["mod-a/src/A.java"])
    assert out == "mvn -pl mod-a -am -q test", out


def test_scope_skip_is_machine_readable():
    """★降级必须有机读账，不能只有自由文本（复核 MEDIUM-2）★

    多词元复合命令被跳过收窄后，整 reactor 缺兄弟模块源码 → L1 假阴性失败，
    而此前日志里一个字都没有——"看起来正常地失败"比"改烂后 127"更难诊断。
    """
    from swarm.worker import l1_pipeline as L
    details: dict = {}
    cmd = "mvn clean && mvn -q compile"
    assert L._scope_maven_command(cmd, "/nonexistent", ["a/A.java"],
                                  details=details, phase="build") == cmd
    rec = details.get("maven_scope_skipped") or []
    assert rec and rec[0]["reason"] == "multi_token" and rec[0]["phase"] == "build", details


def test_plain_mvn_still_gets_scoped(tmp_path):
    """对照面：真 mvn 命令必须仍被收窄——否则"治好了"其实是把功能关掉了。"""
    from swarm.worker import l1_pipeline as L
    (tmp_path / "pom.xml").write_text(
        "<project><modules><module>mod-a</module></modules></project>")
    (tmp_path / "mod-a").mkdir()
    (tmp_path / "mod-a" / "pom.xml").write_text("<project/>")

    out = L._scope_maven_command("mvn -q compile", str(tmp_path), ["mod-a/src/A.java"])
    assert "-pl mod-a" in out and out.startswith("mvn ")


def test_wrapper_skip_is_observable():
    """降级必须留痕（纪律 3）——wrapper 工程拿不到模块收窄这件事要能被看见，
    否则"为什么这个工程编译比别人慢/失败"没有线索。

    ★不用 caplog★（本仓既有范式，见 test_merge_apply_check_base_tree_round29）：
    生产 `setup_logging` 置 propagate=False，且全套件里有测试调 `logging.disable`——
    单跑绿、乱序红。直挂 handler 到目标 logger 并复位 disable，对二者免疫。
    """
    import logging

    from swarm.worker import l1_pipeline as L

    logging.disable(logging.NOTSET)
    lg = logging.getLogger(L.__name__)
    seen: list[str] = []

    class _H(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    h = _H(level=logging.INFO)
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(logging.INFO)
    try:
        L._scope_maven_command("./mvnw -q compile", "/nonexistent", ["a/A.java"])
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)

    assert any("wrapper" in m.lower() for m in seen), seen
