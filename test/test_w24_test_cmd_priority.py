"""W-24（#29-5 挂账，用户拍板维持现序）：`_guess_test_cmd` 栈集合派生化 + 优先级显式化。

机制：原实现循环 `for stack_key in ("python","go","cargo","npm")` 写死——
① 栈集合复制 TEST_DRIVERS 键集：新栈在 STACK_SPEC 加 `test_cmd` 会产出 driver，
   而循环从不问它 ⇒ 该栈工程级测试闸静默零覆盖（返 None → test_skipped）；
② 「JVM 刻意不猜」被 `test_cmd=""` 与该元组**两处独立编码** ⇒ 任一单独突变仍绿
   =不可证伪（xh_exec 锁因此一度撤销，记录在 scripts/xh_exec_mutation_check.py）。
   复活 JVM 突变时又逮到第三处编码=`_ext_for_lang` 手写小表（maven 拿到 test_cmd
   也过不了语言守卫）——已一并从 STACK_SPEC.source_exts 派生化。
治法：栈集合由 `test_drivers_by_priority()` 派生（STACK_SPEC 单一事实源），
胜出序由 `STACK_SPEC.test_priority` 显式声明（数值维持原元组序 python<go<cargo<npm）；
≥2 候选栈时只跑胜出者=固有欠覆盖，落 details["test_cmd_candidates"] 机读键（硬检查④）。
"""

import logging

from swarm.worker import l1_pipeline as lp
from swarm.worker.stack_drivers import TEST_DRIVERS
from swarm.worker.stack_drivers import TestDriver as _TestDriver  # 别名：防 pytest 误收集
from swarm.worker.stack_drivers import test_drivers_by_priority as _drivers_by_priority  # 同上


class TestW24PriorityOrder:
    def test_priority_matches_legacy_tuple_order(self):
        """现序锁（行为零变更承诺）：派生遍历序必须 == 原写死元组序
        python → go → cargo → npm。这条红了=多栈仓胜出者易主=未申报的行为变更。"""
        order = [d.stack_key for d in _drivers_by_priority()]
        assert order[:4] == ["python", "go", "cargo", "npm"], (
            f"派生遍历序 {order[:4]} 偏离原写死元组序——W-24 拍板的是【维持现序】")

    def test_new_stack_with_test_cmd_enters_iteration(self, monkeypatch, tmp_path):
        """派生覆盖锁（原缺陷的反向命题）：往 TEST_DRIVERS 注册一个新栈 driver，
        循环必须问它——旧写死元组下这条恒败（循环从不问元组外的栈）。"""
        fake = _TestDriver(stack_key="fakestack", lang="python",
                           test_cmd="fake test -q",
                           anchor_manifests=("fake.toml",), test_priority=5)
        monkeypatch.setitem(TEST_DRIVERS, "fakestack", fake)
        (tmp_path / "fake.toml").write_text("[x]\n", encoding="utf-8")
        got = lp._guess_test_cmd(str(tmp_path), ["a.py"])
        assert got is not None and "fake test -q" in got, (
            f"新注册栈未被遍历到（写死元组复发）: {got!r}")

    def test_jvm_still_deliberately_not_guessed(self, tmp_path):
        """不变量锁：JVM 两栈 test_cmd="" ⇒ 不在 TEST_DRIVERS ⇒ 派生循环天然不问——
        「刻意不猜」现在由 STACK_SPEC 一处编码（可证伪性修复的承重面）。"""
        assert "maven" not in TEST_DRIVERS and "gradle" not in TEST_DRIVERS
        (tmp_path / "pom.xml").write_text("<project/>\n", encoding="utf-8")
        assert lp._guess_test_cmd(str(tmp_path), ["src/main/java/A.java"]) is None


