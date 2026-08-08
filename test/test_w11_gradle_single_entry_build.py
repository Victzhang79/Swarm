"""#29-5 W-11：gradle 构建命令——单入口构造、-p 必落实际执行入口、绝不吞 stderr。

修复前（29 号文 W-11，全部实测复核过）：
- W-4 模块收窄用 `re.sub(r"^(./gradlew|gradle)(\s+)", ...)` 改写整串，^ 锚定只命中
  `||` 第一分支 ⇒ gradlew 不可用时回退分支无 -p ⇒ 收窄整块失效（修复没真到得了生产）；
- `2>/dev/null` 吞掉 gradlew 真编译错，双跑尾追加 `sh: 1: gradle: not found` ⇒
  `_is_infra_failure` 把真代码错翻转成 infra 故障 ⇒ BLOCKED 走 transient 退避、
  repair 循环输入为空零新增即 break ⇒ 空转到配额耗尽。

每条断言的判据：改回旧形态（|| 双跑 / 2>/dev/null / 正则改写），对应测试必须红。
"""
import swarm.worker.l1_pipeline as lp
from swarm.stacks.spec import STACK_SPEC, gradle_build_cmd


# ── spec 事实面 ──

def test_spec_gradle_cmd_no_stderr_swallow():
    """spec 命令绝不含 2>/dev/null（stderr 是 L1 唯一编译错误证据源）。"""
    cmd = STACK_SPEC["gradle"].whole_project_build_cmd
    assert "2>/dev/null" not in cmd


def test_constructor_single_entry_per_invocation():
    """构造函数每次调用只产一个入口——不存在「只命中第一分支」的改写面。"""
    assert gradle_build_cmd() == "./gradlew -q classes"
    assert gradle_build_cmd(use_wrapper=False) == "gradle -q classes"


def test_constructor_narrow_injects_into_the_only_entry():
    """-p 注在选定入口之后（旧正则改写对回退分支恒失效）。"""
    assert gradle_build_cmd("svc", use_wrapper=True) == "./gradlew -p svc -q classes"
    assert gradle_build_cmd("svc", use_wrapper=False) == "gradle -p svc -q classes"


def test_constructor_quotes_project_dir_internally():
    """裸目录含空格 ⇒ 内部 shlex.quote（复核 LOW：「调用方已 quote」约定太脆弱）。"""
    assert gradle_build_cmd("my dir") == "./gradlew -p 'my dir' -q classes"


def test_spec_first_branch_matches_constructor():
    """spec 字面量首分支与构造函数默认输出对账锁——两处同形态字面量漂移必红。"""
    first = STACK_SPEC["gradle"].whole_project_build_cmd.split("||")[0].strip()
    assert first == gradle_build_cmd()


# ── L1 派生闸端到端（_derive_full_build_command 的 gradle 臂）──

def _derive(tmp_path, files, mods, chmod_wrapper=True):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        # 探测是真 os.access(X_OK)（本地无沙箱上下文时）——夹具必须给执行位，
        # 否则测的是「无执行位回退」而不是本命题（平台相关假绿同族：写文件默认 644）。
        if rel == "gradlew":
            p.chmod(0o755 if chmod_wrapper else 0o644)
    return lp._derive_full_build_command(str(tmp_path), mods, None)


def test_derive_narrow_hits_wrapper_branch(tmp_path):
    """子模块改动 + 有 wrapper ⇒ `./gradlew -p svc`（旧实现这分支本来就对，防回归）。"""
    cmd = _derive(tmp_path, {
        "settings.gradle": "include ':svc'",
        "gradlew": "#!/bin/sh",
        "svc/build.gradle": "plugins{id 'java'}",
        "svc/src/main/java/A.java": "class A{}",
    }, ["svc/src/main/java/A.java"])
    assert cmd == "./gradlew -p svc -q classes"


def test_derive_narrow_hits_fallback_branch(tmp_path):
    """子模块改动 + 无 wrapper ⇒ `gradle -p svc`——旧实现此处回退分支恒无 -p（核心缺陷）。"""
    cmd = _derive(tmp_path, {
        "settings.gradle": "include ':svc'",
        "svc/build.gradle": "plugins{id 'java'}",
        "svc/src/main/java/A.java": "class A{}",
    }, ["svc/src/main/java/A.java"])
    assert cmd == "gradle -p svc -q classes"


def test_derive_never_double_runs_nor_swallows(tmp_path):
    """派生命令绝不含 `||` 双跑与 2>/dev/null——真编译错时输出不再被 not found 翻转
    成 infra 故障（_is_infra_failure('真编译错+not found') 实测为 True）。"""
    cmd = _derive(tmp_path, {
        "build.gradle": "plugins{id 'java'}",
        "gradlew": "#!/bin/sh",
        "src/main/java/A.java": "class A{}",
    }, ["src/main/java/A.java"])
    assert "||" not in cmd
    assert "2>/dev/null" not in cmd
    # 单入口：命令里恰出现一个构建工具入口
    assert (cmd.count("./gradlew") + cmd.count(" gradle")) == 1


def test_derive_root_project_no_narrow(tmp_path):
    """根工程改动不收窄（_d 为空 → 无 -p）。"""
    cmd = _derive(tmp_path, {
        "build.gradle": "plugins{id 'java'}",
        "gradlew": "#!/bin/sh",
        "src/main/java/A.java": "class A{}",
    }, ["src/main/java/A.java"])
    assert cmd == "./gradlew -q classes"


# ── 双复核整改锁 ──

def test_derive_wrapper_not_executable_falls_back(tmp_path, caplog):
    """gradlew 存在但无执行位 ⇒ 回退 gradle 入口 + WARNING（双复核汇合项：
    否则 126 permission denied ⇒ _is_infra_failure 判 False ⇒ 权限问题被当
    代码错修、repair 空烧——repair 修不了执行位）。"""
    import logging
    with caplog.at_level(logging.WARNING, logger="swarm.worker.l1_pipeline"):
        cmd = _derive(tmp_path, {
            "settings.gradle": "include ':svc'",
            "gradlew": "#!/bin/sh",
            "svc/build.gradle": "plugins{id 'java'}",
            "svc/src/main/java/A.java": "class A{}",
        }, ["svc/src/main/java/A.java"], chmod_wrapper=False)
    assert cmd == "gradle -p svc -q classes"
    assert any("不可执行" in r.message for r in caplog.records)


def test_derive_narrow_rejected_warns(tmp_path, caplog):
    """子模块目录不满足 _SAFE_REL_DIR_RE ⇒ 丢弃收窄必须 WARNING（否则整项目
    classes 把无关模块错误归到本子任务的旧危害静默复活）。"""
    import logging
    with caplog.at_level(logging.WARNING, logger="swarm.worker.l1_pipeline"):
        cmd = _derive(tmp_path, {
            "settings.gradle": "x",
            "gradlew": "#!/bin/sh",
            "my svc/build.gradle": "plugins{id 'java'}",
            "my svc/src/main/java/A.java": "class A{}",
        }, ["my svc/src/main/java/A.java"])
    assert "-p" not in cmd
    assert any("收窄" in r.message for r in caplog.records)
