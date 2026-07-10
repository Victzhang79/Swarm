"""D02 回归：plan 后升级的 ModuleLock 在异常路径不再泄漏。

机制（治本前）：_stream_brain_events 内 plan 节点把模块锁 upgrade 成新锁（旧锁 release、
返回新锁），但新锁只存在于该函数【局部变量】，仅正常 return 才经元组传回调用方 finally。
plan 升级后若事件循环内抛任何异常（本例用 RuntimeError 模拟节点异常），函数异常退出不 return
→ 调用方 module_lock 仍指向已被 release 的旧锁 → finally 释放旧锁是 no-op → 升级后的新锁无人
释放。Redis 关闭走进程内 threading.Lock 兜底时【永久】持有至进程重启，同项目后续任务全被拒。

治本：锁经可变容器 lock_holder 传引用，升级时原地写回 lock_holder["lock"]，调用方 finally
释放 lock_holder["lock"]——正常/异常路径都释放到【当前实际持有的锁】。

本测试用进程内锁兜底路径（get_redis 返回 None，最严重的永久泄漏场景）做行为断言：
注入"plan 升级锁后抛异常"，断言升级后的新锁被释放（对应进程内 threading.Lock 可再被获取）。
"""
import asyncio
from unittest.mock import patch

from swarm.infra import redis_client
from swarm.infra.redis_client import ModuleLock, _local_lock_for, module_key_from_plan


_PLAN_DICT = {"subtasks": [{"scope": {"writable": ["srcmod/Foo.java"]}}]}


def _plan_end_event():
    """模拟 LangGraph 在 plan 节点完成时发的 on_chain_end 事件（携 plan 输出）。"""
    return {
        "event": "on_chain_end",
        "name": "plan",
        "data": {"output": {"plan": dict(_PLAN_DICT)}},
    }


class _FakeGraph:
    """astream_events 先吐一个 plan 完成事件（触发锁升级），随后抛异常模拟节点异常。"""

    def __init__(self, exc: Exception):
        self._exc = exc

    async def astream_events(self, graph_input, config=None, version=None):
        yield _plan_end_event()
        raise self._exc

    async def aget_state(self, config):  # 异常先于此调用，不会触达
        return None


def test_upgraded_module_lock_released_on_exception_after_plan():
    from swarm.brain import runner

    project_id = "d02proj"
    old_key = "default"
    new_key = module_key_from_plan(_PLAN_DICT)  # 由 writable 首段推导 → "srcmod"
    assert new_key != old_key, "测试前提：plan 应触发真正的锁升级（key 改变）"

    boom = RuntimeError("simulated node failure after plan upgrade")
    fake_graph = _FakeGraph(boom)

    # get_redis→None：走进程内 threading.Lock 兜底（Redis 关闭时的永久泄漏场景）。
    with patch.object(redis_client, "get_redis", return_value=None), \
         patch.object(runner, "get_compiled_brain_graph", return_value=fake_graph), \
         patch.object(runner, "_sync_task_from_state", lambda *a, **k: None), \
         patch.object(runner.store, "get_task",
                      return_value={"description": "t", "project_id": project_id}), \
         patch.object(runner.store, "check_task_token_limit", return_value=(True, {})):

        # 调用方持有的旧锁（default）。
        old_lock = ModuleLock(project_id, old_key)
        assert old_lock.acquire() is True
        lock_holder = {"lock": old_lock}

        async def _drive():
            await runner._stream_brain_events(
                "task-d02", {"task_id": "task-d02"}, runner._FanoutTopic(),
                project_id=project_id, lock_holder=lock_holder,
            )

        # plan 升级后抛异常 → 应向上抛出，函数不 return。
        try:
            asyncio.run(_drive())
            raised = None
        except RuntimeError as exc:
            raised = exc
        assert raised is boom, "异常应原样向上传播"

        # 治本核心断言 1：holder 已在抛异常前被写回升级后的新锁（不是仍指向旧锁）。
        current = lock_holder["lock"]
        assert current is not old_lock
        assert current.module_key == new_key

        # 旧锁在 upgrade 内已被 release（进程内锁应可再获取）。
        old_local = _local_lock_for(old_lock.key)
        assert old_local.acquire(blocking=False) is True
        old_local.release()

        # E3 语义演进：升级产物是【全部写集】组合锁 MultiModuleLock——逐子键探持有性。
        _sub_keys = [lk.key for lk in current._locks]
        for _k in _sub_keys:
            _probe = _local_lock_for(_k)
            assert _probe.acquire(blocking=False) is False, f"{_k} 应持有中"

        # 模拟调用方 finally：释放 holder 里的【当前】锁（治本后 finally 走 lock_holder["lock"]）。
        current.release()

        # 治本核心断言 2：升级后的新锁被释放 → 进程内锁重新可获取，同项目后续任务不再被永久拒。
        for _k in _sub_keys:
            _probe = _local_lock_for(_k)
            assert _probe.acquire(blocking=False) is True, \
                "升级后的模块锁在异常路径必须被释放（D02 泄漏已治本）"
            _probe.release()
