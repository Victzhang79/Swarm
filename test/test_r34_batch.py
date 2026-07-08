"""R34 统一治本批（E2E_ROUND34_REGISTER.md，P0-P2 全治，用户拍板一批收口）。

G2/R34-6  脚手架 scope×验证命令结构性冲突：D3 Fix E（孤儿强制 -pl 曝光漏注册）与
          round29 A(c)（注册后于脚手架）互斥——自建模块窗口期改用清单本地构建。
G3/R34-1  token 黑洞客户端侧：流式+chunk 看门狗（断连促后端 abort 僵尸生成）+
          bisect 冷却退避。
G4/R34-8  申报语义漂移（批间推卸）：prompt 钉死"仅指存量代码"+申报∩covers 无害化。
G5/R34-2  bisect 哨兵：已知确定性超时批直接切分，免每 attempt 整批重烧 600s。
G6/R34-7  L2 探针沙箱缺构建工具 → 按沙箱不可用处理（infra 绝不冒充代码失败）。
G7/R34-3  跨 attempt 覆盖集增量日志（震荡可观测）。
"""

from __future__ import annotations

import asyncio
import json
import logging

from swarm.brain.nodes import _invoke_llm_abortable, _plan_ultra_batched
from swarm.worker.l1_pipeline import _scope_maven_command

REQ_A = "req-aaaa1111"
REQ_B = "req-bbbb2222"


# ─────────────── G2/R34-6: 自建模块清单本地构建 ───────────────

def _mk_maven_project(tmp_path, registered=("mod-x",)):
    root = tmp_path / "proj"
    (root / "mod-x").mkdir(parents=True)
    mods = "".join(f"<module>{m}</module>" for m in registered)
    (root / "pom.xml").write_text(
        f"<project><modules>{mods}</modules></project>")
    (root / "mod-x" / "pom.xml").write_text("<project/>")
    return str(root)


def test_self_scaffold_orphan_uses_local_manifest_build(tmp_path):
    """本子任务自建的未注册模块（模块 pom 在 modified 集）→ mvn -f 本地构建不进 reactor，
    且需上游产物的目标(compile)降级 validate（hunter Death B：sibling 依赖此刻 .m2 未装）。"""
    proj = _mk_maven_project(tmp_path)
    (tmp_path / "proj" / "new-mod").mkdir()
    (tmp_path / "proj" / "new-mod" / "pom.xml").write_text("<project/>")
    out = _scope_maven_command(
        "mvn -q compile", proj, ["new-mod/pom.xml", "new-mod/src/A.java"])
    assert out.startswith("mvn -f new-mod/pom.xml"), out
    assert "-pl" not in out, "自建模块窗口期绝不进 reactor（注册在后是设计使然）"
    assert "validate" in out and "compile" not in out, \
        "compile 须降级 validate（脚手架验模块良构，不需 sibling 产物 → 不死于依赖解析）"


def test_self_scaffold_validate_goal_unchanged(tmp_path):
    """脚手架目标本就是 validate（不需上游）→ 保持 validate，仅加 -f。"""
    proj = _mk_maven_project(tmp_path)
    (tmp_path / "proj" / "new-mod").mkdir()
    (tmp_path / "proj" / "new-mod" / "pom.xml").write_text("<project/>")
    out = _scope_maven_command(
        "mvn -q validate", proj, ["new-mod/pom.xml"])
    assert out == "mvn -f new-mod/pom.xml -q validate"


def test_existing_orphan_still_fail_closed_via_pl(tmp_path):
    """修改【既有】未注册模块（清单不在 modified）→ Fix E 原语义保留（曝光真漏注册）。"""
    proj = _mk_maven_project(tmp_path)
    (tmp_path / "proj" / "orphan-mod").mkdir()
    (tmp_path / "proj" / "orphan-mod" / "pom.xml").write_text("<project/>")
    out = _scope_maven_command("mvn -q compile", proj, ["orphan-mod/src/A.java"])
    assert "-pl orphan-mod" in out, "既有孤儿必须仍走 -pl fail-closed 曝光"


