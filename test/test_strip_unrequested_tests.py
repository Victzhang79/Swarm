"""task 744316e7 回归：未要求测试时，PLAN 源头剔除测试文件 + 清空 harness.test_command。

根因：Brain 给"加方法"任务塞测试文件(StringUtilsTest.java) + harness 带 mvn test，
但项目无 junit 依赖 → 测试编译失败 → mvn compile 过了却被 L1 判死。
"""
from swarm.brain.nodes.shared import (
    _is_test_file_path,
    _strip_unrequested_tests,
    _task_requests_tests,
)
from swarm.types import FileScope, SubTask, TaskHarness, TaskPlan


def _st(sid, create=None, writable=None, test_cmd="mvn test -Dtest=XxxTest"):
    return SubTask(
        id=sid,
        description=f"task {sid}",
        scope=FileScope(create_files=create or [], writable=writable or []),
        harness=TaskHarness(language="java", build_command="mvn compile", test_command=test_cmd),
    )


def test_is_test_file_path():
    assert _is_test_file_path("ruoyi-common/src/test/java/com/ruoyi/StringUtilsTest.java")
    assert _is_test_file_path("src/test/java/ConvertTest.java")
    assert _is_test_file_path("tests/test_foo.py")
    assert _is_test_file_path("foo_test.go")
    assert _is_test_file_path("a.test.ts")
    # 非测试文件
    assert not _is_test_file_path("src/main/java/StringUtils.java")
    assert not _is_test_file_path("ruoyi-common/src/main/java/com/ruoyi/common/utils/StringUtils.java")
    assert not _is_test_file_path("Convert.java")


def test_task_requests_tests():
    assert _task_requests_tests("给 X 加方法并写单元测试")
    assert _task_requests_tests("add unit test for foo")
    assert _task_requests_tests("提升测试覆盖率")
    # 未要求
    assert not _task_requests_tests("在 StringUtils 中新增 isBlankAll 方法")
    assert not _task_requests_tests("修复登录 bug")


def test_strip_removes_test_files_and_test_command():
    """任务未要求测试 → scope 测试文件被剔除 + test_command 清空。"""
    plan = TaskPlan(
        subtasks=[
            _st("st-1",
                create=["ruoyi-common/src/test/java/com/ruoyi/StringUtilsTest.java"],
                writable=["ruoyi-common/src/main/java/com/ruoyi/StringUtils.java"]),
        ],
        parallel_groups=[["st-1"]],
    )
    out = _strip_unrequested_tests(plan, "在 StringUtils 中新增 isBlankAll 方法")
    st = out.subtasks[0]
    # 测试文件被剔除，主文件保留
    assert st.scope.create_files == []
    assert st.scope.writable == ["ruoyi-common/src/main/java/com/ruoyi/StringUtils.java"]
    # test_command 清空，build_command 保留
    assert st.harness.test_command == ""
    assert st.harness.build_command == "mvn compile"


def test_strip_keeps_tests_when_requested():
    """任务明确要求测试 → 不剔除。"""
    plan = TaskPlan(
        subtasks=[
            _st("st-1",
                create=["ruoyi-common/src/test/java/com/ruoyi/StringUtilsTest.java"],
                writable=["ruoyi-common/src/main/java/com/ruoyi/StringUtils.java"]),
        ],
        parallel_groups=[["st-1"]],
    )
    out = _strip_unrequested_tests(plan, "给 StringUtils 加 isBlankAll 方法并写单元测试")
    st = out.subtasks[0]
    # 要求测试 → 测试文件 + test_command 都保留
    assert "StringUtilsTest.java" in st.scope.create_files[0]
    assert st.harness.test_command == "mvn test -Dtest=XxxTest"