class TestW24CandidatesAccounting:
    def test_multi_stack_candidates_all_collected_winner_first(self, tmp_path, caplog):
        """候选收集锁：python+npm 同仓（两 manifest 都在、.py/.ts 都改、scripts.test 在）
        ⇒ 候选=[python, npm]（priority 序），胜出者 python；且有 WARNING（欠覆盖留痕）。"""
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        (tmp_path / "package.json").write_text(
            '{"name":"x","scripts":{"test":"vitest"}}\n', encoding="utf-8")
        out: list[str] = []
        with caplog.at_level(logging.WARNING, logger="swarm.worker.l1_pipeline"):
            cmd = lp._guess_test_cmd(str(tmp_path), ["a.py", "b.ts"],
                                     candidates_out=out)
        assert out == ["python", "npm"], f"候选集/序错: {out}"
        assert cmd is not None and "pytest" in cmd, "python（priority=10）必须胜出"
        assert any("多栈候选" in r.message for r in caplog.records), (
            "多栈候选只跑一套=固有欠覆盖，必须 WARNING 留痕（硬检查④）")

    def test_note_helper_writes_key_only_when_multi(self):
        """机读键锁：≥2 候选才落 details["test_cmd_candidates"]；单候选/零候选
        绝不留键（键缺席=无欠覆盖，语义干净）。"""
        d1: dict = {}
        lp._note_test_cmd_candidates(d1, ["python", "npm"])
        assert d1["test_cmd_candidates"] == ["python", "npm"]
        d2: dict = {}
        lp._note_test_cmd_candidates(d2, ["python"])
        assert "test_cmd_candidates" not in d2
        d3: dict = {}
        lp._note_test_cmd_candidates(d3, [])
        assert "test_cmd_candidates" not in d3

    def test_single_candidate_no_warning(self, tmp_path, caplog):
        """对照：单栈候选（只有 pyproject）→ 候选恰一条，无 WARNING 无键。"""
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        out: list[str] = []
        with caplog.at_level(logging.WARNING, logger="swarm.worker.l1_pipeline"):
            lp._guess_test_cmd(str(tmp_path), ["a.py"], candidates_out=out)
        assert out == ["python"]
        assert not any("多栈候选" in r.message for r in caplog.records)


class TestW24ExtForLangUnion:
    """复核 MEDIUM 整改：`_ext_for_lang` 同 lang 多栈取并集（不依赖 STACK_SPEC 插入序）。"""

    def test_java_is_union_of_maven_and_gradle(self):
        got = lp._ext_for_lang("java")
        assert set(got) == {".java", ".kt", ".scala", ".groovy"}, (
            f"java 后缀必须=两个 JVM 栈 source_exts 的并集: {got}")

    def test_union_not_first_hit(self, monkeypatch):
        """R2 复核补牙：maven/gradle 后缀集当前恰好全同 ⇒ 上一条分不出「并集」与
        「首命中」。临时注册同 lang="java" 但后缀不同的栈——回退首命中实现这条即红。"""
        import dataclasses

        from swarm.stacks import spec as spec_mod
        fake = dataclasses.replace(spec_mod.STACK_SPEC["gradle"], key="sbt",
                                   source_exts=(".scala", ".sbt"))
        monkeypatch.setitem(spec_mod.STACK_SPEC, "sbt", fake)
        got = lp._ext_for_lang("java")
        assert ".sbt" in got, f"并集必须含同 lang 新栈的独有大后缀: {got}"

    def test_existing_langs_unchanged(self):
        assert lp._ext_for_lang("python") == (".py",)
        assert lp._ext_for_lang("go") == (".go",)
        assert lp._ext_for_lang("rust") == (".rs",)
        # node 随 STACK_SPEC 派生（含 .mjs/.cjs——W-24 有证据多覆盖方向）
        assert set(lp._ext_for_lang("node")) == {
            ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue"}

    def test_unknown_lang_fail_closed(self):
        assert lp._ext_for_lang("cobol") == ()


class TestW24CandidatesConsumer:
    """复核 HIGH 整改（硬检查④：新账必须有人消费）：DELIVER payload 聚合
    `test_cmd_candidates` 进人工闸视野——「没测」与「测过」在终态对账上可分。"""

    def test_collect_aggregates_only_multi_candidate(self):
        from swarm.brain.nodes import _collect_partial_test_coverage
        st = {"subtask_results": {
            "st-1": {"l1_details": {"test_cmd_candidates": ["python", "npm"]}},
            "st-2": {"l1_details": {"test_cmd_candidates": ["go"]}},   # 单候选=无欠覆盖
            "st-3": {"l1_details": {}},                                # 缺键（旧 checkpoint）
            "st-4": {"l1_details": {"test_cmd_candidates": "python,npm"}},  # 脏账拒收
        }}
        assert _collect_partial_test_coverage(st) == [
            {"subtask_id": "st-1", "tested": "python", "untested": ["npm"]}]

    def test_deliver_payload_wires_the_key(self):
        """接线锁：payload 必带 partial_test_coverage 键（删掉聚合接线这条会红）。"""
        from swarm.brain.nodes import _deliver_review_payload
        st = {"subtask_results": {
            "st-1": {"l1_details": {"test_cmd_candidates": ["python", "npm"]}}}}
        p = _deliver_review_payload(st)
        assert p["partial_test_coverage"] == [
            {"subtask_id": "st-1", "tested": "python", "untested": ["npm"]}]
        assert _deliver_review_payload({})["partial_test_coverage"] == []
