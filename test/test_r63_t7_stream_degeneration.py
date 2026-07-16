"""R63-T7 治本锁：worker 复读/退化看门狗 + 模型换挡。

round63 实锤（logs_archive/round63_postmortem/swarm.noheartbeat.log）：
  · st-2-1-1-2（L7464）：`IllegalArgumentEx` 同标识符在正文里高密度复读（预览窗 ×12），
    4 次重启、3 次撞满 900s 墙钟（累计 ~2700s 白烧）；
  · st-8（L6429）：整句循环「我意识到我一直在犯一个循环错误。让我停下来仔细思考」+
    `LinkedHash Map`（带错误空格）自相矛盾复读，迭代上限 95 + 900s 双重终止；
  · st-2-1-1（L3926）：截断类名 `IllegalArgumentExce` 复读并写入源码 → 下游 cannot find symbol。
肇事模型均为本地 Qwopus3.6-27B-v2-NVFP4；复读载体是【正文 content】，chunk 持续产出 →
stall 看门狗/R55 思考预算/max_tokens 全部抓不到，只能等 900s 墙钟或迭代上限。

治本三面：
  ① models/degeneration.py 流式复读探测（词洪泛 + 句循环双通道，密度+多样性判据——
     register 原文「≥3× 即中止」按证据修正：真实代码里 public×2995，纯计数必误杀）；
  ② 触发即 abort 流 + 抛 StreamDegenerationError（capability，绝不 transient）：
     非链尾 → with_fallbacks 同请求内切下一模型（in-request 换挡）；
     链尾 → 传播为子任务失败，l1_decision_source=degeneration_hard_fail → brain
     FINDING-12 通路 force_strong 升最强模型重派；
  ③ T7-0（调查中实锤的真 bug）：ChatGenerationChunk 没有 .content 属性 →
     router._astream_inner 的 content_seen 恒 False → R55「正文已开吐绝不重开」保护
     对真实流失效（旧 test_r55 用自带 .content 的假 chunk 假绿）。权威取 .text。
"""
from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessageChunk
from langchain_core.outputs import ChatGenerationChunk


def _real_chunk(text: str = "", reasoning: str = "") -> ChatGenerationChunk:
    """真实生产形态的流式 chunk（ChatOpenAI._astream 的 yield 类型）。"""
    ak = {"reasoning_content": reasoning} if reasoning else {}
    return ChatGenerationChunk(message=AIMessageChunk(content=text, additional_kwargs=ak))


# ── round63 实锤复读样本（按主日志预览重建） ────────────────────────────
# st-2-1-1-2 形态：同标识符高密度洪泛（词通道）
_R63_IDENTIFIER_LOOP_UNIT = (
    "`IllegalArgumentEx` — 这应该是 `IllegalArgumentEx` 的缩写，但 Java 标准库中是 "
    "`IllegalArgumentEx`。让我确认：实际上 Java 标准库中的类名是 `IllegalArgumentEx`，"
    "这是正确的。等等，让我再确认——Java 标准库中是 `IllegalArgumentEx` 还是 "
    "`IllegalArgumentEx`？"
)
# st-8 形态：整句循环（句通道）
_R63_SENTENCE_LOOP_UNIT = (
    "我意识到我一直在犯一个循环错误。让我停下来仔细思考一下这个问题。"
    "Java 标准库中的 Map 实现类名是 `LinkedHash Map`。这是错误的写法。"
    "正确的类名是 `LinkedHash Map`（没有空格）。但我一直写成了带空格的形式。"
)

# ── 合法高重复文本（绝不许误杀） ────────────────────────────────────────
_LEGIT_SPRING_IMPORTS = "\n".join(
    f"import org.springframework.{pkg};"
    for pkg in (
        "beans.factory.annotation.Autowired",
        "beans.factory.annotation.Qualifier",
        "boot.autoconfigure.SpringBootApplication",
        "boot.context.properties.ConfigurationProperties",
        "context.annotation.Bean",
        "context.annotation.Configuration",
        "data.redis.core.RedisTemplate",
        "http.HttpStatus",
        "http.ResponseEntity",
        "scheduling.annotation.EnableScheduling",
        "scheduling.annotation.Scheduled",
        "stereotype.Component",
        "stereotype.Service",
        "transaction.annotation.Transactional",
        "util.CollectionUtils",
        "util.StringUtils",
        "web.bind.annotation.GetMapping",
        "web.bind.annotation.PostMapping",
        "web.bind.annotation.RequestBody",
        "web.bind.annotation.RequestMapping",
        "web.bind.annotation.RequestParam",
        "web.bind.annotation.RestController",
    )
) + "\nimport java.util.List;\nimport java.util.Map;\nimport java.util.Date;\n"

