"""D4(a) 治本回归：把【N 个同层平行独立实现】(策略/插件模式)的单子任务确定性拆成一实现一子任务。

round18 st-16 真形态：6 个渠道通知 impl(SlackNotifyService/DingTalkNotifyService/企微/邮件/webhook)
塞进【一个】子任务 → worker 被迫在一个上下文里串烧 6 套异构外部集成(各自 HTTP 客户端)→ 迭代/预算
耗尽 + 对某库 API 幻觉即拖垮整批(okhttp3.OkHttpClient 退化死循环烧光 900s)。这类实现【彼此独立】
(只共享接口 INotifyService，不互相引用)，天然可一实现一子任务 —— 而旧 _split_oversized_by_files
只按 Controller 锚点/单实体分层拆，纯 Service 兄弟(无 Controller)落进单实体分支→不拆穿→never split。

核心不变量：
1. 同层平行独立实现家族(≥MIN_PARALLEL_IMPL_SIBLINGS)→ 每实现一批；共享接口/抽象成前置上游批。
2. 各 impl 批 readable 含上游接口批产物(可 import + 沙箱注入)，串行链，文件无丢失。
3. 单实体全栈/低于阈值的兄弟【绝不误拆】(不重蹈 RUN14 契约漂移)。
4. 跨语言/跨栈通用：纯路径+命名判据，不绑 Java。
"""

from __future__ import annotations

from swarm.brain.planning_nodes import (
    MIN_PARALLEL_IMPL_SIBLINGS,
    _detect_parallel_impls,
    _split_oversized_by_files,
    _split_parallel_impl_core,
)
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality

_J = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/channel"
_CHANNELS = ["Slack", "DingTalk", "WeChat", "Email", "Webhook"]


def _notify_subtask(sid="st-16"):
    """round18 st-16 真形态：接口 + 消息类 + 6 渠道 impl(实为 5，够过阈值)。"""
    creates = [f"{_J}/INotifyService.java", f"{_J}/NotifyMessage.java"]
    creates += [f"{_J}/impl/{c}NotifyService.java" for c in _CHANNELS]
    return SubTask(
        id=sid, description="实现多渠道通知：Slack/钉钉/企微/邮件/webhook 各一实现，统一 INotifyService。",
        difficulty=SubTaskDifficulty.COMPLEX, modality=SubTaskModality.TEXT,
        scope=FileScope(create_files=creates), depends_on=["st-1"],
        acceptance_criteria=["各渠道可发送"], est_context_tokens=70_000,
    )


# ── 检测器直测（返回 (leaves, upstream, downstream) 三元组）──
def test_detect_parallel_impls_by_dir_signal():
    core = [f"{_J}/INotifyService.java", f"{_J}/NotifyMessage.java"] + \
           [f"{_J}/impl/{c}NotifyService.java" for c in _CHANNELS]
    leaves, upstream, downstream = _detect_parallel_impls(core)
    assert len(leaves) == len(_CHANNELS), f"impl 目录下 {len(_CHANNELS)} 兄弟应成 leaves，实得 {leaves}"
    # 接口/消息类归 upstream(共享上游)，不进 leaves
    assert set(upstream) == {f"{_J}/INotifyService.java", f"{_J}/NotifyMessage.java"}
    assert downstream == []


def test_detect_parallel_impls_by_naming_signal_no_impl_dir():
    """无 impl 目录、纯命名信号(共享后缀 Handler + 各异前缀)也应识别。"""
    d = "app/src/main/java/com/x/notify"
    core = [f"{d}/SlackHandler.java", f"{d}/EmailHandler.java", f"{d}/SmsHandler.java"]
    leaves, upstream, downstream = _detect_parallel_impls(core)
    assert len(leaves) == 3 and upstream == [] and downstream == [], \
        f"3 个 *Handler 兄弟应成 leaves，实得 {leaves}/{upstream}/{downstream}"


def test_detect_below_threshold_returns_none():
    """低于阈值(2 个)不构成家族。"""
    core = [f"{_J}/impl/SlackNotifyService.java", f"{_J}/impl/EmailNotifyService.java",
            f"{_J}/INotifyService.java"]
    assert _detect_parallel_impls(core) is None


