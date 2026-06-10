"""Quick sanity check for all modified components"""

# 1. Test _parse_json_from_llm with both str and list content
from swarm.brain.nodes import _parse_json_from_llm

# str input
assert _parse_json_from_llm('{"key": "value"}') == {"key": "value"}
assert _parse_json_from_llm('```json\n{"key": "value"}\n```') == {"key": "value"}
print("✅ _parse_json_from_llm(str) works")

# list input (multimodal content)
list_content = [{"type": "text", "text": '{"key": "value"}'}]
assert _parse_json_from_llm(list_content) == {"key": "value"}
print("✅ _parse_json_from_llm(list) works")

# list with string items — this is edge case: multiple JSON strings joined
# Not a real scenario from LLM output, just confirm no crash on single-item list
list_content2 = ['{"a": 1}']
result = _parse_json_from_llm(list_content2)
assert result == {"a": 1}
print(f"  list of strings result: {result}")

# 2. Verify get_worker_llm mapping
from swarm.models.router import ModelRouter

r = ModelRouter()
for strategy in ["cost_optimized", "quality", "complex"]:
    llm = r.get_worker_llm(strategy=strategy)
    print(f"✅ get_worker_llm(strategy='{strategy}') → {type(llm).__name__}")

# 3. Verify dispatch and _dispatch_to_worker are async
import inspect

from swarm.brain.nodes import _dispatch_to_worker, dispatch

assert inspect.iscoroutinefunction(dispatch), "dispatch should be async"
assert inspect.iscoroutinefunction(_dispatch_to_worker), "_dispatch_to_worker should be async"
print("✅ dispatch and _dispatch_to_worker are async")

# 4. Verify WorkerExecutor.project_path
from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality
from swarm.worker.executor import WorkerExecutor

st = SubTask(
    id="test-1", description="test",
    difficulty=SubTaskDifficulty.MEDIUM, modality=SubTaskModality.TEXT,
    scope=FileScope(writable=[], readable=[]),
    contract={}, acceptance_criteria=[], depends_on=[],
)
ex = WorkerExecutor(subtask=st, project_path="/custom/path")
assert ex.project_path == "/custom/path", f"Expected /custom/path, got {ex.project_path}"
print("✅ WorkerExecutor.project_path works")

# 5. Verify graph compiles with async dispatch
from swarm.brain.graph import compile_brain_graph

compiled = compile_brain_graph()
assert 'dispatch' in compiled.nodes
print("✅ Graph compiles with async dispatch node")

print("\n🎉 All sanity checks passed!")
