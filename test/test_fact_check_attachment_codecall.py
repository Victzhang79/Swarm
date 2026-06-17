"""e2e 暴露的真 bug：tech_design 事实核验把附件名/示例代码当被点名文件 → 误判虚假前提冤杀 ultra 任务。"""
import os
import subprocess
import tempfile

from swarm.brain.planning_nodes import _verify_named_files_exist


def _mk_repo():
    d = tempfile.mkdtemp()
    subprocess.run(["git", "-C", d, "init", "-q"], check=True)
    os.makedirs(d + "/src", exist_ok=True)
    open(d + "/src/Foo.java", "w").write("class Foo {}")
    return d


def test_attachment_not_treated_as_named_file():
    """PRD.md 等上传附件不应被当成被点名的项目文件核验（否则误判虚假前提）。"""
    d = _mk_repo()
    r = _verify_named_files_exist("详见 PRD.md 文档实现预警平台", d)
    assert not any("PRD.md" in x["file"] for x in r), f"附件不应被核验: {[x['file'] for x in r]}"


def test_code_calls_not_treated_as_named_file():
    """示例代码里的标准库/SDK 调用(Map.of/log.info/X.builder)不应被当文件核验。"""
    d = _mk_repo()
    desc = "示例: AlarmSimpleUtil.builder().sendMsg(); Map.of(a,b); log.info(x); System.out.println(y);"
    r = _verify_named_files_exist(desc, d)
    files = [x["file"] for x in r]
    assert not any(c in f for f in files
                   for c in ("Map.of", "log.info", "builder", "out.println", "System.out")), \
        f"代码调用不应被核验: {files}"


def test_real_source_file_still_verified():
    """真正的源文件路径仍正常核验（排除规则不放过真文件）。"""
    d = _mk_repo()
    r = _verify_named_files_exist("修改 Foo.java 和 src/Foo.java", d)
    assert any("Foo.java" in x["file"] for x in r), "真源文件应被核验"
    # Foo.java 存在 → exists=True
    foo = [x for x in r if "Foo.java" in x["file"]][0]
    assert foo["exists"] is True


def test_ultra_requirement_no_false_premise():
    """模拟 e2e 场景：含附件+示例代码的新建需求，不应产生被核验的虚假前提 token。"""
    d = _mk_repo()
    desc = ("实现预警编排平台 详见 PRD.md。SDK 示例 AlarmSimpleUtil.builder()"
            ".idempotentValue(x).params(Map.of(k,v)).sendMsg(); log.info(msg);")
    r = _verify_named_files_exist(desc, d)
    # 全部应是排除掉的，core 结果里不该有 PRD.md / 代码调用
    bad = [x["file"] for x in r if not x["exists"]]
    assert not any(c in f for f in bad
                   for c in ("PRD", "Map.of", "log.info", "builder")), \
        f"不该有附件/代码调用被判不存在: {bad}"
