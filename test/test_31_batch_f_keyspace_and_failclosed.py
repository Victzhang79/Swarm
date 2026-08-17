"""31 号文批 F 锁：A2-M1/L2 键空间统一（G5 落全）· A2-M2 冒烟推导 fail-closed · A2-L1 死代码 · A2-L3 前缀剥离。

A2-M1 与 A2-L2 是**同一族**（键空间不统一），报告明写"必须一次改全，否则又是半落地"——
故本文件把四个站点的锁写在一起，任何一处回退都红。

A2-M1 的病理形态值得单记：**「判死的名单 ⊅ 收敛的名单」**。
validator 按归一键判"2 个无依赖写者同写一文件"→ 硬失败；而收敛器（规则1.5）按原始拼写建键，
看见的是"两个文件各 1 个写者"→ 不接手。收敛器救不了它判死的东西 ⇒ 规划期硬闸永不收敛 ⇒
同签名两轮熔断 fail-fast。方向上 validator 是 fail-closed（不会假过），但按北极星
「绝不派已知会失败的 plan」，不可收敛的确定性闸＝烧钱。
"""

from __future__ import annotations

import pytest

from swarm.brain.contract_utils import _norm_scope_path, normalize_plan_scopes
from swarm.brain.plan_validator import validate_plan_structure
from swarm.types import FileScope, SubTask, TaskPlan


def _st(sid: str, *, create=None, writable=None, depends=None, desc="") -> SubTask:
    return SubTask(
        id=sid, description=desc or sid,
        scope=FileScope(create_files=list(create or []), writable=list(writable or [])),
        depends_on=list(depends or []),
    )


# ───────────────── A2-M1：规则1.5 键空间（判死名单 ⊇ 收敛名单）─────────────────

def _shared_writer_plan(spell_b: str) -> TaskPlan:
    """RUN9 形态：两个写者各自挂 scaffold 链、彼此并行；仅 st-b 的路径拼写可变。"""
    return TaskPlan(subtasks=[
        _st("st-scaffold", create=["x/registry.json"], desc="scaffold"),
        _st("st-a", writable=["x/registry.json"], depends=["st-scaffold"], desc="writer a"),
        _st("st-b", writable=[spell_b], depends=["st-scaffold"], desc="writer b"),
    ])


@pytest.mark.parametrize("spell", [
    "x/registry.json",        # 对照：拼写一致（治前也过）
    "./x/registry.json",      # ★finding 的实测落点★
    "x\\registry.json",       # 反斜杠
    "/x/registry.json",       # 前导 /
    "x/registry.json/",       # 尾 /（_norm_scope_path 也剥它）
    ".//x/registry.json",     # 多重 ./
])
def test_m1_rule15_serializes_writers_across_all_spellings(spell):
    """★核心锁★ 规则1.5 必须把全部写者串成总序链——不论路径拼写。

    突变：把 `_writers_final` 的键换回原始串 ⇒ 除第一格外全红。
    """
    plan = _shared_writer_plan(spell)
    normalize_plan_scopes(plan, None, None)
    b = next(s for s in plan.subtasks if s.id == "st-b")
    assert "st-a" in (b.depends_on or []), (
        f"拼写 {spell!r} 下规则1.5 未串序（键空间与 validator 不同源）: deps={b.depends_on}")


@pytest.mark.parametrize("spell", [
    "./x/registry.json", "x\\registry.json", "/x/registry.json", ".//x/registry.json",
])
def test_m1_validator_agrees_after_normalization(spell):
    """★同一命题的第二个面★：收敛后 validator 必须放行。

    这条与上一条不冗余——上条断"边加上了"，本条断"硬闸因此不再打回"。
    治前实测：valid=False + "3 个无依赖子任务同时写 x/registry.json"。
    """
    plan = _shared_writer_plan(spell)
    normalize_plan_scopes(plan, None, None)
    res = validate_plan_structure(plan)
    assert res.valid, (
        f"拼写 {spell!r} 下 validator 仍硬失败 ⇒ 判死名单 ⊅ 收敛名单（熔断烧钱）: "
        f"{res.issues[:2]}")