_LEGIT_CONSTANTS = "\n".join(
    f'    public static final String ALARM_{name} = "{i}";'
    for i, name in enumerate(
        ("STATUS_OK", "STATUS_FAIL", "STATUS_PENDING", "STATUS_RETRY", "TYPE_EMAIL",
         "TYPE_SMS", "TYPE_WEBHOOK", "TYPE_VOICE", "LEVEL_INFO", "LEVEL_WARN",
         "LEVEL_ERROR", "LEVEL_FATAL", "CHANNEL_WECHAT", "CHANNEL_LARK",
         "CHANNEL_DINGTALK", "CHANNEL_SLACK", "MODE_SYNC", "MODE_ASYNC",
         "MODE_BATCH", "MODE_STREAM", "SCOPE_GLOBAL", "SCOPE_TENANT",
         "SCOPE_PROJECT", "SCOPE_USER")
    )
)

_LEGIT_POM_DEPS = "\n".join(
    "        <dependency>\n"
    "            <groupId>org.springframework.boot</groupId>\n"
    f"            <artifactId>spring-boot-starter-{a}</artifactId>\n"
    "        </dependency>"
    for a in ("web", "aop", "data-redis", "validation", "test", "actuator",
              "quartz", "mail", "security", "cache")
)

_LEGIT_STUBS = "\n".join(
    f"    @Override\n    public AlarmTask select{m}(Long id) {{\n"
    f"        // TODO: {m} 查询逻辑\n        return null;\n    }}"
    for m in ("ById", "ByName", "ByStatus", "ByLevel", "ByChannel", "ByTenant")
)


# ═══════════════ A. 探测器单元 ═══════════════

def _det(**kw):
    from swarm.models.degeneration import StreamRepetitionDetector
    return StreamRepetitionDetector(**kw)


def test_word_flood_fires_on_round63_identifier_loop():
    """★头号锁★ st-2-1-1-2 形态：同标识符高密度洪泛必须触发（词通道）。"""
    det = _det()
    verdict = None
    for _ in range(8):
        verdict = verdict or det.feed(_R63_IDENTIFIER_LOOP_UNIT)
    assert verdict is not None, "round63 实锤标识符洪泛必须被探测"
    assert verdict.channel == "word_flood"
    assert verdict.needle == "IllegalArgumentEx"
    assert verdict.count >= 8


def test_segment_loop_fires_on_round63_sentence_loop():
    """★头号锁★ st-8 形态：整句块循环必须触发（句通道；词通道密度不够抓不住）。"""
    det = _det()
    verdict = None
    for _ in range(8):
        verdict = verdict or det.feed(_R63_SENTENCE_LOOP_UNIT)
    assert verdict is not None, "round63 实锤整句循环必须被探测"
    assert verdict.channel == "segment_loop"
    assert verdict.count >= 4


def test_no_fire_on_legit_spring_import_block():
    """合法 import 块：springframework 高频出现但每行是新类名——绝不许误杀。"""
    det = _det()
    for _ in range(3):  # 反复喂多份（模拟长文件流过窗口）
        assert det.feed(_LEGIT_SPRING_IMPORTS) is None, "合法 import 块被误判为复读"


def test_no_fire_on_legit_constants_file():
    """合法常量文件：public static final String 结构性重复——绝不许误杀。"""
    det = _det()
    for _ in range(3):
        assert det.feed(_LEGIT_CONSTANTS) is None, "合法常量文件被误判为复读"


def test_no_fire_on_legit_pom_dependency_list():
    """合法 pom 依赖表：<groupId> 行字面全同——绝不许误杀（worker 天天写 pom）。"""
    det = _det()
    for _ in range(3):
        assert det.feed(_LEGIT_POM_DEPS) is None, "合法 pom 依赖表被误判为复读"


def test_no_fire_on_legit_stub_methods():
    """合法 stub 方法组：return null; / @Override 重复——绝不许误杀。"""
    det = _det()
    for _ in range(3):
        assert det.feed(_LEGIT_STUBS) is None, "合法 stub 方法被误判为复读"


