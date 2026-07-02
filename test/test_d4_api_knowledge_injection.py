"""D4(b) 治本回归：plan 声明的依赖 artifact → 命中"常幻觉库→正确 API 签名"知识表 →
注入相关子任务 context_snippets，消除本地小模型对第三方库类名/方法名的幻觉。

round18 st-16：worker 对 OkHttp 客户端类名产生盲区+退化死循环(把 okhttp3.OkHttpClient 写成
OkHttp / 方法名退化 executeecute)烧光 900s。通用治法(非硬编 okhttp=B 类 hack)：小型可扩展知识表
(key=groupId:artifactId 或 import 前缀)，按 plan.shared_contract.dependencies 命中，把正确签名
片段确定性注入需要它的子任务(写源码的)，不注入纯 pom/注册子任务。
"""

from __future__ import annotations

from swarm.brain.contract_utils import _API_KNOWLEDGE, inject_api_knowledge
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality, TaskPlan

_J = "ruoyi-alarm/src/main/java/com/ruoyi/alarm/channel"


def _plan(deps, subtasks):
    return TaskPlan(subtasks=subtasks, shared_contract={"dependencies": deps})


def _impl_st(sid="st-16-2", f=f"{_J}/impl/SlackNotifyService.java"):
    return SubTask(id=sid, description="Slack 渠道 impl", difficulty=SubTaskDifficulty.MEDIUM,
                   modality=SubTaskModality.TEXT, scope=FileScope(create_files=[f]))


def _pom_st(sid="st-1", mod="ruoyi-alarm"):
    return SubTask(id=sid, description="建模块 pom", difficulty=SubTaskDifficulty.MEDIUM,
                   modality=SubTaskModality.TEXT, scope=FileScope(create_files=[f"{mod}/pom.xml"]))


def test_okhttp_injected_into_source_subtask():
    deps = [{"module": "ruoyi-alarm", "artifacts": ["com.squareup.okhttp3:okhttp:4.12.0"]}]
    impl = _impl_st()
    plan = _plan(deps, [_pom_st(), impl])
    assert inject_api_knowledge(plan) is True
    snip = impl.context_snippets
    assert "OkHttpClient" in snip, "应注入正确类名 OkHttpClient"
    assert "okhttp3" in snip
    # 引导 JDK 备选(避开第三方盲区)
    assert "java.net.http.HttpClient" in snip


def test_pom_only_subtask_not_injected():
    deps = [{"module": "ruoyi-alarm", "artifacts": ["com.squareup.okhttp3:okhttp:4.12.0"]}]
    pom = _pom_st()
    inject_api_knowledge(_plan(deps, [pom, _impl_st()]))
    assert not (pom.context_snippets or ""), "纯 pom/注册子任务不应被注入 API 片段"


def test_no_injection_when_lib_not_declared():
    deps = [{"module": "ruoyi-alarm", "artifacts": ["org.projectlombok:lombok"]}]
    impl = _impl_st()
    assert inject_api_knowledge(_plan(deps, [impl])) is False
    assert not (impl.context_snippets or "")


def test_bare_import_prefix_hits():
    """依赖以裸 import 前缀声明(okhttp3)也应命中。"""
    deps = [{"module": "ruoyi-alarm", "artifacts": ["okhttp3"]}]
    impl = _impl_st()
    assert inject_api_knowledge(_plan(deps, [impl])) is True
    assert "OkHttpClient" in impl.context_snippets


def test_idempotent_no_duplicate():
    deps = [{"module": "ruoyi-alarm", "artifacts": ["com.squareup.okhttp3:okhttp"]}]
    impl = _impl_st()
    plan = _plan(deps, [impl])
    assert inject_api_knowledge(plan) is True
    first = impl.context_snippets
    assert inject_api_knowledge(plan) is False, "二次注入应幂等(replan 安全)"
    assert impl.context_snippets == first
    assert impl.context_snippets.count("OkHttpClient") == first.count("OkHttpClient")


def test_preserves_existing_snippets():
    deps = [{"module": "ruoyi-alarm", "artifacts": ["okhttp3"]}]
    impl = _impl_st()
    impl.context_snippets = "已有的 scope 片段（enrich 注入）"
    inject_api_knowledge(_plan(deps, [impl]))
    assert "已有的 scope 片段" in impl.context_snippets, "不得覆盖已有片段(与 enrich 叠加)"
    assert "OkHttpClient" in impl.context_snippets


def test_sole_physical_module_fallback():
    """契约以逻辑模块名声明依赖，但代码落进唯一物理模块 → 仍应命中(A5 同风格 fallback)。"""
    deps = [{"module": "alarm-robot", "artifacts": ["com.squareup.okhttp3:okhttp"]}]
    impl = _impl_st()  # 物理模块 ruoyi-alarm，与契约 module 名不同
    plan = _plan(deps, [impl])
    assert inject_api_knowledge(plan) is True
    assert "OkHttpClient" in impl.context_snippets


def test_no_deps_spec_noop():
    impl = _impl_st()
    assert inject_api_knowledge(TaskPlan(subtasks=[impl], shared_contract={})) is False


def test_knowledge_table_not_project_hardcoded():
    """知识表 entry 以库 artifact 为 key，不含项目/模块名(非 B 类 hack)。"""
    for entry in _API_KNOWLEDGE:
        assert entry.get("artifacts"), "每条须有 artifacts key"
        blob = " ".join(entry["artifacts"]).lower()
        assert "ruoyi" not in blob and "alarm" not in blob, "key 不得绑定具体项目"


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== D4(b) API 知识注入: {len(fns)}/{len(fns)} passed ===")