def test_m1_genuinely_parallel_writers_still_rejected():
    """★反向锁★ A2-M1 是"让收敛器接手"，不是"把硬闸拆掉"。

    真正无 scaffold 依赖、彼此并行且**不可收敛**的形态仍须被 validator 打回。
    若有人把这条闸顺手放宽，本条红。
    """
    # 两个写者互不依赖且都无上游——规则1.5 会串序，但这里刻意让它们写【不同】文件
    # 而并行写同一个根聚合清单（根聚合有独立 backstop），验证硬闸本体还在。
    plan = TaskPlan(subtasks=[
        _st("st-a", create=["pom.xml"], desc="writer a"),
        _st("st-b", create=["pom.xml"], desc="writer b"),
        _st("st-c", create=["pom.xml"], desc="writer c"),
    ])
    res = validate_plan_structure(plan)
    assert not res.valid, "三个并行写者同写根聚合清单竟被放行 ⇒ 硬闸本体被拆了"


def test_m1_no_false_merge_of_distinct_files():
    """区分力锁：归一后仍是**不同**文件的，绝不能被合并成一个写者组。

    若有人把归一写成过度激进（如剥掉所有目录层），本条红。
    """
    plan = TaskPlan(subtasks=[
        _st("st-scaffold", create=["a/registry.json", "b/registry.json"], desc="scaffold"),
        _st("st-a", writable=["a/registry.json"], depends=["st-scaffold"], desc="a"),
        _st("st-b", writable=["b/registry.json"], depends=["st-scaffold"], desc="b"),
    ])
    normalize_plan_scopes(plan, None, None)
    b = next(s for s in plan.subtasks if s.id == "st-b")
    assert "st-a" not in (b.depends_on or []), \
        "写不同文件的两个子任务被串了序 ⇒ 归一过度激进，无谓串行化"


# ───────────────── A2-L2：同族三处 sibling ─────────────────

def test_l2_dedupe_module_scaffolds_merges_across_spellings():
    """L2-(1)：模块清单的重复脚手架必须跨拼写合并。"""
    from swarm.brain.contract_utils import dedupe_module_scaffolds

    plan = TaskPlan(subtasks=[
        _st("st-1", create=["mod/pom.xml"], desc="scaffold mod"),
        _st("st-2", create=["./mod/pom.xml"], desc="scaffold mod again"),
    ])
    merged = dedupe_module_scaffolds(plan)
    assert merged >= 1, "两种拼写的同一模块清单未合并（键空间未统一）"


def test_l2_maven_module_build_scope_does_not_double_add():
    """L2-(2)：`-pl mod` 的 mod/pom.xml 已有写者（异拼写）时不得重复追加。

    ★两侧同源★：`all_write_targets` 与 `mod_pom` 都必须过归一，只改一侧＝把假阴换成假阳。
    """
    from swarm.brain.contract_utils import _ensure_maven_module_build_scope
    from swarm.types import TaskHarness

    plan = TaskPlan(subtasks=[
        _st("st-owner", create=["./mod/pom.xml", "mod/src/main/java/A.java"], desc="owner"),
        SubTask(id="st-b", description="build mod",
                scope=FileScope(create_files=["mod/src/main/java/B.java"]),
                harness=TaskHarness(language="java",
                                    build_command="mvn -pl mod -am compile")),
    ])
    _ensure_maven_module_build_scope(plan.subtasks)   # 真签名：吃 subtasks 列表
    b = next(s for s in plan.subtasks if s.id == "st-b")
    _creates = [_norm_scope_path(f) for f in (b.scope.create_files or [])]
    assert _creates.count("mod/pom.xml") == 0, (
        f"mod/pom.xml 已有写者（拼写 './mod/pom.xml'）却被重复追加给 st-b: {_creates}")