def test_detect_interface_excluded_from_family():
    """接口(I 前缀)即使在 impl 目录也不算平行实现，须归 upstream。"""
    core = [f"{_J}/impl/INotifyService.java"] + \
           [f"{_J}/impl/{c}NotifyService.java" for c in _CHANNELS]
    leaves, upstream, downstream = _detect_parallel_impls(core)
    assert f"{_J}/impl/INotifyService.java" in upstream, "接口应归上游 upstream"
    assert all("INotify" not in f for f in leaves)


# ── 回归修复(对抗审计 #1)：共享基类/抽象 → 上游批；工厂/协调者 → 下游批 ──
def test_base_class_routed_upstream():
    """BaseHandler 被 Bar/Foo/Baz 继承 → 须归 upstream(先建),否则子批 cannot find symbol。"""
    d = f"{_J}/impl"
    core = [f"{d}/BaseHandler.java", f"{d}/BarHandler.java",
            f"{d}/FooHandler.java", f"{d}/BazHandler.java"]
    leaves, upstream, downstream = _detect_parallel_impls(core)
    assert f"{d}/BaseHandler.java" in upstream, "基类应归上游(被各 leaf 继承)"
    assert len(leaves) == 3 and downstream == []


def test_base_class_upstream_built_first_and_readable():
    """经 _split_oversized_by_files：基类批在首,各 leaf 批 readable 含基类(编译不缺符号)。"""
    d = f"{_J}/impl"
    files = [f"{d}/BaseHandler.java", f"{d}/BarHandler.java", f"{d}/FooHandler.java",
             f"{d}/BazHandler.java", f"{d}/QuxHandler.java"]  # >4 文件方过 oversized 闸门
    st = SubTask(id="st-9", description="handler 家族", scope=FileScope(create_files=files))
    children = _split_oversized_by_files(st)
    assert children[0].scope.create_files == [f"{d}/BaseHandler.java"], "基类应为首批(上游)"
    for c in children[1:]:
        assert f"{d}/BaseHandler.java" in (getattr(c.scope, "readable", []) or []), \
            f"{c.id} readable 缺基类 → cannot find symbol"


def test_factory_routed_downstream_reads_all_leaves():
    """NotifyChannelFactory 实例化全部渠道 → 须归下游末批,readable 含全部 leaf(先建 leaf 再建工厂)。"""
    d = f"{_J}/impl"
    channels = [f"{d}/SlackChannel.java", f"{d}/DingChannel.java",
                f"{d}/EmailChannel.java", f"{d}/SmsChannel.java"]  # 4 leaf, +工厂=5 文件过闸门
    core = channels + [f"{d}/NotifyChannelFactory.java"]
    leaves, upstream, downstream = _detect_parallel_impls(core)
    assert f"{d}/NotifyChannelFactory.java" in downstream, "工厂应归下游(引用全部 leaf)"
    assert len(leaves) == 4
    st = SubTask(id="st-11", description="渠道+工厂", scope=FileScope(create_files=core))
    children = _split_oversized_by_files(st)
    last = children[-1]
    assert last.scope.create_files == [f"{d}/NotifyChannelFactory.java"], "工厂应为末批"
    readable = set(getattr(last.scope, "readable", []) or [])
    assert set(channels) <= readable, f"工厂末批 readable 应含全部 leaf，缺 {set(channels) - readable}"


def test_shared_plus_two_leaves_below_threshold_not_split():
    """剥离共享基类后 leaf 仅 2 个(<3) → 不成家族(不拆),守住 pre-D4 单子任务可编译。"""
    d = f"{_J}/impl"
    core = [f"{d}/BaseHandler.java", f"{d}/BarHandler.java", f"{d}/FooHandler.java"]
    assert _detect_parallel_impls(core) is None


# ── split 集成 ──
def test_st16_splits_one_channel_per_subtask():
    st = _notify_subtask()
    children = _split_oversized_by_files(st)
    # 1 批接口/消息(上游) + 5 批各渠道
    assert len(children) == 1 + len(_CHANNELS), f"应拆成 {1 + len(_CHANNELS)} 批，实得 {len(children)}"
    # 首批 = 共享接口 + 消息类
    first = children[0]
    assert set(first.scope.create_files) == {f"{_J}/INotifyService.java", f"{_J}/NotifyMessage.java"}
    # 后续每批恰好一个渠道 impl
    impl_batches = children[1:]
    for c in impl_batches:
        assert len(c.scope.create_files) == 1, f"每渠道一批(独立)，实得 {c.scope.create_files}"
        assert "/impl/" in c.scope.create_files[0] and c.scope.create_files[0].endswith("NotifyService.java")
    # 5 渠道全覆盖，无丢失
    impl_files = sorted(f for c in impl_batches for f in c.scope.create_files)
    assert impl_files == sorted(f"{_J}/impl/{c}NotifyService.java" for c in _CHANNELS)