# ── 复核 R-F1/R-F2 实锤误杀锁（中文合法高重复 + SQL 种子） ────────────────

def test_no_fire_on_plan_json_shared_acceptance():
    """★复核 R-F1 锁（HIGH）★ brain 规划 JSON：多个 subtask 共享同一条中文验收句是
    健康输出（本系统主领域），绝不许在流中途杀掉规划调用。"""
    plan = "\n".join(
        "  {\n"
        f'    "id": "st-{i}",\n'
        f'    "description": "{d}",\n'
        '    "acceptance_criteria": ["编译通过", "单元测试通过", "符合契约约束"],\n'
        f'    "create_files": ["src/main/java/com/ruoyi/alarm/{f}.java"]\n'
        "  },"
        for i, (d, f) in enumerate((
            ("创建告警任务实体类与字段校验", "domain/AlarmTask"),
            ("实现告警任务服务接口的增删改查", "service/IAlarmTaskService"),
            ("实现告警渠道配置的持久化层", "mapper/AlarmChannelMapper"),
            ("实现邮件渠道的发送适配器", "channel/EmailChannelAdapter"),
            ("实现短信渠道的发送适配器", "channel/SmsChannelAdapter"),
            ("实现告警调度器的定时触发逻辑", "scheduler/AlarmScheduler"),
            ("实现告警记录的查询控制器", "controller/AlarmLogController"),
            ("实现告警模板的渲染引擎", "template/AlarmTemplateEngine"),
            ("实现告警去重与抑制策略", "dedup/AlarmSuppressor"),
            ("实现告警升级链路的状态机", "escalate/AlarmEscalator"),
        ))
    )
    det = _det()
    for i in range(0, len(plan), 50):  # 流式逐块喂入
        assert det.feed(plan[i:i + 50]) is None, \
            f"规划 JSON 共享验收句被误杀（offset={i}）"


def test_no_fire_on_prd_list_shared_acceptance_phrase():
    """★复核 R-F1 锁★ PRD 需求抽取列表：每条需求共享同一句验收措辞——健康输出。"""
    prd = "\n".join(
        f"{i}. {d}。验收标准：管理员可增删改查该配置，操作结果需持久化并可审计追溯。"
        for i, d in enumerate((
            "支持邮件渠道的告警推送配置", "支持短信渠道的告警推送配置",
            "支持企微机器人渠道的告警推送", "支持飞书渠道的告警推送配置",
            "支持钉钉渠道的告警推送配置", "支持语音电话渠道的告警推送",
            "支持告警模板的自定义变量渲染", "支持告警的分级抑制与静默窗口",
            "支持告警升级链路的多级审批", "支持告警记录的全文检索与导出",
        ))
    )
    det = _det()
    for i in range(0, len(prd), 50):
        assert det.feed(prd[i:i + 50]) is None, f"PRD 共享验收措辞被误杀（offset={i}）"


def test_no_fire_on_stub_shared_chinese_javadoc_and_literal():
    """★复核 R-F1 锁★ 多个 stub 方法共享同一段中文 Javadoc + 同一句异常字面量——健康代码。"""
    code = "\n".join(
        "    /** 查询告警数据，失败时抛业务异常，调用方需处理降级 */\n"
        f"    public AlarmTask select{m}(Long id) {{\n"
        '        throw new ServiceException("系统繁忙，请稍后重试");\n'
        "    }"
        for m in ("ById", "ByName", "ByStatus", "ByLevel", "ByChannel",
                  "ByTenant", "ByTime", "ByGroup")
    )
    det = _det()
    for i in range(0, len(code), 50):
        assert det.feed(code[i:i + 50]) is None, f"共享中文 Javadoc/字面量被误杀（offset={i}）"


def test_no_fire_on_sql_seed_repeated_timestamp():
    """★复核 R-F2 锁（MED）★ SQL 种子数据：每行 create_time/update_time 双列复用
    CURRENT_TIMESTAMP——健康播种写法（ctx 比率恰打 0.5 临界，须严格小于）。"""
    sql = "\n".join(
        f"INSERT INTO sys_alarm_channel VALUES ({i}, '渠道{i}', 'admin', "
        "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);"
        for i in range(40)
    )
    det = _det()
    for i in range(0, len(sql), 50):
        assert det.feed(sql[i:i + 50]) is None, f"SQL 种子双列同值被误杀（offset={i}）"