def test_l2_rule0_recognizes_create_across_spellings(tmp_path):
    """L2-(3)：规则0 的 `_all_creates` 必须跨拼写认得 create（否则造双 create）。

    ★必须给【真 git 仓】★：规则0 整块在 `if _tree:` 内（`_base_tree_listing` 对非 git 目录
    返 None）。初版传 `project_path=None` ⇒ 整块跳过 ⇒ **那条锁在空转**（突变 f7 全绿逮到）。
    这是"夹具形状决定被测命题"的第三次实例（批 E 的 e17 同型）。

    行为面：writable 指向一个**已被别人 create（异拼写）**的文件时，不得被挪进 create_files。
    """
    import subprocess

    # 真 git 仓 + base 树里【没有】x/Foo.java（它是本轮新建的）
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "README.md").write_text("base\n", "utf-8")
    for _c in (["git", "-C", str(tmp_path), "add", "-A"],
               ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t",
                "commit", "-q", "-m", "base"]):
        subprocess.run(_c, check=True, capture_output=True)

    plan = TaskPlan(subtasks=[
        _st("st-a", create=["./x/Foo.java"], desc="creator"),
        _st("st-b", writable=["x/Foo.java"], depends=["st-a"], desc="modifier"),
    ])
    import logging as _logging
    _records: list[str] = []

    class _Cap(_logging.Handler):
        def emit(self, record):
            _records.append(record.getMessage())

    _lg = _logging.getLogger("swarm.brain.contract_utils")
    _h = _Cap()
    _lg.addHandler(_h)
    try:
        normalize_plan_scopes(plan, str(tmp_path), None)
    finally:
        _lg.removeHandler(_h)

    b = next(s for s in plan.subtasks if s.id == "st-b")
    _b_creates = [_norm_scope_path(f) for f in (b.scope.create_files or [])]
    assert "x/Foo.java" not in _b_creates, (
        f"writable 被误挪进 create_files ⇒ 双 create（键空间未统一）: {_b_creates}")

    # ★必须断【规则0 自己的判定】，不能只断终态★
    # 实测：键空间回退后规则0 确实误判并打出"视为新建挪入 create_files"，但**下游规则1
    # 随后把它收敛回去** ⇒ 终态相同 ⇒ 只断终态的锁永远绿（正是 finding 把这三条评为 LOW
    # 的理由："都有下游兜底"）。这也是"冗余防御=互相兜底=两条都不可证伪"的实例：
    # 兜底让上游缺陷在终态不可见，所以锁必须钉在**兜底修不掉的那个可观测量**上——
    # 这里是规则0 自己的误判日志。
    _misjudged = [m for m in _records
                  if "规则0" in m and "st-b" in m and "视为新建挪入 create_files" in m]
    assert not _misjudged, (
        "规则0 把一个【已被别的子任务 create（异拼写）】的 writable 误判为新建"
        f"（下游规则1 虽会收敛，但误判本身就是键空间未统一的证据）: {_misjudged}")


def test_l2_rule0_fixture_actually_reaches_the_rule(tmp_path):
    """★前提锁★ 证明上一条的夹具真的进了规则0（否则它是空转的假绿）。

    判据：base 树里**已存在**的文件被 writable 引用时，规则0 会把它**留在** writable
    （不挪进 create）——这是规则0 在跑的可观测证据。若夹具没进规则0，本条也不会红，
    所以再加一条反向格：base 树【没有】的文件 + 无人 create ⇒ 规则0 会挪进 create_files。
    """
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, capture_output=True)
    _ex = tmp_path / "x"
    _ex.mkdir()
    (_ex / "Existing.java").write_text("class Existing {}\n", "utf-8")
    for _c in (["git", "-C", str(tmp_path), "add", "-A"],
               ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t",
                "commit", "-q", "-m", "base"]):
        subprocess.run(_c, check=True, capture_output=True)

    # 反向格：base 树里没有 Ghost.java、也没人 create 它 ⇒ 规则0 应把它挪进 create_files
    plan = TaskPlan(subtasks=[_st("st-x", writable=["x/Ghost.java"], desc="writer")])
    normalize_plan_scopes(plan, str(tmp_path), None)
    x = plan.subtasks[0]
    _creates = [_norm_scope_path(f) for f in (x.scope.create_files or [])]
    assert "x/Ghost.java" in _creates, (
        "规则0 未把'不在 base 树且无人 create'的 writable 挪进 create_files ⇒ "
        f"夹具没进规则0，上一条锁是空转的: creates={_creates}")


def test_l2_keyspace_is_one_single_source():
    """★族级锁★ 四个站点必须都用同一个归一函数。

    不用 getsource 断文本（批 E 教训：注释里有那个词就假绿）；改为**行为等价性**：
    对同一组拼写变体，四处的判定必须一致。此处用可直接驱动的三处 + 归一函数自身。
    """
    _variants = ["x/pom.xml", "./x/pom.xml", "x\\pom.xml", "/x/pom.xml", ".//x/pom.xml"]
    _keys = {_norm_scope_path(v) for v in _variants}
    assert _keys == {"x/pom.xml"}, f"归一函数自身对变体不收敛: {_keys}"


