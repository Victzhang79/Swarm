"""26 号文 P0-证伪批：三条"治本被证明无效"的根治。

共性：机制写了、测试绿了、登记册打了 ✅，但在它自己的目标场景下完全不生效——
比没做更危险，因为团队会以为已经治好。

★本文件自身的元教训（对抗双复核 R1 逮到）★
初版守卫全是 `inspect.getsource(...)` 文本断言，复核用**突变实验**证明它们对真回归不敏感：
把 primary 的 `callbacks=[...]` 整块删掉（真回归），`src.count("ModelInvocationLogger") >= 3`
仍为真（注释里还有一次出现）→ 测试照绿。这正是本批要治的病在守卫自己身上复发。
现已全部改为行为级断言：spy 真实构造路径、真跑 shell 分支、断言实例属性。
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


# ══════════════════════════════════════════════
# V1（26 号文 C-13）：A3 凭据可观测对 brain 完全失效
# ══════════════════════════════════════════════

def _spy_brain_construction(monkeypatch):
    """spy 掉真实构造入口，回收 brain 三处 get_chat_model 的实参。"""
    from swarm.models.router import EndpointProvider, ModelRouter

    seen: list[tuple[list[str], object]] = []
    _orig = EndpointProvider.get_chat_model

    def spy(self, model_name, temperature=0.2, callbacks=None, **kw):
        seen.append(([getattr(c, "role", "") for c in (callbacks or [])],
                     kw.get("no_fallback")))
        return _orig(self, model_name, temperature, callbacks=callbacks, **kw)

    monkeypatch.setattr(EndpointProvider, "get_chat_model", spy)
    r = ModelRouter()
    r.get_brain_llm()
    r.get_brain_fallback_llm()
    return seen


def test_brain_paths_attach_invocation_logger(monkeypatch):
    """★A3 的 403 升 ERROR 挂在 ModelInvocationLogger.on_llm_error 上，而 brain 三处
    get_chat_model 此前【都不传 callbacks】（worker 侧六处全传）→ 回调在 brain 永不触发。
    而 round67m2 的 403 恰恰发生在 brain（k3 是 brain primary）：2h20m 零 WARNING。★

    当年的测试假绿在于：它手工构造 `ModelInvocationLogger("brain", …)` 直接调
    on_llm_error 断言 ERROR——而 `role="brain"` 这个取值**生产代码从不产生**。
    本测试走真实构造路径，断言【每一处】都挂上了 role 以 brain 打头的回调；
    删掉任意一处的 callbacks 都会红（复核用突变实验验过旧版对此不敏感）。
    """
    seen = _spy_brain_construction(monkeypatch)
    assert len(seen) == 3, f"brain 应有 primary/内嵌 fallback/显式 fallback 三处构造，实得 {len(seen)}"
    for roles, _ in seen:
        assert any(r.startswith("brain") for r in roles), \
            f"该构造点未挂 brain 侧可观测回调：roles={roles}"


def test_brain_explicit_fallback_is_marked_chain_tail(monkeypatch):
    """26 号文 E-H4：`get_brain_fallback_llm` 是 _invoke_llm_abortable 的最后一根稻草
    （外层墙钟超时后显式切备），却漏标 no_fallback → 备用再 reasoning runaway 时抛出
    无人接的 TransientInfraError，记"主备双失败"整批失败；标了才会走"关 thinking
    同模型重开"保产出。get_brain_llm 的内嵌 fallback 早就标了，这里是漏网。

    口径边界（复核 M-2 纠正）：no_fallback 只覆盖 **reasoning runaway** 一支；
    墙钟超时分支不看该标志，仍走"主备双失败"。"""
    seen = _spy_brain_construction(monkeypatch)
    assert [nf for _, nf in seen][1:] == [True, True], \
        "两条链尾（内嵌 fallback / 显式 fallback）都必须标 no_fallback"


def test_adversarial_reviewer_b_is_not_chain_tail(monkeypatch):
    """★复核 H3：`get_brain_fallback_llm` 有两类语义完全不同的消费者★
    adversarial_verify 的 reviewer B 把它当 primary 用（独立第二双眼睛）。若也标链尾，
    reasoning runaway 时会走 R56-1 "关 thinking 同模型重开"——而那条路自己的注释写明
    "实测会漏需求（round56：106→92 条，整块功能消失）"。于是 reviewer B 从
    "挂了 → 记 single_reviewer degraded → 挡 auto_accept" 静默变成
    "带降级推理照常出 verdict、账不写、auto_accept 不挡" = fail-open。"""
    from swarm.models.router import EndpointProvider, ModelRouter

    seen: list[object] = []
    _orig = EndpointProvider.get_chat_model

    def spy(self, model_name, temperature=0.2, callbacks=None, **kw):
        seen.append(kw.get("no_fallback"))
        return _orig(self, model_name, temperature, callbacks=callbacks, **kw)

    monkeypatch.setattr(EndpointProvider, "get_chat_model", spy)
    ModelRouter().get_brain_fallback_llm(chain_tail=False)
    assert seen == [False], f"reviewer B 绝不能是链尾语义：no_fallback={seen}"


def test_thinking_off_degrade_is_counted(monkeypatch):
    """★复核 M-2：关 thinking 重开是【已知会漏需求】的质量降级，不能只有一行 WARNING★
    E-H4 把 brain 切备的最后一根稻草从"响亮的主备双失败"换成了"安静地少产出"，
    降级面必须能被 /api/metrics 的降级计数查到。

    ★批25 GS-5w 换锁★ 原命题=「record_degrade("...thinking_off_reopen") 须在 thinking
    切换之前」（源码行序断言）。改为真驱动 _DualTimeoutChatOpenAI 的 reasoning
    runaway 降级分支：构造链尾实例（swarm_no_fallback=True），父类 _astream 换成
    【只吐思维链、超思考预算】的假流——触发"关 thinking 同模型重开"后第二流吐正文。
    断言：①降级计数真的被记（存在性）；②计数早于重开流发起（序的行为证据——
    生产代码里 record_degrade 之后才有第二次 super()._astream）；③重开请求确实带
    thinking=disabled（真切换，不是只记了账）；④消费端只收到重开后的正文。
    删什么会变红：删 record_degrade→事件流无 degrade；把它挪到重开之后→序断言红；
    删 thinking 切换→第二次流 kwargs 无 extra_body.thinking→红。"""
    import asyncio

    from langchain_openai import ChatOpenAI

    from swarm.infra import degrade as _degrade_mod
    from swarm.models.router import _DualTimeoutChatOpenAI

    monkeypatch.delenv("SWARM_CASSETTE_RECORD_DIR", raising=False)
    monkeypatch.delenv("SWARM_CASSETTE_REPLAY_DIR", raising=False)

    events: list[str] = []

    def _fake_record_degrade(name, *a, **k):
        events.append(f"degrade:{name}")

    monkeypatch.setattr(_degrade_mod, "record_degrade", _fake_record_degrade)

    class _Chunk:
        """正文判据单一权威是 .text（R63-T7-0）——空 text=纯思维链 chunk。"""

        def __init__(self, text):
            self.text = text

    stream_kwargs: list[dict] = []
    stream_count_holder = [0]

    def _fake_super_astream(self, *args, **kwargs):
        stream_count_holder[0] += 1
        idx = stream_count_holder[0]
        events.append(f"stream:{idx}")
        stream_kwargs.append(dict(kwargs))

        async def _gen():
            if idx == 1:
                await asyncio.sleep(0.05)   # 必烧穿思考预算（0.01s）
                yield _Chunk("")            # 纯思维链：正文一个字都没吐
                await asyncio.sleep(30)     # 不被判 runaway 就挂死（生产会 aclose 它）
            else:
                yield _Chunk("正文")
        return _gen()

    monkeypatch.setattr(ChatOpenAI, "_astream", _fake_super_astream)

    llm = _DualTimeoutChatOpenAI(
        model="fake-model", api_key="sk-fake",
        swarm_reasoning_phase_budget=0.01,
        swarm_no_fallback=True,   # 链尾：runaway 只能"关 thinking 同模型重开"
    )

    async def _consume():
        return [c.text async for c in llm._astream([{"role": "user", "content": "x"}])]

    received = asyncio.run(_consume())
    assert received == ["正文"], f"重开后消费端只应收到正文: {received}"
    assert "degrade:models.router.thinking_off_reopen" in events, \
        f"关 thinking 重开必须记降级计数（/api/metrics 可查），实际事件: {events}"
    assert stream_count_holder[0] == 2, "runaway 后必须用同一模型重开流"
    # 序的行为证据：降级记账必须早于重开流发起
    assert events.index("degrade:models.router.thinking_off_reopen") < events.index("stream:2"), \
        f"降级计数必须在真正重开之前记（挪到重开之后此处即红）: {events}"
    _eb = stream_kwargs[1].get("extra_body") or {}
    assert _eb.get("thinking") == {"type": "disabled"}, \
        f"重开请求必须真的关掉 thinking（只记账不切换=假降级）: {stream_kwargs[1]}"


# ══════════════════════════════════════════════
# V2（26 号文 P0-1）：B1 反回归段假阳性
# ══════════════════════════════════════════════

_ISSUE = ("新建类 simple name 'GenController'（ruoyi-admin/.../GenController.java，"
          "st-91-1）与 base 既有类 ruoyi-generator/.../GenController.java 同名异路径。")
_ISSUE_RENUMBERED = _ISSUE.replace("st-91-1", "st-128-1")


def _hist(gate: str, *texts):
    return [{"gate": gate, "text": t} for t in texts]


def test_no_regress_block_not_fooled_by_renumber():
    """★核心：全量重拆必然 renumber st-id，而 B1 的设计前提正是全量重拆★
    原按【原始字符串】比对 → 同一缺陷跨轮原串不等 → 恒判"已修" → 把**从未修复**的缺陷
    写进"此前轮次已修掉、绝不许回归"段。round67m2 报文级实证：轮3 的 46 个 plan_batch
    全带该段、块内 10 条 10/10 仍在当轮打回池里。假阳性率结构性趋近 100%。"""
    from swarm.brain.nodes import _no_regress_feedback_block
    blk = _no_regress_feedback_block({
        "plan_validation_issue_history": _hist("G1", _ISSUE),
        "plan_validation_issues": [_ISSUE_RENUMBERED],
        "plan_validation_gate": "G1",
    })
    assert not blk, "同一缺陷仅 st-id 变化，绝不能谎报'已修掉'"


def test_no_regress_block_still_reports_real_fixes():
    """闸不能矫枉过正：真修好了（本轮走到了更靠后的闸）仍要进反回归段。"""
    from swarm.brain.nodes import _no_regress_feedback_block
    blk = _no_regress_feedback_block({
        "plan_validation_issue_history": _hist("G1", _ISSUE),
        "plan_validation_issues": ["覆盖缺口：req-1 未被任何子任务 covers"],
        "plan_validation_gate": "coverage",     # G1 本轮跑过且通过了
    })
    assert blk and "GenController" in blk


def test_no_regress_block_not_fooled_by_cross_gate_bounce():
    """★复核 H-3：validate_plan 是 9 道【顺序早退】闸★
    轮 N 死在 G1（第 7 闸）、轮 N+1 死在 structure（第 2 闸）→ 本轮 issues 里没有任何
    G1 条目 → 历轮 G1 缺陷被写进"已修掉、绝不许回归"，**而 G1 本轮根本没被执行过**。
    与 renumber 假阳性同危害（谎报已修）、不同根，属"修一类先全仓捞 sibling"的漏捞。"""
    from swarm.brain.nodes import _no_regress_feedback_block
    blk = _no_regress_feedback_block({
        "plan_validation_issue_history": _hist("G1", _ISSUE),
        "plan_validation_issues": ["子任务 st-3 依赖未知任务 st-99"],
        "plan_validation_gate": "structure",    # 第 2 闸 → G1 本轮压根没跑
    })
    assert not blk, "本轮没跑过的闸，绝不能宣告它的历轮缺陷'已修掉'"


def test_no_regress_block_fail_closed_on_unknown_gate():
    """旧 checkpoint 里历史条目是裸字符串（无 gate）→ fail-closed 不进本段。
    宁可少一句提醒，也绝不谎报"你已经修好了"。"""
    from swarm.brain.nodes import _no_regress_feedback_block
    assert not _no_regress_feedback_block({
        "plan_validation_issue_history": [_ISSUE],      # 裸串=旧格式
        "plan_validation_issues": ["别的问题"],
        "plan_validation_gate": "coverage",
    })


def test_history_dedup_by_signature_not_raw_string():
    """累积账同样按签名去重——否则同一缺陷会因每轮 renumber 被反复追加，
    历史账无限膨胀且把真正的历轮缺陷淹掉。"""
    from swarm.brain.graph import _increment_plan_retry
    out = _increment_plan_retry({
        "plan_validation_issue_history": _hist("G1", _ISSUE),
        "plan_validation_issues": [_ISSUE_RENUMBERED],
        "plan_validation_gate": "G1",
        "plan_retry_count": 1,
    })
    assert len(out["plan_validation_issue_history"]) == 1


def test_history_keeps_structure_gate_issues_distinct():
    """★复核 M1：signature 的适用面必须与 H-5 严格对齐★
    `_gate_fuse_and_account` 早已裁定"structure 闸 issue 去 st-id 后判别力不足"
    （'st-3 依赖未知任务 st-99' 与 'st-7 依赖未知任务 st-40' 同签名）并在调用侧排除。
    累积账复用了同一把尺子却漏了这条排除面 → 6 条互异真违例只留 4 条，
    而反回归段的尾注还宣称"全量见此账"。"""
    from swarm.brain.graph import _increment_plan_retry
    out = _increment_plan_retry({
        "plan_validation_issue_history": [],
        "plan_validation_issues": [
            "子任务 st-3 依赖未知任务 st-99",
            "子任务 st-7 依赖未知任务 st-40",
            "子任务 st-5 出现在多个 parallel_groups",
        ],
        "plan_validation_gate": "structure",
        "plan_retry_count": 1,
    })
    assert len(out["plan_validation_issue_history"]) == 3, "structure 闸绝不能按签名塌缩"


def test_gate_order_covers_every_early_return():
    """闸序表是单一事实源：每个早退点写的 gate 都必须在表里，否则该闸的历轮缺陷
    会被 fail-closed 永久排除（少一句提醒无害，但表漏项应当被立刻发现）。"""
    import inspect
    import re

    from swarm.brain import nodes
    from swarm.brain.nodes import _VALIDATE_GATE_ORDER
    src = inspect.getsource(nodes.validate_plan)
    used = set(re.findall(r'"plan_validation_gate": "([^"]+)"', src))
    assert used, "validate_plan 每个早退点都必须打 gate 标"
    assert used <= set(_VALIDATE_GATE_ORDER), f"闸序表漏登记: {used - set(_VALIDATE_GATE_ORDER)}"


def test_b1_and_h5_share_one_signature_source():
    """★口径同源★：B1 与 H-5 熔断必须用同一个归一函数、同一条排除面。
    （复核 M4 指出旧版 getsource 的两个对象【都是 B1 侧】，从未碰 H-5——
     这条"防错配"的断言恰恰对错配不敏感。改为行为级：同一组 issues 喂两侧，
     断言它们对"是否同一缺陷"的判定一致。）"""
    from swarm.brain.graph import _increment_plan_retry
    from swarm.brain.nodes import _gate_fuse_and_account

    base = {"plan_validation_issue_history": _hist("G1", _ISSUE),
            "plan_validation_gate": "G1", "plan_retry_count": 1}
    b1_says_same = len(_increment_plan_retry(
        {**base, "plan_validation_issues": [_ISSUE_RENUMBERED]}
    )["plan_validation_issue_history"]) == 1

    _, acct_a = _gate_fuse_and_account({}, "G1", [_ISSUE], 1)
    _, acct_b = _gate_fuse_and_account({}, "G1", [_ISSUE_RENUMBERED], 1)
    h5_says_same = acct_a["sig"] == acct_b["sig"]

    assert b1_says_same == h5_says_same is True, "两把尺子对同一对 issue 的判定必须一致"


# ══════════════════════════════════════════════
# V3（26 号文 P0-2）：取证污染
# ══════════════════════════════════════════════

def _run_mirrors(tid: str, tag: str, tmp: str):
    return subprocess.run(
        ["bash", str(_ROOT / "scripts" / "e2e_mirrors.sh"), tid, tag, tmp],
        capture_output=True, text=True, timeout=60)


def _pgrep_count(pattern: str) -> int:
    r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    return len([x for x in r.stdout.split() if x.strip()])


@pytest.fixture
def mirror_sandbox():
    """真跑镜像脚本的夹具——收尾无条件清理，绝不把 daemon 漏给后续用例。"""
    tag = f"pytest{os.getpid()}"
    tid = f"pytest-tid-{os.getpid()}"
    yield tag, tid
    subprocess.run(["pkill", "-f", f"{tag}_mirrors/mirror_"], capture_output=True)
    time.sleep(0.5)
    subprocess.run(["pkill", "-f", f"tail -n0 -F .*{tid}"], capture_output=True)
    import shutil
    shutil.rmtree(_ROOT / "logs_archive" / "process" / f"{tag}_mirrors", ignore_errors=True)
    (_ROOT / "logs" / f"{tid}.log").unlink(missing_ok=True)


@pytest.mark.skipif(not (_ROOT / "scripts" / "e2e_mirrors.sh").exists(), reason="脚本缺失")
def test_startup_cleanup_actually_kills_the_writers(mirror_sandbox):
    """★复核 CRITICAL：`pkill -f <tag>_mirrors` 杀不掉真正的写入者★
    daemon 进程树是 `bash <tag>_mirrors/mirror_swarm.sh` → `tail -n0 -F` | `grep`，
    **只有 bash 的 argv 含 tag**——tail 的 argv 是日志路径、grep 的是 KW 串。
    26 号文实测的"9 个 tail 从 7/28 跨三轮一直在跑"正是这类孤儿：旧版 pgrep 探测不到、
    pkill 杀不掉，脚本却打印"已清理"给出虚假背书。
    旧版测试只断言脚本里出现 `pkill -f "${TAG}_mirrors"` 字面量——**这个 CRITICAL 下它照绿**。
    本测试真跑两次，断言进程数不递增。"""
    tag, tid = mirror_sandbox
    _run_mirrors(tid, tag, "/tmp")
    time.sleep(1)
    after1 = _pgrep_count(f"tail -n0 -F .*{tid}")
    assert after1 > 0, "第一次起跑应有 tail 写入者"
    _run_mirrors(tid, tag, "/tmp")
    time.sleep(1.5)
    after2 = _pgrep_count(f"tail -n0 -F .*{tid}")
    assert after2 == after1, f"重跑后写入者从 {after1} 涨到 {after2}——旧 daemon 未被真正清掉"


@pytest.mark.skipif(not (_ROOT / "scripts" / "e2e_mirrors.sh").exists(), reason="脚本缺失")
def test_cleaned_daemon_stops_writing_to_mirror(mirror_sandbox):
    """内容级验证（复核就是用这招证伪初版的）：清理后写入源码日志的新行
    只能出现【一次】。旧 daemon 若存活，同一行会被灌进同一个 mirror 文件两次。"""
    tag, tid = mirror_sandbox
    src = _ROOT / "logs" / f"{tid}.log"
    mirror = _ROOT / "logs_archive" / "process" / f"{tag}_mirrors" / "swarm_mirror.log"
    _run_mirrors(tid, tag, "/tmp")
    time.sleep(1)
    src.write_text("PLAN 起跑前\n")
    time.sleep(1)
    _run_mirrors(tid, tag, "/tmp")
    time.sleep(1)
    with src.open("a") as f:
        f.write("PLAN 清理之后唯一一行\n")
    time.sleep(1.5)
    assert mirror.read_text().count("PLAN 清理之后唯一一行") == 1


@pytest.mark.skipif(not (_ROOT / "scripts" / "e2e_mirrors.sh").exists(), reason="脚本缺失")
def test_mirror_uses_per_task_log_even_before_it_exists(mirror_sandbox):
    """★复核 H-1/M2：源选择不能是一次性 `[ -f ]` 快照★
    E2E 动作序是"提交任务 → 立刻起镜像"，而 per-task 文件由 _PerTaskFileHandler 惰性创建
    （要等第一条带 task ctx 的日志）；任务排队时守卫在起跑时刻**常态命中** →
    整轮锁死在全局日志上，治本对当轮等于没做。而 `tail -n0 -F` 本就为"文件尚不存在"设计。
    本测试在文件不存在时起镜像，随后创建并写入，断言镜像接上了。"""
    tag, tid = mirror_sandbox
    src = _ROOT / "logs" / f"{tid}.log"
    src.unlink(missing_ok=True)
    r = _run_mirrors(tid, tag, "/tmp")
    assert "per-task" in r.stdout
    time.sleep(1)
    src.write_text("PLAN 文件后建也要接上\n")
    time.sleep(1.5)
    mirror = _ROOT / "logs_archive" / "process" / f"{tag}_mirrors" / "swarm_mirror.log"
    assert "PLAN 文件后建也要接上" in mirror.read_text()


def test_per_task_log_handler_is_default_on(monkeypatch):
    """镜像改用 per-task 日志的前提：该 handler 默认开启。行为级断言（复核 L4：
    旧版用源码文本 + `.replace("_os.", "os.")` 打补丁，对格式化改动脆弱）。"""
    import logging

    from swarm.logging_config import _PerTaskFileHandler, setup_logging
    monkeypatch.delenv("SWARM_PER_TASK_LOGS", raising=False)
    setup_logging(force=True)
    # handler 挂在 "swarm" logger 上（不是 root）——这正是行为级断言优于源码扫描的地方：
    # 源码里写着 os.environ.get(...) 不代表 handler 真挂上了。
    hs = logging.getLogger("swarm").handlers
    assert any(isinstance(h, _PerTaskFileHandler) for h in hs), \
        f"per-task handler 默认应挂上（当前 handlers={[type(h).__name__ for h in hs]}）"
    monkeypatch.setenv("SWARM_PER_TASK_LOGS", "false")
    setup_logging(force=True)
    assert not any(isinstance(h, _PerTaskFileHandler)
                   for h in logging.getLogger("swarm").handlers), "开关关掉必须真的不挂"
    monkeypatch.delenv("SWARM_PER_TASK_LOGS", raising=False)
    setup_logging(force=True)


def test_mirror_script_syntax_ok():
    """shell 语法闸——这个脚本每轮 E2E 起跑都要跑，语法错=整轮没有取证。"""
    r = subprocess.run(["bash", "-n", str(_ROOT / "scripts" / "e2e_mirrors.sh")],
                       capture_output=True)
    assert r.returncode == 0, r.stderr.decode()[:400]