def test_registered_module_scoping_unchanged(tmp_path):
    """既有注册模块的 -pl -am 收窄行为零变化。"""
    proj = _mk_maven_project(tmp_path)
    out = _scope_maven_command("mvn -q compile", proj, ["mod-x/src/A.java"])
    assert "-pl mod-x -am" in out


def test_mixed_scaffold_and_registered_keeps_reactor_and_warns(tmp_path, caplog):
    """混合改动（自建模块+注册模块）→ 保留 reactor 行为，且自建模块不静默排除（复核 LOW#A：
    fail-loud WARNING 留痕，杜绝其 .java 未验证却读作 PASS）。"""
    proj = _mk_maven_project(tmp_path)
    (tmp_path / "proj" / "new-mod").mkdir()
    (tmp_path / "proj" / "new-mod" / "pom.xml").write_text("<project/>")
    import logging as _lg
    from swarm.worker import l1_pipeline as _l1
    recs: list[_lg.LogRecord] = []

    class _Cap(_lg.Handler):
        def emit(self, r):
            recs.append(r)

    h = _Cap()
    _l1.logger.addHandler(h)
    _l1.logger.setLevel(_lg.WARNING)
    try:
        out = _scope_maven_command(
            "mvn -q compile", proj, ["new-mod/pom.xml", "mod-x/src/A.java"])
    finally:
        _l1.logger.removeHandler(h)
    assert "-pl" in out and "mvn -f" not in out
    joined = "\n".join(r.getMessage() for r in recs)
    assert "混合子任务" in joined and "new-mod" in joined, "混合场景必须 fail-loud 留痕"


# ─────────────── G3/R34-1: 流式看门狗 ───────────────

class _StreamLLM:
    """有 astream 的桩：按脚本产出 chunk，支持注入停滞。"""

    def __init__(self, chunks, stall_after=None):
        self.chunks = chunks
        self.stall_after = stall_after
        self.closed = False

    def astream(self, messages):
        outer = self

        class _Gen:
            def __init__(self):
                self.i = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                if outer.stall_after is not None and self.i >= outer.stall_after:
                    await asyncio.sleep(999)
                if self.i >= len(outer.chunks):
                    raise StopAsyncIteration
                c = outer.chunks[self.i]
                self.i += 1
                return type("C", (), {"content": c})()

            async def aclose(self):
                outer.closed = True

        return _Gen()

    async def ainvoke(self, messages):
        raise AssertionError("有 astream 时绝不该走 ainvoke")


class _PlainLLM:
    async def ainvoke(self, messages):
        return type("R", (), {"content": "plain"})()


async def test_abortable_streams_and_concatenates():
    llm = _StreamLLM(["a", "b", "c"])
    r = await _invoke_llm_abortable(llm, [], 10)
    assert r.content == "abc"


async def test_abortable_stall_raises_timeout_and_closes(monkeypatch):
    """chunk 停滞超过 gap → TimeoutError 且生成器被 aclose（断连促后端 abort）。"""
    monkeypatch.setenv("SWARM_PLAN_BATCH_CHUNK_GAP", "0.3")
    llm = _StreamLLM(["a"], stall_after=1)
    import pytest
    with pytest.raises(asyncio.TimeoutError):
        await _invoke_llm_abortable(llm, [], 30)
    assert llm.closed is True, "超时必须关闭流（僵尸源头治理）"


async def test_abortable_fallback_without_astream():
    r = await _invoke_llm_abortable(_PlainLLM(), [], 5)
    assert r.content == "plain"


async def test_abortable_list_content_blocks_extracted():
    """hunter CONFIRMED：list content-block 必须抽 text 而非 str()（否则 repr 污染 JSON）。"""
    llm = _StreamLLM([
        [{"type": "text", "text": '{"a"'}],   # list content-block
        ": 1}",                                 # str chunk
    ])
    r = await _invoke_llm_abortable(llm, [], 10)
    assert r.content == '{"a": 1}', f"list 块须抽 text 拼接，不得 repr: {r.content!r}"
    assert json.loads(r.content) == {"a": 1}


# ─────────────── G5/R34-2 + G4/R34-8: 分批集成 ───────────────

