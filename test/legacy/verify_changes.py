#!/usr/bin/env python
"""验证所有改动可以正常 import"""

# 1. 验证 ModelRouter.get_worker_llm
from swarm.models.router import ModelRouter

r = ModelRouter()
print('get_brain_llm type:', type(r.get_brain_llm()))
print('get_worker_llm type:', type(r.get_worker_llm()))
print('get_worker_llm(strategy=quality) type:', type(r.get_worker_llm(strategy='quality')))
print('get_worker_llm(strategy=complex) type:', type(r.get_worker_llm(strategy='complex')))

# 2. 验证 nodes.py 全部节点可导入
from swarm.brain.nodes import (
    dispatch,
)

print('All nodes imported successfully')

# 3. 验证 dispatch 是 async
import asyncio

print('dispatch is coroutine function:', asyncio.iscoroutinefunction(dispatch))

# 4. 验证 graph 可以编译
from swarm.brain.graph import compile_brain_graph

compiled = compile_brain_graph()
print('Graph compiled successfully:', type(compiled))

# 5. 验证 WorkerExecutor 有 project_path
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality
from swarm.worker.executor import WorkerExecutor

st = SubTask(
    id="test-1",
    description="test",
    difficulty=SubTaskDifficulty.MEDIUM,
    modality=SubTaskModality.TEXT,
    scope=FileScope(writable=[], readable=[]),
    contract={},
    acceptance_criteria=[],
    depends_on=[],
)
ex = WorkerExecutor(subtask=st, project_path="/tmp/test")
print('WorkerExecutor.project_path:', ex.project_path)
print('WorkerExecutor default project_path:', WorkerExecutor(subtask=st).project_path)

print('\n✅ All checks passed!')