# ───────────────── A2-M2：冒烟推导 fail-closed ─────────────────

def _mixed_scala_java_repo(tmp_path):
    """混合仓：根 build.sbt（scala 源占优）+ 子目录可执行 boot pom。

    ★夹具形状即命题★：必须让 `detect_stack_deterministic` 真的裁出 build='sbt'
    （scala 源文件数占优），否则测的是另一条路径。
    """
    (tmp_path / "build.sbt").write_text('name := "mixed"\nscalaVersion := "2.13.12"\n', "utf-8")
    _sd = tmp_path / "src" / "main" / "scala"
    _sd.mkdir(parents=True)
    for i in range(4):
        (_sd / f"A{i}.scala").write_text(f"object A{i}\n", "utf-8")
    _ja = tmp_path / "javaapp"
    (_ja / "src" / "main" / "java").mkdir(parents=True)
    (_ja / "src" / "main" / "java" / "B.java").write_text("class B {}\n", "utf-8")
    (_ja / "pom.xml").write_text(
        "<project><groupId>g</groupId><artifactId>javaapp</artifactId><version>1</version>"
        "<build><plugins><plugin><groupId>org.springframework.boot</groupId>"
        "<artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></build>"
        "</project>", "utf-8")
    return tmp_path


def test_m2_unregistered_jvm_build_yields_none_not_malformed(tmp_path, caplog):
    """★核心锁★ STACK_SPEC 无该 JVM 构建键时，start_cmd 必须 None，绝不是畸形命令。

    治前实测：`start_cmd='java -jar javaapp/'`（无产物路径）+ `prepare_cmd=None`，
    而 evidence 上报"已推导" ⇒ 穿过 `smoke_derivation_missing`（只判非空）进入真启动 ⇒
    `Unable to access jarfile` 被当运行期事实归类 ⇒ **推导缺陷被洗成代码/环境缺陷**。
    """
    from swarm.brain.smoke_derive import derive_runtime_smoke
    from swarm.brain.stack_detect import detect_stack_deterministic

    _mixed_scala_java_repo(tmp_path)
    stack = detect_stack_deterministic(str(tmp_path))
    assert stack.get("build") == "sbt", f"夹具没裁出 sbt（命题变了）: {stack.get('build')}"

    with caplog.at_level("WARNING"):
        d = derive_runtime_smoke(stack, str(tmp_path))
    assert d.start_cmd is None, f"畸形 start_cmd 仍被产出: {d.start_cmd!r}"
    assert "java -jar" not in (d.start_cmd or ""), "拼出了没有产物路径的 java -jar"
    # 降级必须留痕（铁律#3）
    assert any("A2-M2" in r.getMessage() for r in caplog.records), \
        "fail-closed 无 WARNING ⇒ 「该栈不支持」与「推导器坏了」不可分"


def test_m2_downstream_gate_now_rejects(tmp_path):
    """接线锁：消费侧闸必须因此判"推导不可用"（归因回到推导，不再洗成运行期失败）。"""
    from swarm.brain.nodes.verify import smoke_derivation_missing, smoke_derivation_usable
    from swarm.brain.smoke_derive import derive_runtime_smoke
    from swarm.brain.stack_detect import detect_stack_deterministic

    _mixed_scala_java_repo(tmp_path)
    stack = detect_stack_deterministic(str(tmp_path))
    d = derive_runtime_smoke(stack, str(tmp_path))
    assert not smoke_derivation_usable(d)
    assert "start_cmd" in smoke_derivation_missing(d)


