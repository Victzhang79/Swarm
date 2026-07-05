"""Worker 沙箱生命周期混入 —— 从 worker/executor.py 抽出（round26 god-file 治理 Step4）。

LIFECYCLE 连通分量（3 方法）：远程沙箱创建/在沙箱内执行/销毁（create_sandbox/run_in_sandbox/
kill_sandbox）。跨簇仅调 self._log（核心类，MRO 解析）；禁 eager import worker.executor
（防 A6 环）——get_sandbox_manager 保持方法内 lazy import。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class _SandboxLifecycleMixin:
    """WorkerExecutor 的沙箱生命周期方法簇（见模块 docstring）。不持有自身状态。"""

    def create_sandbox(self) -> Any:
        """创建远程 CubeSandbox 实例（用于沙箱内代码执行和编译验证）"""
        from swarm.worker.sandbox import get_sandbox_manager

        manager = get_sandbox_manager()
        # 传 task_id/project_id，使 cancel_task 能按任务 kill_by_task 释放资源
        sandbox = manager.create(
            project_id=self.project_id,
            task_id=self.task_id,
            source="worker",
        )
        self._sandbox = sandbox
        self._sandbox_manager = manager
        self._log(f"远程沙箱已创建: {sandbox.sandbox_id}")
        return sandbox

    def run_in_sandbox(self, code: str, timeout: int = 30) -> str:
        """在远程沙箱中执行 Python 代码并返回输出"""
        if not hasattr(self, "_sandbox") or self._sandbox is None:
            self.create_sandbox()
        result = self._sandbox_manager.run_code(self._sandbox, code, timeout)
        output_parts = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.text:
            output_parts.append(f"→ {result.text}")
        if result.stderr:
            output_parts.append(f"STDERR: {result.stderr}")
        if result.error:
            output_parts.append(f"ERROR: {result.error}")
        return "\n".join(output_parts)

    def kill_sandbox(self) -> None:
        """释放远程沙箱：热池借来的归还(健康则回池)，否则销毁。"""
        if hasattr(self, "_sandbox") and self._sandbox is not None:
            from_pool = getattr(self, "_from_pool", False)
            pool = getattr(self, "_sandbox_pool", None)
            sid = self._sandbox.sandbox_id
            try:
                if from_pool and pool is not None:
                    # 归还：L1 通过/无异常的沙箱可复用；失败的不回池(可能脏)。
                    # TD2606-C4：项目专属镜像(_sandbox_has_source)把源码烤进 /workspace 且【不含 .git】。
                    # 池复用前 clean_workspace 的 `rm -rf /workspace` 会抹掉烤进的源码 → 下个任务缺源编译失败；
                    # 而仅"保留 /workspace"又会带入上个任务的改动破坏跨任务隔离。两难。故烤源沙箱【不回池】，
                    # 每任务从【缓存镜像】创建新容器（镜像层仍含源码+.m2/warmup，启动快），杜绝抹源与污染两种 bug。
                    # getattr 默认与 __init__ 一致取 False（fail-closed：缺标记=不复用脏沙箱）
                    reusable = bool(getattr(self, "_l1_passed_flag", False)) and not getattr(
                        self, "_sandbox_has_source", False)
                    pool.release(self._sandbox, reusable=reusable)
                    self._log(f"远程沙箱已归还热池(reusable={reusable}): {sid}")
                else:
                    self._sandbox_manager.kill(sid)
                    self._log(f"远程沙箱已销毁: {sid}")
            except Exception as e:
                self._log(f"沙箱释放失败: {e}")
            self._sandbox = None
            self._sandbox_manager = None
            self._sandbox_pool = None
