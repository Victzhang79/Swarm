"""R23-1：verify_l2/l3 的同步阻塞调用必须放线程池，不卡 async 事件循环。"""
import inspect
from swarm.brain.nodes import verify


def test_verify_l2_offloads_integration_review():
    src = inspect.getsource(verify.verify_l2)
    assert "asyncio.to_thread" in src and "run_integration_review" in src, \
        "verify_l2 的 run_integration_review 必须经 asyncio.to_thread 卸到线程池"


def test_verify_l3_offloads_poll():
    src = inspect.getsource(verify.verify_l3)
    assert "asyncio.to_thread" in src and "trigger_and_poll_pipeline" in src, \
        "verify_l3 的 trigger_and_poll_pipeline 必须经 asyncio.to_thread 卸到线程池"