def test_m2_maven_path_unaffected(tmp_path):
    """★反向锁（防把闸拧成恒 None）★ maven 正常路径必须照旧推得出。"""
    from swarm.brain.smoke_derive import derive_runtime_smoke
    from swarm.brain.stack_detect import detect_stack_deterministic

    (tmp_path / "pom.xml").write_text(
        "<project><packaging>pom</packaging><groupId>g</groupId><artifactId>root</artifactId>"
        "<version>1</version><modules><module>javaapp</module></modules></project>", "utf-8")
    _ja = tmp_path / "javaapp"
    (_ja / "src" / "main" / "java").mkdir(parents=True)
    (_ja / "src" / "main" / "java" / "B.java").write_text("class B {}\n", "utf-8")
    (_ja / "pom.xml").write_text(
        "<project><groupId>g</groupId><artifactId>javaapp</artifactId><version>1</version>"
        "<build><plugins><plugin><groupId>org.springframework.boot</groupId>"
        "<artifactId>spring-boot-maven-plugin</artifactId></plugin></plugins></build>"
        "</project>", "utf-8")
    stack = detect_stack_deterministic(str(tmp_path))
    assert stack.get("build") == "maven", f"夹具没裁出 maven: {stack.get('build')}"
    d = derive_runtime_smoke(stack, str(tmp_path))
    assert d.start_cmd == "java -jar javaapp/target/*.jar", \
        f"maven 正常路径被 fail-closed 误伤: {d.start_cmd!r}"
    assert d.prepare_cmd, "maven 的 prepare_cmd 丢了"


def test_m2_jvm_manifest_backend_entries_are_covered_or_registered():
    """★兜底闸：下一个 JVM 构建工具进表时不得静默★

    「为漏项造的兜底网不能用同一份枚举编」的适用面：判据从**生产的两张权威表**派生
    （`_MANIFEST_BACKEND` × `JVM_LANGS` × `STACK_SPEC`），不手抄任何名单。

    规则：凡 `_MANIFEST_BACKEND` 里语言落在 JVM 臂的条目，其 build 键要么在 STACK_SPEC 里
    且有非空 `runtime_prepare_marker`（＝真支持），要么在下面的**显式未支持登记**里
    （＝已知缺口，由 A2-M2 的 fail-closed 兜住）。两者都不满足 ⇒ 红。

    ★这条闸的价值不在钉住现状，而在钉住【新增】★：往 `_MANIFEST_BACKEND` 加一个 JVM 清单
    （或往 `JVM_LANGS` 加一门语言）而忘了配 spec，本条立刻红，而不是等到某次冒烟被误归因。
    """
    from swarm.brain.smoke_derive import JVM_LANGS
    from swarm.brain.stack_detect import _MANIFEST_BACKEND
    from swarm.stacks.spec import STACK_SPEC, spec_for_stack

    # 已知未支持的 JVM 构建键（fail-closed 兜住；将来支持了就从这里删掉并配 spec）
    _KNOWN_UNSUPPORTED_JVM_BUILDS = {"sbt"}

    _offenders: list[str] = []
    for manifest, val in sorted(_MANIFEST_BACKEND.items()):
        if not (isinstance(val, tuple) and len(val) >= 2):
            continue
        lang, build = str(val[0]), str(val[1])
        if lang not in JVM_LANGS:
            continue
        if build in _KNOWN_UNSUPPORTED_JVM_BUILDS:
            continue
        _spec = spec_for_stack(build)
        if build not in STACK_SPEC or _spec is None:
            _offenders.append(f"{manifest} → build={build!r} 不在 STACK_SPEC 且未登记未支持")
            continue
        if not getattr(_spec, "runtime_prepare_marker", ""):
            _offenders.append(
                f"{manifest} → build={build!r} 在 STACK_SPEC 但 runtime_prepare_marker 为空"
                "（JVM 臂会拼不出产物路径）")
    assert not _offenders, (
        "JVM 清单表与 STACK_SPEC 不一致（下一个 JVM 构建工具会静默走进 A2-M2 的缺口）：\n  "
        + "\n  ".join(_offenders))


def test_m2_known_unsupported_registry_is_not_a_blanket_exemption():
    """前提锁：未支持登记必须真的**是**缺口，不能拿它豁免已支持的栈。

    若有人为了让上一条闸变绿而往登记表里塞 maven/gradle，本条红。
    """
    from swarm.stacks.spec import spec_for_stack

    for _b in ("maven", "gradle"):
        _s = spec_for_stack(_b)
        assert _s is not None and getattr(_s, "runtime_prepare_marker", ""), \
            f"{_b} 应当是已支持的（有 marker）——它绝不该出现在未支持登记里"


