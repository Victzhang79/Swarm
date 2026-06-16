"""L1 scope 闸门：create_files 是合法可写（bug 9da731ab：原仅认 writable 误判新建越权）。"""
from swarm.types import FileScope
from swarm.worker.l1_pipeline import _scope_violations

_NEW = "--- /dev/null\n+++ b/com/x/HealthController.java\n@@ -0,0 +1 @@\n+class H {}\n"
_MOD = "--- a/com/x/ShiroConfig.java\n+++ b/com/x/ShiroConfig.java\n@@ -1 +1 @@\n+changed\n"


def test_create_files_not_violation():
    """新建文件在 create_files 中 → 不判越权（bug 修复核心）。"""
    scope = FileScope(writable=["com/x/ShiroConfig.java"],
                      create_files=["com/x/HealthController.java"])
    v = _scope_violations(_NEW + _MOD, scope)
    assert v == [], f"create_files 文件不应判越权: {v}"


def test_writable_only_still_ok():
    scope = FileScope(writable=["com/x/ShiroConfig.java"])
    assert _scope_violations(_MOD, scope) == []


def test_real_violation_still_caught():
    """改 scope 外文件 → 仍判越权（修复不放水）。"""
    scope = FileScope(writable=["com/x/ShiroConfig.java"])
    other = "--- a/com/x/Other.java\n+++ b/com/x/Other.java\n@@ -1 +1 @@\n+x\n"
    v = _scope_violations(other, scope)
    assert "com/x/Other.java" in v


def test_delete_files_allowed():
    scope = FileScope(writable=[], create_files=[], delete_files=["com/x/Old.java"])
    d = "--- a/com/x/Old.java\n+++ b/com/x/Old.java\n@@ -1 +0 @@\n-gone\n"
    assert _scope_violations(d, scope) == []
