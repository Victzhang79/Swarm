"""A4 治本（round22 假绿门）：build 命令派生沙箱模式漏判 manifest。

根因：_derive_full_build_command 的 has() 用本地 os.path.isfile 判 pom.xml/go.mod…；
沙箱模式本地树只有 pull-back 的可写文件，根 manifest 不在本地 → has("pom.xml")=False →
Brain 未下发 build_command 时 derive 返回 "" → build 闸门整段跳过；_compile_files 对 Java
无编译分支直接 return True → L1.2 假绿（真编译错误漏网）。

治本：has() 改走已有的沙箱优先 _manifest_present（与 lint/_build_cmd_applicable 同源），
沙箱里 find 到 manifest 即视为存在。跨栈（Java/Go/Rust/TS）一致。

行为测试：mock _manifest_present 模拟"本地无、沙箱有"。
"""
from __future__ import annotations

from unittest.mock import patch

from swarm.worker import l1_pipeline


def _derive(modified, stack=None):
    return l1_pipeline._derive_full_build_command("/tmp/swarm-a4", modified, stack or {})


def test_maven_derived_when_manifest_only_in_sandbox():
    """本地无 pom（isfile 全 False），但沙箱有 → 沙箱感知 _manifest_present 判在 → 派生 mvn。"""
    def fake_present(manifests, project_path):
        return "pom.xml" in manifests  # 沙箱里有 pom，其余(gradle)无
    with patch.object(l1_pipeline, "_manifest_present", side_effect=fake_present):
        cmd = _derive(["ruoyi-common/src/main/java/A.java"])
    assert cmd == "mvn -q compile", f"沙箱有 pom 时应派生 maven 编译，got {cmd!r}"


def test_go_derived_when_manifest_only_in_sandbox():
    def fake_present(manifests, project_path):
        return "go.mod" in manifests
    with patch.object(l1_pipeline, "_manifest_present", side_effect=fake_present):
        cmd = _derive(["pkg/svc.go"])
    assert cmd == "go build ./...", f"got {cmd!r}"


def test_no_manifest_anywhere_still_empty():
    """回归：本地与沙箱都无 manifest → 仍返回 ""（不凭空造命令）。"""
    with patch.object(l1_pipeline, "_manifest_present", return_value=False):
        cmd = _derive(["A.java"])
    assert cmd == "", f"无 manifest 不应派生命令，got {cmd!r}"


def test_explicit_stack_build_maven_independent_of_manifest():
    """回归：stack 明示 build=maven 时不依赖 manifest 探测。"""
    with patch.object(l1_pipeline, "_manifest_present", return_value=False):
        cmd = _derive(["A.java"], stack={"build": "maven"})
    assert cmd == "mvn -q compile", f"got {cmd!r}"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== A4 build 派生沙箱感知: {len(fns)}/{len(fns)} passed ===")
