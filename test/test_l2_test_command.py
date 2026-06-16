"""task dc1ec890 回归：任务无显式测试命令时 VERIFY_L2 跳过测试验证（不写死 pytest）。

根因：_l2_test_command_from_criteria 默认返回 "pytest -q"（写死 Python），对 Java/Go/前端
项目跑 pytest 必然失败；且任务未要求测试时本不该跑测试。VERIFY_L2 的 integration_review
（编译+契约+git apply 同源）已是充分集成验证。
修复：criteria 无显式测试命令时返回空串 → verify_l2 跳过沙箱/本地测试验证，integration_review
通过即 L2 通过。
"""
from swarm.brain.nodes.shared import _l2_test_command_from_criteria


def test_no_test_command_returns_empty():
    """无显式测试命令的 criteria → 返回空串（不再默认 pytest）。"""
    assert _l2_test_command_from_criteria([]) == ""
    assert _l2_test_command_from_criteria(["编译通过", "方法签名正确"]) == ""
    assert _l2_test_command_from_criteria(["isBlankAll 方法存在且逻辑正确"]) == ""


def test_explicit_test_command_extracted():
    """criteria 含显式测试命令 → 提取它。"""
    assert _l2_test_command_from_criteria(["运行 pytest -q tests/"]) == "pytest -q tests/"
    assert _l2_test_command_from_criteria(["mvn test -pl ruoyi-common"]) == "mvn test -pl ruoyi-common"
    assert _l2_test_command_from_criteria(["npm test"]) == "npm test"


def test_java_criteria_not_forced_to_pytest():
    """Java 项目的验收标准不应被强制成 pytest（核心回归）。"""
    java_criteria = [
        "在 StringUtils.java 中新增 isBlankAll 方法",
        "mvn compile -pl ruoyi-common 通过",
    ]
    cmd = _l2_test_command_from_criteria(java_criteria)
    # mvn compile 不是测试命令(_L2_CMD_RE 只匹配 mvn test)，应返回空，绝不返回 pytest
    assert cmd == "", f"Java 编译标准不应返回测试命令，更不能是 pytest: {cmd!r}"
    assert "pytest" not in cmd