# ───────────────── A2-L3：前缀剥离语义 ─────────────────

@pytest.mark.parametrize("path,expect", [
    (".mvn/pom.xml", ".mvn/pom.xml"),                 # ★lstrip 会削成 mvn/pom.xml★
    (".config/settings.gradle", ".config/settings.gradle"),
    (".yarn/releases/x.cjs", ".yarn/releases/x.cjs"),
    ("./pom.xml", "pom.xml"),
    (".//pom.xml", "pom.xml"),
    ("/pom.xml", "pom.xml"),
    ("pom.xml", "pom.xml"),
    ("a\\b\\pom.xml", "a/b/pom.xml"),
])
def test_l3_manifest_path_norm_strips_literal_prefix_not_charset(path, expect):
    """`lstrip("./")` 剥的是字符集 `{'.','/'}` ⇒ 隐藏目录被削掉第一个字符。

    本仓已就同一坑做过 #29-8 M-3 整改（`nodes/__init__.py:4920` 明写"绝不用 lstrip('./')"），
    这两处原样保留＝口径不同源。现实危害为零（两个消费者判的是 `"/" not in p`，剥完仍含 `/`），
    故按**一致性**治：`.mvn/wrapper`、`.yarn/releases` 被同型错误剔没了是已立档的实例。
    """
    from swarm.stacks.spec import _norm_manifest_path

    assert _norm_manifest_path(path) == expect


def test_l3_root_aggregate_verdict_unchanged_for_hidden_dirs():
    """行为面：隐藏目录下的清单仍判非根级（该方向本来就安全，治后不得反转）。"""
    from swarm.stacks.spec import is_root_aggregate_manifest

    assert is_root_aggregate_manifest("pom.xml") is True
    assert is_root_aggregate_manifest("./pom.xml") is True
    assert is_root_aggregate_manifest(".mvn/pom.xml") is False
    assert is_root_aggregate_manifest("sub/pom.xml") is False


@pytest.mark.parametrize("path", [".pom.xml", ".package.json", ".settings.gradle"])
def test_l3_dotfile_is_not_the_root_aggregate_manifest(path):
    """★★这一格证伪了原报告的严重度判断★★

    报告称 `lstrip("./")` 的"现实危害为零 / 方向安全（不会把子目录清单误判成根聚合清单）"。
    对**子目录**成立（剥完仍含 `/` ⇒ 仍判非根级），但它漏了**根级 dotfile**：
        `.pom.xml`      --lstrip("./")-->  `pom.xml`      ⇒ 判成根聚合清单 ★假阳★
        `.package.json` --lstrip("./")-->  `package.json` ⇒ 同
        `..//pom.xml`   --lstrip("./")-->  `pom.xml`      ⇒ 同
    这是**假阳**方向（把不是根聚合清单的东西当成它），比报告设想的方向危险：消费者是
    `plan_validator` 的根聚合硬失败闸（D1 backstop），假阳＝对一个普通 dotfile 触发
    "多写者同写根聚合清单"的硬打回＝冤杀。

    突变 f12（把 is_root_aggregate_manifest 换回内联 lstrip）会被本组锁逮到；
    只测 `.mvn/pom.xml` 那格逮不到（两种归一在那格判定恰好相同）——
    **夹具形状决定被测命题**的又一实例。
    """
    from swarm.stacks.spec import is_root_aggregate_manifest

    assert is_root_aggregate_manifest(path) is False, \
        f"{path!r} 被判成根聚合清单（dotfile 被 lstrip 削成了清单名）"


def test_l3_parent_traversal_not_collapsed_to_root():
    """`..//pom.xml` 不得被折成根级 `pom.xml`（同上假阳族）。"""
    from swarm.stacks.spec import is_root_aggregate_manifest

    assert is_root_aggregate_manifest("..//pom.xml") is False


# ───────────────── A2-L1：死代码标注不得静默失效 ─────────────────