def test_st16_impl_batches_read_interface_upstream():
    """A1：各渠道 impl 批 readable 必含上游接口批产物(import INotifyService + 沙箱注入)。"""
    st = _notify_subtask()
    children = _split_oversized_by_files(st)
    iface = f"{_J}/INotifyService.java"
    for c in children[1:]:
        readable = set(getattr(c.scope, "readable", []) or [])
        assert iface in readable, f"{c.id} readable 缺共享接口 {iface}: {readable}"


def test_st16_serial_chain_and_deps():
    st = _notify_subtask()
    children = _split_oversized_by_files(st)
    assert children[0].depends_on == ["st-1"], "首批继承父依赖"
    for i in range(1, len(children)):
        assert f"st-16-{i}" in children[i].depends_on, "后批串行依赖前批"


def test_st16_no_file_loss():
    st = _notify_subtask()
    children = _split_oversized_by_files(st)
    got = sorted(f for c in children for f in c.scope.create_files)
    assert got == sorted(st.scope.create_files), "拆分不得丢/重文件"


def test_st16_child_descriptions_self_contained():
    st = _notify_subtask()
    children = _split_oversized_by_files(st)
    for c in children:
        assert "INotifyService" in c.description or "统一" in c.description or len(c.description) > 100
        assert "批" in c.description


# ── 反向守卫：不误拆 ──
def test_single_entity_fullstack_not_split_as_parallel():
    """单实体全栈(domain/mapper/service/impl/controller 同词干)不被误判平行家族。"""
    j = "ruoyi-alarm/src/main/java/com/ruoyi/alarm"
    files = [
        f"{j}/domain/AlarmApp.java", f"{j}/mapper/AlarmAppMapper.java",
        "ruoyi-alarm/src/main/resources/mapper/alarm/AlarmAppMapper.xml",
        f"{j}/service/IAlarmAppService.java", f"{j}/service/impl/AlarmAppServiceImpl.java",
        f"{j}/controller/AlarmAppController.java",
    ]
    st = SubTask(id="st-2", description="AlarmApp 全栈", scope=FileScope(create_files=files))
    assert _split_oversized_by_files(st) == [st], "单实体全栈不得被平行拆分误伤"
    # core 仅 1 个 impl(AlarmAppServiceImpl)在 impl 目录 → 不成家族
    assert _detect_parallel_impls(files) is None


def test_two_siblings_below_threshold_not_split():
    """2 个渠道(低于阈值)+ 接口(共 3 文件)未过 oversized 阈值 → 原样不拆。"""
    core = [f"{_J}/INotifyService.java", f"{_J}/impl/SlackNotifyService.java",
            f"{_J}/impl/EmailNotifyService.java"]
    st = SubTask(id="st-x", description="双渠道", scope=FileScope(create_files=core))
    assert _split_oversized_by_files(st) == [st]


def test_parallel_impl_core_none_when_no_family():
    core = ["a/domain/Foo.java", "a/service/IFooService.java", "a/controller/FooController.java",
            "a/mapper/FooMapper.java", "a/x/Bar.java"]
    assert _split_parallel_impl_core(core) is None


# ── 跨栈通用(Go)──
def test_parallel_impls_cross_stack_go():
    d = "internal/senders"
    core = [f"{d}/email_sender.go", f"{d}/sms_sender.go", f"{d}/push_sender.go"]
    leaves, upstream, downstream = _detect_parallel_impls(core)
    assert len(leaves) == 3, "Go senders 目录 3 兄弟应识别(不绑 Java)"


def test_min_siblings_constant_sane():
    assert MIN_PARALLEL_IMPL_SIBLINGS >= 3, "阈值≥3，避免 2 个也拆"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== D4(a) 平行渠道拆分: {len(fns)}/{len(fns)} passed ===")
