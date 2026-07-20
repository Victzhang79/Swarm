"""R65D-T2 第④刀（worker 侧）：H1 模板覆写后，旧考卷不得考新模板。

round65d st-26 冤案链（沙箱 6b266c2b ev15→ev17→ev53）：worker 117s 交出含 jackson 的
并集 pom（本可通过自己的考卷）→ H1「模板即真值」覆写销毁 → 旧考卷 `grep -q jackson`
考模板必死 → False 零日志 → H2 回滚 → HANDLE_FAILURE 掉账饿死全场。

治本：H1 确定性落盘的文件，其内容【就是】brain 生成的模板——对该文件的内容断言
（grep / test -z "$(grep…)"）要么同义反复要么是陈旧卷，执行它只可能制造冤案。
worker 侧铁律=跳过 H1 覆写文件的内容断言 + 机读留痕（verify_skipped_h1）+ 响亮日志；
构建/校验类命令（mvn validate 等）照常执行。规划期同源重生成（brain 侧
reconcile_template_exam）是第一防线，本刀是外科/replan 路径下旧 plan 的运行期兜底。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from swarm.types import (
    FileScope,
    SubTask,
    SubTaskDifficulty,
    SubTaskModality,
    TaskHarness,
)

_TPL = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    # #29-B：结构完整（groupId+version），过 L1.1c pom 结构闸——本文件测的是 verify 断言/H1 覆写，
    # 非 pom 结构；给合法坐标才不会被结构闸提前短路。
    "<project>\n    <groupId>com.example</groupId>\n    <artifactId>mod-a</artifactId>\n"
    "    <version>1.0.0</version>\n    <dependencies>\n"
    "        <dependency>\n            <groupId>com.squareup.okhttp3</groupId>\n"
    "            <artifactId>okhttp</artifactId>\n        </dependency>\n"
    "    </dependencies>\n</project>")


def _mk_subtask(verify, desc="", create=None):
    return SubTask(
        id="st-26w", description=desc or "task st-26w",
        difficulty=SubTaskDifficulty.TRIVIAL, modality=SubTaskModality.TEXT,
        scope=FileScope(create_files=create or ["mod-a/pom.xml"]),
        harness=TaskHarness(verify_commands=verify),
    )


def _pom_diff():
    return ("--- /dev/null\n+++ b/mod-a/pom.xml\n@@ -0,0 +1,2 @@\n"
            "+<project>\n+</project>\n")


def test_l1_skips_content_assert_on_h1_enforced_rel():
    """★冤案拦截★：H1 覆写过的文件，针对它的旧内容断言（必死的 grep jackson）
    必须跳过 + verify_skipped_h1 机读留痕，绝不执行旧卷杀 worker。"""
    from swarm.worker.l1_pipeline import run_l1_pipeline

    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "mod-a").mkdir()
        (Path(d) / "mod-a" / "pom.xml").write_text(_TPL, encoding="utf-8")
        st = _mk_subtask(["grep -q 'jackson' mod-a/pom.xml"])
        ok, details = run_l1_pipeline(
            d, st, _pom_diff(), timeout=30,
            template_enforced_rels={"mod-a/pom.xml": _TPL})
        assert ok is True, f"H1 覆写文件的旧内容断言不得判死 worker: {details}"
        skipped = details.get("verify_skipped_h1") or []
        assert any("jackson" in c for c in skipped), \
            f"跳过必须机读留痕 verify_skipped_h1: {details}"
        assert not details.get("verify_failed"), details


def test_l1_negative_assert_on_enforced_rel_also_skipped():
    """负断言形态（test -z "$(grep…)"）同样是对 H1 文件的内容断言 → 跳过。"""
    from swarm.worker.l1_pipeline import run_l1_pipeline

    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "mod-a").mkdir()
        (Path(d) / "mod-a" / "pom.xml").write_text(_TPL, encoding="utf-8")
        st = _mk_subtask(["test -z \"$(grep 'okhttp' mod-a/pom.xml)\""])
        ok, details = run_l1_pipeline(
            d, st, _pom_diff(), timeout=30,
            template_enforced_rels={"mod-a/pom.xml": _TPL})
        assert ok is True, details
        assert details.get("verify_skipped_h1"), details


def test_l1_other_verifies_still_run_on_enforced_rel():
    """非内容断言（工具类命令）与其他文件的断言照常执行——跳过面收窄到
    「H1 文件 × 内容断言」交集，绝不放大成整个 verify 面的免死金牌。"""
    from swarm.worker.l1_pipeline import run_l1_pipeline

    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "mod-a").mkdir()
        (Path(d) / "mod-a" / "pom.xml").write_text(_TPL, encoding="utf-8")
        (Path(d) / "mod-a" / "Engine.java").write_text("class Engine {}",
                                                       encoding="utf-8")
        st = _mk_subtask([
            "test -f mod-a/pom.xml",                      # 工具类：照常跑
            "grep -q 'class Engine' mod-a/Engine.java",   # 其他文件：照常跑
        ])
        ok, details = run_l1_pipeline(
            d, st, _pom_diff(), timeout=30,
            template_enforced_rels={"mod-a/pom.xml": _TPL})
        assert ok is True, details
        ran = [r["cmd"] for r in (details.get("verify_commands") or [])]
        assert "test -f mod-a/pom.xml" in ran, details
        assert "grep -q 'class Engine' mod-a/Engine.java" in ran, details
        assert not details.get("verify_skipped_h1"), \
            "无内容断言命中 H1 文件时不得有跳过记录"


def test_l1_failing_content_assert_without_enforcement_still_kills():
    """对照面：没有 H1 覆写（template_enforced_rels 未传/为空）时，失败的内容断言
    照旧硬阻断——兜底绝不弱化既有考卷牙齿。"""
    from swarm.worker.l1_pipeline import run_l1_pipeline

    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "mod-a").mkdir()
        (Path(d) / "mod-a" / "pom.xml").write_text(_TPL, encoding="utf-8")
        st = _mk_subtask(["grep -q 'jackson' mod-a/pom.xml"])
        ok, details = run_l1_pipeline(d, st, _pom_diff(), timeout=30)
        assert ok is False, details
        assert details.get("verify_failed"), details


def test_gate_wires_enforced_rels_into_pipeline():
    """接线面：_deterministic_l1_gate 里 H1 落盘后必须把覆写文件集传进
    run_l1_pipeline（不传=兜底永不生效，st-26 冤案原样复活）。"""
    from swarm.worker.executor import WorkerExecutor

    with tempfile.TemporaryDirectory() as d:
        desc = ("创建 mod-a 脚手架\n【权威 pom 模板（确定性生成，原样写入 "
                f"mod-a/pom.xml）】\n```xml\n{_TPL}\n```")
        st = SubTask(
            id="st-26g", description=desc,
            difficulty=SubTaskDifficulty.TRIVIAL, modality=SubTaskModality.TEXT,
            scope=FileScope(create_files=["mod-a/pom.xml"]),
            harness=TaskHarness(verify_commands=["grep -q 'jackson' mod-a/pom.xml"]),
        )
        # 真实时序（ev10→ev15→ev17）：executor 构造时文件尚不存在（保持 CREATE 形态，
        # 否则 scope 归一化会降级 writable、H1 不再适用）→ worker 徒手写出并集 pom
        # → 闸门期 H1 覆写为模板
        ex = WorkerExecutor(subtask=st, project_path=d)
        (Path(d) / "mod-a").mkdir()
        (Path(d) / "mod-a" / "pom.xml").write_text(
            "<project>worker 徒手并集版</project>", encoding="utf-8")
        captured: dict = {}

        def _fake_pipeline(*args, **kwargs):
            captured.update(kwargs)
            return True, {"pipeline": "fake"}

        with patch.object(ex, "_get_git_diff", return_value=_pom_diff()), \
             patch("swarm.worker.l1_pipeline.run_l1_pipeline", _fake_pipeline):
            ex._deterministic_l1_gate()
        assert captured.get("template_enforced_rels") == {"mod-a/pom.xml": _TPL}, \
            f"H1 覆写文件→模板映射必须接线进 run_l1_pipeline: {captured}"
        # H1 真实落盘了模板（write-through 本地）
        assert (Path(d) / "mod-a" / "pom.xml").read_text(
            encoding="utf-8").strip() == _TPL.strip()


def test_l1_asserts_regain_teeth_when_content_diverges_from_template():
    """★猎手 CRITICAL 锁★：H1 登记后文件又被后续机制（R56-5/version-repair）改写
    （verify 时点内容≠模板）→ 内容断言恢复照常执行——温差窗口绝不留假绿。"""
    from swarm.worker.l1_pipeline import run_l1_pipeline

    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "mod-a").mkdir()
        # 磁盘内容 ≠ 登记的模板（模拟 H1 之后被依赖合法性闸改写）
        (Path(d) / "mod-a" / "pom.xml").write_text(
            "<project><artifactId>mod-a</artifactId></project>", encoding="utf-8")
        st = _mk_subtask(["grep -q '<artifactId>okhttp</artifactId>' mod-a/pom.xml"])
        ok, details = run_l1_pipeline(
            d, st, _pom_diff(), timeout=30,
            template_enforced_rels={"mod-a/pom.xml": _TPL})
        assert ok is False, \
            f"内容已偏离模板时断言必须恢复牙齿（okhttp 依赖真丢了）: {details}"
        assert details.get("verify_failed"), details
        assert not details.get("verify_skipped_h1"), \
            "内容≠模板时绝不可跳过（跳过=掩盖 R56-5 等机制引入的真实缺陷）"


def test_l1_path_boundary_no_false_skip_on_similar_names():
    """★复核 CONFIRMED 锁★：`xmod-a/pom.xml` 的断言绝不因 `mod-a/pom.xml` 被 H1
    登记而误跳过——路径按完整 token 边界匹配，嵌套/相似名不串扰。"""
    from swarm.worker.l1_pipeline import run_l1_pipeline

    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "mod-a").mkdir()
        (Path(d) / "mod-a" / "pom.xml").write_text(_TPL, encoding="utf-8")
        (Path(d) / "xmod-a").mkdir()
        (Path(d) / "xmod-a" / "pom.xml").write_text(
            "<project>没有那个依赖</project>", encoding="utf-8")
        st = _mk_subtask(["grep -q 'jackson' xmod-a/pom.xml"],
                         create=["mod-a/pom.xml", "xmod-a/pom.xml"])
        ok, details = run_l1_pipeline(
            d, st, _pom_diff(), timeout=30,
            template_enforced_rels={"mod-a/pom.xml": _TPL})
        assert ok is False, \
            f"针对 xmod-a/pom.xml 的断言与 H1 文件无关，必须照常执行并如实失败: {details}"
        assert not details.get("verify_skipped_h1"), details


def test_l1_needs_review_when_all_verify_skipped():
    """★猎手 HIGH 锁★：verify 清单非空但全部被 H1 跳过=语义正确性零覆盖，
    必须打 needs_review=verify_all_skipped_h1（绝不无痕放行）。"""
    from swarm.worker.l1_pipeline import run_l1_pipeline

    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "mod-a").mkdir()
        (Path(d) / "mod-a" / "pom.xml").write_text(_TPL, encoding="utf-8")
        st = _mk_subtask(["grep -q 'jackson' mod-a/pom.xml"])
        ok, details = run_l1_pipeline(
            d, st, _pom_diff(), timeout=30,
            template_enforced_rels={"mod-a/pom.xml": _TPL})
        assert ok is True, details
        assert details.get("needs_review") == "verify_all_skipped_h1", \
            f"全跳过必须打 needs_review 标记: {details}"