def test_word_flood_still_fires_on_single_segment_flood():
    """句多样性闸只在句数足够时生效——单行无标点的纯标识符洪泛（1 句）仍必须触发。"""
    det = _det()
    blob = ("IllegalArgumentEx " * 40).strip()  # 无任何句分隔符
    verdict = None
    for _ in range(4):
        verdict = verdict or det.feed(blob)
    assert verdict is not None and verdict.channel == "word_flood", \
        "单句洪泛不得被句多样性闸放走"


def test_fires_incrementally_mid_stream():
    """流式增量喂入（40 字符/chunk）也必须在流中途触发，不是只在收尾时。"""
    det = _det()
    blob = _R63_IDENTIFIER_LOOP_UNIT * 12
    fired_at = None
    for i in range(0, len(blob), 40):
        if det.feed(blob[i:i + 40]) is not None:
            fired_at = i
            break
    assert fired_at is not None, "增量流中途必须触发"
    assert fired_at < len(blob) - 40, "必须在流结束前触发（不是最后一口才报）"


def test_short_text_never_fires():
    """未达最小累计量（min_chars）绝不触发——短回复天然重复度高，不误杀。"""
    det = _det()
    assert det.feed("好的好的好的好的好的") is None


def test_reset_clears_state():
    """reset 后旧窗口清空（R56-1 关 thinking 重开流时必须配套 reset）。"""
    det = _det()
    for _ in range(6):
        det.feed(_R63_IDENTIFIER_LOOP_UNIT)
    det.reset()
    assert det.feed("正常的一句话。") is None


# ═══════════════ B. T7-0：真实 chunk 形态的文本抽取（R55 隐性 bug） ═══════════════

def test_chunk_text_extracts_from_real_chatgenerationchunk():
    """★T7-0 锁★ ChatGenerationChunk 没有 .content 属性——旧 getattr(chunk,'content')
    恒取空 → content_seen 永假。权威抽取必须用 .text。"""
    from swarm.models.router import _gen_chunk_text
    c = _real_chunk("正文内容")
    assert not hasattr(c, "content"), \
        "前提：真实 chunk 无 .content（若 langchain 升级了此形态，重审 T7-0）"
    assert _gen_chunk_text(c) == "正文内容"
    assert _gen_chunk_text(_real_chunk("")) == ""


def test_reasoning_text_extracts_from_additional_kwargs():
    from swarm.models.router import _chunk_reasoning_text
    assert _chunk_reasoning_text(_real_chunk("", reasoning="思考中")) == "思考中"
    assert _chunk_reasoning_text(_real_chunk("正文")) == ""


# ═══════════════ C. 路由层集成（_astream_inner 钩子） ═══════════════

def _dual(**kw):
    from swarm.models.router import _DualTimeoutChatOpenAI
    defaults = dict(
        model="fake", api_key="x", base_url="http://x/v1",
        swarm_first_token_timeout=5, swarm_inter_chunk_timeout=5,
    )
    defaults.update(kw)
    return _DualTimeoutChatOpenAI(**defaults)


@pytest.mark.asyncio
async def test_astream_aborts_and_raises_on_degenerate_stream(monkeypatch):
    """★核心锁★ 复读流 → 中途 abort（agen.aclose 释放 GPU）+ 抛 StreamDegenerationError。"""
    from langchain_openai import ChatOpenAI
    from swarm.models.errors import StreamDegenerationError

    closed = {"v": False}

    async def _fake_astream(self, *args, **kwargs):
        try:
            for _ in range(400):
                yield _real_chunk(_R63_IDENTIFIER_LOOP_UNIT[:60])
                await asyncio.sleep(0)
        finally:
            closed["v"] = True

    monkeypatch.setattr(ChatOpenAI, "_astream", _fake_astream, raising=False)
    llm = _dual(swarm_degen_enabled=True)
    n_out = 0
    with pytest.raises(StreamDegenerationError):
        async for _ in llm._astream([]):
            n_out += 1
    assert closed["v"], "必须 aclose 底层流（让 vLLM abort 解码释放 GPU）"
    assert n_out < 400, "必须中途触发，不是吐完才报"