def _fp(path, resp="r"):
    return {"path": path, "action": "create", "responsibility": resp}


def _payload(tag, n=1, extra=None):
    d = {"subtasks": [
        {"id": f"st-{tag}-{i}", "description": f"{tag} 工作 {i}",
         "scope": {"writable": [f"{tag}/f{i}"], "readable": []}}
        for i in range(n)]}
    d.update(extra or {})
    return json.dumps(d)


class _RouteLLM:
    def __init__(self, payloads, timeout_mods=()):
        self.payloads = payloads
        self.timeout_mods = set(timeout_mods)
        self.calls: dict[str, int] = {}

    async def ainvoke(self, messages):
        p = messages[-1]["content"]
        for mod, payload in self.payloads.items():
            if f"模块 '{mod}'" in p:
                self.calls[mod] = self.calls.get(mod, 0) + 1
                if mod in self.timeout_mods:
                    await asyncio.sleep(9)
                return type("R", (), {"content": payload})()
        raise AssertionError(f"no match: {p[:150]}")


def _state(extra=None):
    return {"tech_design": {}, "shared_contract_draft": {}, "project_id": "",
            **(extra or {})}


async def _run(llm, state, file_plan, monkeypatch):
    monkeypatch.setenv("SWARM_PLAN_BATCH_TIMEOUT", "2")
    monkeypatch.setenv("SWARM_PLAN_BATCH_MAX_ATTEMPTS", "1")
    monkeypatch.setenv("SWARM_PLAN_BATCH_MAX_FILES", "20")
    monkeypatch.setenv("SWARM_PLAN_BATCH_TIMEOUT_COOLDOWN", "0")
    return await _plan_ultra_batched(llm, state, "需求", {}, "", file_plan)


async def test_bisect_sentinel_skips_whole_batch_rerun(monkeypatch):
    """G5：上一轮 bisect 过的批 → 哨兵命中直接切分，整批零 LLM 调用零 600s 重烧。"""
    files = [_fp(f"slow/f{i}.txt") for i in range(4)]
    llm1 = _RouteLLM({"slow": _payload("w"), "slow~a": _payload("ha", 2),
                      "slow~b": _payload("hb")}, timeout_mods={"slow"})
    _p1, failed1, _b1, cache1 = await _run(llm1, _state(), files, monkeypatch)
    assert failed1 == []
    assert any(v.get("bisected") for v in cache1.values()), "整批签名必须落哨兵"
    # 第二轮（构造补齐重试态）：整批绝不真跑，半批缓存命中
    llm2 = _RouteLLM({"slow": _payload("w"), "slow~a": _payload("ha", 2),
                      "slow~b": _payload("hb")})
    state2 = _state({"plan_batch_failed_modules": [{"name": "其他", "files": 1,
                                                    "reason": "timeout"}],
                     "plan_batch_cache": cache1})
    _p2, _f2, _b2, cache2 = await _run(llm2, state2, files, monkeypatch)
    assert llm2.calls.get("slow") is None, "哨兵命中整批不得重烧"
    assert any(v.get("bisected") for v in cache2.values()), "哨兵随轮重写防陈旧"


async def test_declaration_overlapping_covers_dropped(monkeypatch):
    """G4：申报与兄弟批 covers 重叠 → 丢申报保 covers（批间推卸无害化）。"""
    from swarm.brain.nodes import plan
    import swarm.brain.nodes as nodes
    fake_content = json.dumps({
        "subtasks": [{"id": "st-1", "description": "x",
                      "scope": {"writable": ["a"], "readable": []},
                      "covers": [REQ_A]}],
        "parallel_groups": [["st-1"]],
        "baseline_covered": [
            {"id": REQ_A, "reason": "已在本计划其他批实现"},   # 与 covers 重叠 → 丢
            {"id": REQ_B, "reason": "现有代码 legacy 模块已支持"},  # 真基线形态 → 留
        ],
    })

    class _One:
        async def ainvoke(self, messages):
            return type("R", (), {"content": fake_content})()

    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _One())
    from swarm.types import Complexity
    out = await plan({
        "task_description": "t", "complexity": Complexity.MEDIUM,
        "requirement_items": [
            {"id": REQ_A, "text": "甲", "kind": "functional",
             "source_quote": "甲", "source": "description"},
            {"id": REQ_B, "text": "乙", "kind": "functional",
             "source_quote": "乙", "source": "description"},
        ],
    })
    assert [e["id"] for e in out["baseline_covered"]] == [REQ_B], \
        "重叠申报必须被无害化丢弃，仅申报无 covers 的保留"


