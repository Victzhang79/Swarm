"""W-22（#29-5 挂账 1，用户拍板「治」）：A2 注入坐标推送未达 → failure_class=transient。

机制链（四环，缺一即断）：
① `_push_manifests_to_sandbox(status_out=...)` 区分「无沙箱」（本地模式，非降级）与
   「有沙箱但推送未达」（本地注入对构建不可见）——原 return 0 把两态合并不可分；
② A2 段收集注入栈+缺依赖坐标，推送未达时落 evidence_out["a2_push_undelivered"]；
③ `_note_a2_push_undelivered` 把记录抄进 details（=l1_details，brain 可见）；
④ l1_verdict：构建失败且【同一坐标仍在报错】（确定性证据）→ failure_class=transient
   + 降 sticky，brain 退避重试（重试即成），不走 capability 换模型阶梯（冤杀好活）。
"""

from swarm.worker import l1_pipeline as lp
from swarm.worker import l1_verdict as lv
from swarm.worker import sibling_dep_repair as sdr


class TestW22PushStatus:
    """环①：status_out 把「无沙箱」与「有沙箱但推送未达」分开（原 return 0 两态合并）。"""

    def test_no_sandbox_marks_not_present(self, monkeypatch, tmp_path):
        monkeypatch.setattr(lp, "_sandbox_ctx", lambda: None)
        status: dict = {}
        got = lp._push_manifests_to_sandbox(str(tmp_path), ["package.json"],
                                            status_out=status)
        assert got == 0
        assert status == {"sandbox_present": False, "uploaded": 0}

    def test_sandbox_without_sync_api_marks_undelivered(self, monkeypatch, tmp_path):
        (tmp_path / "package.json").write_text("{}\n", encoding="utf-8")
        fake_ctx = (object(), object(), "/remote")  # manager 无 sync_files_to_sandbox
        monkeypatch.setattr(lp, "_sandbox_ctx", lambda: fake_ctx)
        status: dict = {}
        got = lp._push_manifests_to_sandbox(str(tmp_path), ["package.json"],
                                            status_out=status)
        assert got == 0
        assert status["sandbox_present"] is True and status["uploaded"] == 0

    def test_successful_push_marks_uploaded(self, monkeypatch, tmp_path):
        (tmp_path / "package.json").write_text('{"name":"x"}\n', encoding="utf-8")

        class _Mgr:
            @staticmethod
            def sync_files_to_sandbox(sandbox, src_root, rels, remote):
                return {"uploaded": len(rels), "errors": []}

        monkeypatch.setattr(lp, "_sandbox_ctx", lambda: (object(), _Mgr(), "/remote"))
        status: dict = {}
        got = lp._push_manifests_to_sandbox(str(tmp_path), ["package.json"],
                                            status_out=status)
        assert got == 1
        assert status == {"sandbox_present": True, "uploaded": 1}