@pytest.mark.asyncio
async def test_degeneration_error_is_not_transient_shaped(monkeypatch):
    """★换挡语义锁★ 错误首行绝不含 transient 关键词——否则 _breaker_error_transient
    误喂熔断 + classify_failure 误归 transient（退避重试同一模型 = 白烧）。"""
    from langchain_openai import ChatOpenAI
    from swarm.models.errors import CAPABILITY, StreamDegenerationError, classify_failure
    from swarm.models.router import _breaker_error_transient

    async def _fake_astream(self, *args, **kwargs):
        for _ in range(400):
            yield _real_chunk(_R63_IDENTIFIER_LOOP_UNIT[:60])
            await asyncio.sleep(0)

    monkeypatch.setattr(ChatOpenAI, "_astream", _fake_astream, raising=False)
    llm = _dual(swarm_degen_enabled=True)
    with pytest.raises(StreamDegenerationError) as ei:
        async for _ in llm._astream([]):
            pass
    exc = ei.value
    assert classify_failure(exc) == CAPABILITY, "复读退化=模型能力问题，必须归 capability 换模型"
    assert not _breaker_error_transient(repr(exc)), \
        "错误首行不得命中 transient 关键词（否则误喂熔断，健康模型被摘）"


@pytest.mark.asyncio
async def test_healthy_varied_stream_passes_through(monkeypatch):
    """健康多样流（真实常量文件 + import 块）全量透传，零干预。"""
    from langchain_openai import ChatOpenAI

    blob = _LEGIT_SPRING_IMPORTS + "\n" + _LEGIT_CONSTANTS + "\n" + _LEGIT_POM_DEPS

    async def _fake_astream(self, *args, **kwargs):
        for i in range(0, len(blob), 50):
            yield _real_chunk(blob[i:i + 50])

    monkeypatch.setattr(ChatOpenAI, "_astream", _fake_astream, raising=False)
    llm = _dual(swarm_degen_enabled=True)
    out = ""
    async for c in llm._astream([]):
        out += c.text
    assert out == blob, "健康流必须完整透传"


@pytest.mark.asyncio
async def test_degen_disabled_flag_passes_degenerate_stream(monkeypatch):
    """总开关关闭（SWARM_MODEL_STREAM_DEGEN_ENABLED=0）→ 复读流不干预（可观测降级出口）。"""
    from langchain_openai import ChatOpenAI

    async def _fake_astream(self, *args, **kwargs):
        for _ in range(60):
            yield _real_chunk(_R63_IDENTIFIER_LOOP_UNIT[:60])

    monkeypatch.setattr(ChatOpenAI, "_astream", _fake_astream, raising=False)
    llm = _dual(swarm_degen_enabled=False)
    n = 0
    async for _ in llm._astream([]):
        n += 1
    assert n == 60


@pytest.mark.asyncio
async def test_reasoning_channel_fires_on_degenerate_reasoning(monkeypatch):
    """reasoning 通道：思维链复读（正文未吐）也要抓——比 R55 的 600s 时间预算快得多。"""
    from langchain_openai import ChatOpenAI
    from swarm.models.errors import StreamDegenerationError

    async def _fake_astream(self, *args, **kwargs):
        for _ in range(400):
            yield _real_chunk("", reasoning=_R63_SENTENCE_LOOP_UNIT[:60])
            await asyncio.sleep(0)

    monkeypatch.setattr(ChatOpenAI, "_astream", _fake_astream, raising=False)
    llm = _dual(swarm_degen_enabled=True)
    with pytest.raises(StreamDegenerationError):
        async for _ in llm._astream([]):
            pass


