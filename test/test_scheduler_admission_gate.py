"""批3 回归：调度器准入闸门——项目沙箱未就绪时任务仅入池不启动。

docs/DESIGN_project_sandbox_prebake_source.md §5.1。
"""

from __future__ import annotations

from unittest.mock import patch

from swarm.brain import scheduler


def test_ready_project_admitted():
    with patch("swarm.project.store.get_project", return_value={"status": "READY"}):
        assert scheduler._project_ready_for_exec("p1") is True


def test_preprocessing_project_held():
    with patch("swarm.project.store.get_project", return_value={"status": "PREPROCESSING"}):
        assert scheduler._project_ready_for_exec("p1") is False


def test_building_sandbox_held():
    # building_sandbox 是 phase，status 仍 PREPROCESSING → 留池
    with patch("swarm.project.store.get_project", return_value={"status": "PREPROCESSING"}):
        assert scheduler._project_ready_for_exec("p1") is False


def test_error_project_held():
    # E12（阶段5）语义演进：ERROR 项目不再"留池等待"（旧口径 200 次×3s 后强制放行=
    # 注定失败的白烧）——三态准入返回 "error"，消费循环 fail-fast 标任务 FAILED。
    with patch("swarm.project.store.get_project", return_value={"status": "ERROR"}):
        assert scheduler._project_exec_admission("p1") == "error"
        assert scheduler._project_ready_for_exec("p1") is True  # 不留池（fail-fast 由调用侧执行）


def test_missing_project_conservatively_admitted():
    # 项目记录缺失 → 保守放行（交由 runner 处理，不卡死队列）
    with patch("swarm.project.store.get_project", return_value=None):
        assert scheduler._project_ready_for_exec("p1") is True


def test_db_error_conservatively_admitted():
    with patch("swarm.project.store.get_project", side_effect=RuntimeError("db down")):
        assert scheduler._project_ready_for_exec("p1") is True


def test_admission_retry_cap_exists():
    # 防 ERROR 项目无限 re-enqueue 的上限存在且合理（覆盖最长构建耗时）
    assert scheduler._MAX_ADMISSION_RETRIES >= 100
    assert isinstance(scheduler._admission_retries, dict)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== 批3 调度器准入闸门: {len(fns)}/{len(fns)} passed ===")