class TestW22A2Wiring:
    """环②：A2 推送未达 → evidence_out 落机读记录（栈+坐标）；推送成功/无沙箱不落。"""

    @staticmethod
    def _run(monkeypatch, tmp_path, push_ret, sandbox_present):
        monkeypatch.setattr(
            sdr, "repair_from_sibling_manifests",
            lambda *a, **k: (1, ["pkg/package.json"]))

        def _fake_push(project_path, manifests, status_out=None):
            if status_out is not None:
                status_out["sandbox_present"] = sandbox_present
                status_out["uploaded"] = push_ret
            return push_ret

        monkeypatch.setattr(lp, "_push_manifests_to_sandbox", _fake_push)
        evidence: dict = {}
        lp._attempt_build_repair(
            str(tmp_path), "Cannot find module 'left-pad'", ["src/a.ts"], 60,
            None, evidence_out=evidence)
        return evidence

    def test_undelivered_records_stacks_and_coords(self, monkeypatch, tmp_path):
        ev = self._run(monkeypatch, tmp_path, push_ret=0, sandbox_present=True)
        rec = ev.get("a2_push_undelivered")
        assert rec is not None, "有沙箱但推送未达必须落机读记录"
        assert rec["stacks"] == ["npm"]
        assert "left-pad" in rec["coords"]

    def test_successful_push_leaves_no_record(self, monkeypatch, tmp_path):
        ev = self._run(monkeypatch, tmp_path, push_ret=1, sandbox_present=True)
        assert "a2_push_undelivered" not in ev

    def test_no_sandbox_leaves_no_record(self, monkeypatch, tmp_path):
        """无沙箱=本地模式，构建直接读 project_path，注入本就可见——非降级，不落账。"""
        ev = self._run(monkeypatch, tmp_path, push_ret=0, sandbox_present=False)
        assert "a2_push_undelivered" not in ev

    def test_coords_parse_error_does_not_kill_push(self, monkeypatch, tmp_path, caplog):
        """复核 M2：坐标解析异常必须有独立兜底——推送照跑（_a2_paths 已先收）、
        WARNING 留痕；绝不冒出循环被调用方一把 break 吞成 debug（=transient 通道
        静默失坐标证据）。"""
        import logging

        monkeypatch.setattr(
            sdr, "repair_from_sibling_manifests",
            lambda *a, **k: (1, ["pkg/package.json"]))

        def _boom(*a, **k):
            raise ValueError("夹具：坐标解析炸")

        monkeypatch.setattr(sdr, "missing_deps_for", _boom)
        pushed: list = []

        def _fake_push(project_path, manifests, status_out=None):
            pushed.append(list(manifests))
            if status_out is not None:
                status_out["sandbox_present"] = True
                status_out["uploaded"] = 0
            return 0

        monkeypatch.setattr(lp, "_push_manifests_to_sandbox", _fake_push)
        evidence: dict = {}
        with caplog.at_level(logging.WARNING, logger="swarm.worker.l1_pipeline"):
            lp._attempt_build_repair(
                str(tmp_path), "Cannot find module 'left-pad'", ["src/a.ts"], 60,
                None, evidence_out=evidence)  # 不抛=异常没冒出循环
        assert pushed == [["pkg/package.json"]], "坐标解析异常绝不连带丢推送"
        assert any("坐标解析异常" in r.message for r in caplog.records), (
            "降级必须 WARNING 留痕（debug 会埋掉）")


class TestW22NoteHelper:
    """环③：evidence → details 抄写（coords 非空才落键；键缺席=未发生）。"""

    def test_copies_record_into_details(self):
        d: dict = {}
        lp._note_a2_push_undelivered(
            d, {"a2_push_undelivered": {"stacks": ["npm"], "coords": ["left-pad"]}})
        assert d["a2_push_undelivered"] == {"stacks": ["npm"], "coords": ["left-pad"]}

    def test_empty_coords_no_key(self):
        d: dict = {}
        lp._note_a2_push_undelivered(
            d, {"a2_push_undelivered": {"stacks": ["npm"], "coords": []}})
        assert "a2_push_undelivered" not in d

    def test_missing_or_dirty_evidence_no_key(self):
        for ev in (None, {}, {"a2_push_undelivered": "dirty"}):
            d: dict = {}
            lp._note_a2_push_undelivered(d, ev)
            assert "a2_push_undelivered" not in d