# ── 猎手 F1-F4 整改锁 ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_agent_reraises_degeneration_even_with_recursion_needle():
    """★猎手 F1 锁（CRITICAL）★ _run_agent 的「撞迭代上限」子串启发式（'recursion' in
    str(exc)）不得吞掉 StreamDegenerationError——needle 恰含 recursion 字样（模型卡壳时
    念叨 RecursionError 很常见）时，异常必须原样上抛，升档信号不丢。"""
    import time
    from types import SimpleNamespace

    from swarm.models.errors import StreamDegenerationError
    from swarm.worker.executor_agent import _AgentLoopMixin

    class _FakeExec(_AgentLoopMixin):
        def __init__(self, agent):
            self._agent = {"agent": agent}
            self.start_time = time.monotonic()
            self.max_execution_time = 60
            self.max_iterations = 10
            self.subtask = SimpleNamespace(
                id="st-x", difficulty=SimpleNamespace(value="medium"))
            self.project_id = "p"
            self.task_id = "t"
            self.phase = SimpleNamespace(value="coding")

        def _log(self, _m):
            pass

    class _RaisingAgent:
        async def ainvoke(self, *_a, **_k):
            raise StreamDegenerationError(
                "stream degeneration: 流式输出复读退化（模型能力问题，换模型档）\n"
                "channel=word_flood needle='infiniteRecursion' count=12")

    with pytest.raises(StreamDegenerationError):
        await _FakeExec(_RaisingAgent())._run_agent("hi")

    class _RecursionAgent:  # 对照：真 GraphRecursionError 形态仍优雅返回（老行为不破）
        async def ainvoke(self, *_a, **_k):
            raise RuntimeError("GraphRecursionError: recursion limit reached")

    out = await _FakeExec(_RecursionAgent())._run_agent("hi")
    assert "迭代上限" in out


@pytest.mark.asyncio
async def test_detector_self_exception_fails_open(monkeypatch, caplog):
    """★猎手 F2 锁★ 探测器自身异常 → fail-open：健康流完整透传 + WARNING 可观测。"""
    import logging

    from langchain_openai import ChatOpenAI

    from swarm.models.degeneration import StreamRepetitionDetector

    def _boom(self, _text):
        raise ValueError("detector internal bug")

    monkeypatch.setattr(StreamRepetitionDetector, "feed", _boom)

    async def _fake_astream(self, *args, **kwargs):
        for i in range(30):
            yield _real_chunk(f"第{i}行正常内容。\n")

    monkeypatch.setattr(ChatOpenAI, "_astream", _fake_astream, raising=False)
    llm = _dual(swarm_degen_enabled=True)
    out = ""
    with caplog.at_level(logging.WARNING, logger="swarm.models.router"):
        async for c in llm._astream([]):
            out += c.text
    assert out.count("正常内容") == 30, "探测器故障不得杀死健康流"
    assert any("探测器内部异常" in r.message for r in caplog.records), \
        "fail-open 必须可观测（WARNING），不许静默"


def test_chunk_text_no_deprecated_text_call():
    """★猎手 F3 锁★ .text 是 TextAccessor（str 子类带 __call__）——不得走 t() 方法式
    调用（每 chunk 一条弃用告警，且 langchain 2.0 移除后会静默掉兜底）。"""
    import warnings

    from swarm.models.router import _gen_chunk_text

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        assert _gen_chunk_text(_real_chunk("正文")) == "正文"
    assert not any("deprecat" in str(x.message).lower() for x in w), \
        "抽取正文不得触发 .text() 弃用调用"


def test_chunk_text_handles_list_content_fallback():
    """★猎手 F4 锁★ .text 缺失且 content 是多模态 block 列表时，正文不得被判空
    （否则 content_seen 恒假的老 bug 换形态复活）。"""
    from types import SimpleNamespace

    from swarm.models.router import _gen_chunk_text

    fake = SimpleNamespace(content=["前", {"type": "text", "text": "后"}, {"type": "image"}])
    assert _gen_chunk_text(fake) == "前后"


# ═══════════════ D. 换挡接线（worker/brain 升档通路） ═══════════════

def test_with_fallbacks_switches_model_on_degeneration():
    """★in-request 换挡锁★ 非链尾复读 → with_fallbacks 同请求内切下一模型
    （载荷假设：langchain fallback 默认捕获 Exception 子类——若升级后收窄，此锁变红）。"""
    from langchain_core.runnables import RunnableLambda
    from swarm.models.errors import StreamDegenerationError

    calls: list[str] = []

    def _primary(_x):
        calls.append("primary")
        raise StreamDegenerationError("stream degeneration: 复读退化（测试）")

    def _fallback(_x):
        calls.append("fallback")
        return "ok-from-fallback"

    chain = RunnableLambda(_primary).with_fallbacks([RunnableLambda(_fallback)])
    assert chain.invoke("x") == "ok-from-fallback"
    assert calls == ["primary", "fallback"], "复读必须触发链内切换到下一模型"