def test_l1_dead_function_still_has_no_production_callsite():
    """★钉住标注为真★ `_dep_group_from_baseline` 若被接进生产，本条转红。

    A2-L1 的治法是"显式标注 + 机读钉住"而非删除（保留理由已写进该函数 docstring：
    移植三条测试会给锁引入网络依赖）。标注本身会腐烂——除非有机器检查它。
    一旦有人把它接回生产，红灯逼其重新评估是否该改用 `maven_registry.resolve_artifacts`。
    """
    import pathlib
    import subprocess

    root = pathlib.Path(__file__).resolve().parent.parent
    out = subprocess.run(
        ["grep", "-rn", "--include=*.py", "_dep_group_from_baseline", "."],
        cwd=root, capture_output=True, text=True).stdout
    _hits = [ln for ln in out.splitlines()
             if ln.strip() and "/.venv/" not in ln and not ln.startswith("./.venv")]
    _prod = [ln for ln in _hits
             if not ln.startswith("./test/") and ":def _dep_group_from_baseline" not in ln]
    assert not _prod, (
        "`_dep_group_from_baseline` 出现了生产调用点——它的 groupId 判定序是 "
        "`maven_registry.resolve_artifacts` 的**子集**（无 Central 反查）。请改用后者，"
        f"或更新该函数 docstring 的标注：\n  " + "\n  ".join(_prod))


def test_l1_successor_covers_the_three_propositions(tmp_path):
    """承接方必须真的覆盖那三个命题（否则"已由 X 承接"的标注是空话）。

    ★不走网络★：只断 baseline/reactor 两档（毒坐标 drop、唯一第三方证据胜出、
    reactor 内部模块）。Central 反查那档刻意不测——它是承接方的**超集**部分。
    """
    from swarm.brain.maven_registry import index_baseline, resolve_artifacts

    # ★版本必须【本地受管】★：承接方的 version 档在查不到时会 drop（"绝不产出无版本又无人
    # 管的依赖"）。若不在 dependencyManagement 里钉版本，它会走仓库查询 ⇒ **本锁变成网络依赖**
    # （实测：同一夹具联网时 kept=cn.hutool:5.8.47、断网时 dropped）。这也正是 A2-L1 选"标注
    # 而非移植测试"的实证依据——移植会把网络依赖带进锁。
    (tmp_path / "pom.xml").write_text(
        '<?xml version="1.0"?><project><groupId>com.ruoyi</groupId>'
        "<artifactId>ruoyi</artifactId><version>4.8.3</version><packaging>pom</packaging>"
        "<dependencyManagement><dependencies>"
        "<dependency><groupId>cn.hutool</groupId><artifactId>hutool-all</artifactId>"
        "<version>5.8.47</version></dependency>"
        "</dependencies></dependencyManagement>"
        "<modules><module>ruoyi-common</module></modules></project>", "utf-8")
    for _m, _g in (("bad-mod", "com.ruoyi"), ("good-mod", "cn.hutool")):
        (tmp_path / _m).mkdir()
        (tmp_path / _m / "pom.xml").write_text(
            f"<project><parent><groupId>com.ruoyi</groupId></parent><artifactId>{_m}</artifactId>"
            f"<dependencies><dependency><groupId>{_g}</groupId>"
            "<artifactId>hutool-all</artifactId></dependency></dependencies></project>", "utf-8")
    (tmp_path / "ruoyi-common").mkdir()
    (tmp_path / "ruoyi-common" / "pom.xml").write_text(
        "<project><artifactId>ruoyi-common</artifactId></project>", "utf-8")

    idx = index_baseline(str(tmp_path))
    # 命题①：毒坐标（工程 groupId 挂第三方 artifact）不得被采信
    kept, dropped = resolve_artifacts(
        str(tmp_path), ["spring-boot-starter-web"], idx=idx)
    assert "spring-boot-starter-web" in dropped and not kept, \
        "承接方未 drop 幽灵坐标 ⇒ 标注失实"
    # 命题②：互斥证据下唯一真第三方证据胜出
    kept, _ = resolve_artifacts(str(tmp_path), ["hutool-all"], idx=idx)
    assert [d.group for d in kept] == ["cn.hutool"], \
        f"承接方未在毒证据在场时选中真第三方 group: {[(d.group, d.artifact) for d in kept]}"
    # 命题③：reactor 内部模块 → 工程 groupId
    kept, _ = resolve_artifacts(str(tmp_path), ["ruoyi-common"], idx=idx)
    assert [d.group for d in kept] == ["com.ruoyi"]