class TestW22VerdictConsumer:
    """环④：构建失败+同一坐标仍报错 → transient+降 sticky；坐标不在/无记录 → 原分类。"""

    @staticmethod
    def _details(**over):
        base = {
            "l1_2_1_build_ok": False,
            "build_failed": "npm run build",
            "build_output": "ERROR Cannot find module 'left-pad'",
            "a2_push_undelivered": {"stacks": ["npm"], "coords": ["left-pad"]},
        }
        base.update(over)
        return base

    def _verdict(self, details):
        return lv._evaluate_l1_core(det_ok=False, det_details=details,
                                    verify_result=None, llm_ok=None,
                                    prior=None, phase="test")

    def test_same_coord_still_reported_marks_transient(self):
        v = self._verdict(self._details())
        assert v.passed is False and v.sticky is False, "transient 必须可翻盘（重试即成）"
        assert v.details["failure_class"] == "transient"

    def test_coord_gone_keeps_original_classification(self):
        """坐标不再报错（如已修复但构建因别的原因失败）→ 不标 transient。"""
        v = self._verdict(self._details(build_output="ERROR something else entirely"))
        assert v.sticky is True
        assert "failure_class" not in v.details

    def test_checked_marker_distinguishes_miss_from_no_record(self):
        """复核 M1（硬检查④）：记录在案但判据未命中 → 落 checked=False 机读账；
        无记录 → 不落（两种死法可分辨，transient 通道被静默关掉时有账可查）。"""
        v = self._verdict(self._details(build_output="ERROR something else entirely"))
        assert v.details["a2_push_undelivered_checked"] is False
        d = self._details()
        d.pop("a2_push_undelivered")
        v2 = self._verdict(d)
        assert "a2_push_undelivered_checked" not in v2.details
        # transient 命中 → 不落的反账（failure_class 已是正面留痕）
        v3 = self._verdict(self._details())
        assert "a2_push_undelivered_checked" not in v3.details

    def test_no_record_keeps_original_classification(self):
        d = self._details()
        d.pop("a2_push_undelivered")
        v = self._verdict(d)
        assert v.sticky is True
        assert "failure_class" not in v.details

    def test_non_compile_source_never_transient(self):
        """scope 违规等非构建失败，即使有记录也绝不标 transient（方向性闸）。"""
        v = self._verdict(self._details(
            l1_2_1_build_ok=None, build_failed=None,
            scope_violations=["src/evil.ts"]))
        assert "failure_class" not in v.details

    def test_helper_truth_table_fail_closed(self):
        assert lv._a2_push_undelivered_still_failing({}) is False
        assert lv._a2_push_undelivered_still_failing(
            {"a2_push_undelivered": "dirty"}) is False
        assert lv._a2_push_undelivered_still_failing(
            {"a2_push_undelivered": {"coords": []},
             "build_output": "Cannot find module 'x'"}) is False
        # 有记录有坐标但无任何构建错误文本 → False（无证据不判）
        assert lv._a2_push_undelivered_still_failing(
            {"a2_push_undelivered": {"coords": ["left-pad"]}}) is False
        # build_error_lines 单独命中也算（build_output 压缩截断时的第二证据源）
        assert lv._a2_push_undelivered_still_failing(
            {"a2_push_undelivered": {"coords": ["left-pad"]},
             "build_error_lines": ["Cannot find module 'left-pad'"]}) is True

    def test_go_missing_module_line_is_extracted(self):
        """复核 reviewer MEDIUM：go 缺模块三形态必须进 _SIGNAL_PATTERNS——否则
        build_error_lines 对 Go 恒空，transient 判据只剩被压缩的 build_output
        一条命（缺模块行落省略区间=W-22 原危害链 Go 分支复发）。口径对齐
        sibling_dep_repair._GO_MISSING_RE。"""
        from swarm.worker.output_compress import extract_error_lines
        raw = ("some noise\n"
               "no required module provides package github.com/foo/bar; to add it:\n"
               "missing go.sum entry for module providing package github.com/baz/qux\n"
               'cannot find package "github.com/old/dep" in any of:\n'
               "more noise\n")
        lines = extract_error_lines(raw)
        assert any("no required module provides package" in x for x in lines)
        assert any("missing go.sum entry" in x for x in lines)
        assert any("cannot find package" in x for x in lines)
        # 端到端：压缩丢了 build_output 也能凭 build_error_lines 判 transient
        assert lv._a2_push_undelivered_still_failing(
            {"a2_push_undelivered": {"coords": ["github.com/foo/bar"]},
             "build_output": "",
             "build_error_lines": lines}) is True