def test_exception_l1_details_marks_degeneration_hard_fail():
    """worker 侧异常落账：StreamDegenerationError → l1_decision_source=degeneration_hard_fail。"""
    from swarm.models.errors import StreamDegenerationError
    from swarm.worker.l1_verdict import exception_l1_details

    exc = StreamDegenerationError(
        "stream degeneration: 复读退化",
        evidence={"channel": "word_flood", "needle": "IllegalArgumentEx", "count": 12},
    )
    d = exception_l1_details(exc, "capability")
    assert d["l1_decision_source"] == "degeneration_hard_fail"
    assert d["failure_class"] == "capability"
    assert d["degeneration_evidence"]["needle"] == "IllegalArgumentEx"

    d2 = exception_l1_details(RuntimeError("boom"), None)
    assert "l1_decision_source" not in d2, "普通异常不得冒充退化标记"
    assert d2["error"] == "boom"


def test_handle_failure_force_strong_on_degeneration():
    """★brain 升档锁★ degeneration_hard_fail 子任务重试 → force_strong（下轮最强模型）。
    round56「思考失控先换模型」只覆盖 reasoning 时间预算（且本地 worker 关 thinking 永不触发）；
    本锁把【内容复读退化】接进同一升档通路。"""
    import asyncio as _aio
    from unittest.mock import patch

    from swarm.brain.nodes import handle_failure
    from swarm.types import FileScope, SubTask, TaskPlan, WorkerOutput

    def _wo(sid, ok, source=None):
        return WorkerOutput(
            subtask_id=sid, diff="--- a\n+++ b\n@@ -1 +1 @@\n-a\n+b\n" if ok else "",
            summary="", l1_passed=ok,
            l1_details={"l1_decision_source": source} if source else {},
            confidence="high" if ok else "low",
        )

    plan = TaskPlan(subtasks=[
        SubTask(id="st-1", description="d", scope=FileScope(writable=["a.java"])),
        SubTask(id="st-2", description="d", scope=FileScope(writable=["b.java"])),
    ])
    state = {
        "failed_subtask_ids": ["st-2"],
        "subtask_results": {"st-1": _wo("st-1", True),
                            "st-2": _wo("st-2", False, source="degeneration_hard_fail")},
        "subtask_retry_counts": {},
        "dispatch_remaining": [],
        "plan": plan,
    }

    async def _inv(_self, _msgs):
        class R:
            content = '{"strategy":"retry","reasoning":"x"}'
        return R()

    with patch("swarm.brain.nodes._get_brain_llm") as m:
        m.return_value.ainvoke = _inv.__get__(m.return_value)
        r = _aio.run(handle_failure(state))
    assert (r.get("subtask_force_strong") or {}).get("st-2") is True, \
        f"复读退化子任务必须升档最强模型: {r.get('subtask_force_strong')}"


# ═══════════════ E. 配置面 ═══════════════

def test_model_config_degen_defaults():
    from swarm.config.settings import ModelConfig
    f = ModelConfig.model_fields
    assert f["stream_degen_enabled"].default is True
    assert f["stream_degen_window_chars"].default == 1200
    assert f["stream_degen_word_repeats"].default == 8
    assert f["stream_degen_seg_repeats"].default == 4


def test_env_registry_registers_degen_keys():
    from swarm.config.env_registry import REGISTERED_ENVS
    for k in ("SWARM_MODEL_STREAM_DEGEN_ENABLED",
              "SWARM_MODEL_STREAM_DEGEN_WINDOW_CHARS",
              "SWARM_MODEL_STREAM_DEGEN_WORD_REPEATS",
              "SWARM_MODEL_STREAM_DEGEN_SEG_REPEATS"):
        assert k in REGISTERED_ENVS, f"{k} 未登记（阶段7 冻结面）"


def test_router_transmits_degen_config():
    """get_chat_model 必须把 config 的 degen 参数透传到 _DualTimeoutChatOpenAI 实例。"""
    from swarm.config.settings import ModelConfig, ProviderConfig
    from swarm.models.router import EndpointProvider

    mc = ModelConfig()
    provider = EndpointProvider(
        ProviderConfig(id="t7test", label="t", kind="local",
                       base_url="http://x/v1", api_key="x"),
        mc,
    )
    m = provider.get_chat_model("fake-model")
    assert m.swarm_degen_enabled is bool(mc.stream_degen_enabled)
    assert m.swarm_degen_window == int(mc.stream_degen_window_chars)
    assert m.swarm_degen_word_repeats == int(mc.stream_degen_word_repeats)
    assert m.swarm_degen_seg_repeats == int(mc.stream_degen_seg_repeats)


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