def test_batched_prompt_pins_baseline_semantics():
    """G4：纪律文案钉死"仅指仓库当前已存在的代码，将建模块绝不申报"。"""
    from swarm.brain.nodes import _requirement_coverage_prompt_block
    block = _requirement_coverage_prompt_block([
        {"id": REQ_A, "text": "甲", "kind": "functional",
         "source_quote": "甲", "source": "description"}], batched=True)
    assert "当前已存在" in block and "绝不" in block


# ─────────────── G6/R34-7: L2 探针工具缺失 ───────────────

def test_l2_tool_missing_classified_as_sandbox_unavailable(monkeypatch, tmp_path):
    """探针沙箱缺构建工具（RC127/command not found）→ ran=False（沙箱不可用语义），
    绝不记成集成编译失败误导归因。"""
    from swarm.brain import nodes as nodes_mod

    class _Res:
        stdout = "/bin/bash: line 1: mvn: command not found\n__RC__127\n"
        stderr = ""

    class _Sb:
        sandbox_id = "s1"

    killed = []

    class _Mgr:
        def create(self, **kw):
            return _Sb()

        def sync_project_to_sandbox(self, *a, **kw):
            pass

        def run_command(self, sandbox, cmd, timeout=600):
            return _Res()

        def kill(self, sid):
            killed.append(sid)

    import swarm.worker.sandbox as sbx
    monkeypatch.setattr(sbx, "get_sandbox_manager", lambda: _Mgr())
    ran, ok, out, sid = nodes_mod._run_reactor_build_in_sandbox(
        str(tmp_path), "p1", "mvn -q compile", timeout=60)
    assert ran is False and ok is False, "工具缺失=infra，按沙箱不可用处理"
    assert killed == ["s1"], "探针沙箱必须被清理（finally 必杀不因早退漏杀）"


# ─────────────── G7/R34-3: 覆盖增量日志 ───────────────

async def test_coverage_delta_logged_across_attempts(monkeypatch):
    from swarm.brain.nodes import validate_plan
    import swarm.brain.nodes as nodes
    from swarm.types import FileScope, SubTask, SubTaskDifficulty, TaskPlan

    class _OkLLM:
        async def ainvoke(self, messages):
            return type("R", (), {"content": '{"valid": true, "issues": []}'})()

    monkeypatch.setattr(nodes, "_get_brain_llm", lambda: _OkLLM())
    records: list[logging.LogRecord] = []

    class _Cap(logging.Handler):
        def emit(self, r):
            records.append(r)

    h = _Cap()
    nodes.logger.addHandler(h)
    nodes.logger.setLevel(logging.INFO)
    try:
        st = SubTask(id="st-1", description="x",
                     difficulty=SubTaskDifficulty.MEDIUM,
                     scope=FileScope(writable=["a"], readable=[]), covers=[REQ_A])
        await validate_plan({
            "plan": TaskPlan(subtasks=[st], parallel_groups=[["st-1"]]),
            "task_description": "t", "complexity": "medium",
            "plan_retry_count": 1,
            "requirement_items": [
                {"id": REQ_A, "text": "甲", "kind": "functional",
                 "source_quote": "甲", "source": "description"},
                {"id": REQ_B, "text": "乙", "kind": "functional",
                 "source_quote": "乙", "source": "description"},
            ],
            # 上一轮 issue 提的是 REQ_A（本轮已修复），本轮新增 REQ_B
            "plan_validation_issues": [f"需求条目未被任何子任务覆盖: {REQ_A} — 甲"],
        })
    finally:
        nodes.logger.removeHandler(h)
    joined = "\n".join(r.getMessage() for r in records)
    assert "覆盖增量" in joined and REQ_B in joined and REQ_A in joined
