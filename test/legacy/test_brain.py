#!/usr/bin/env python3
"""Quick smoke test for Brain state machine"""

from swarm.brain.state import BrainState
from swarm.types import Complexity, HumanDecision

# Test BrainState
state: BrainState = {
    "task_id": "test-1",
    "task_description": "Add login feature",
    "project_id": "proj-1",
    "complexity": Complexity.MEDIUM,
}
print("BrainState OK:", state.get("complexity"))

# Test graph build
from swarm.brain.graph import build_brain_graph, compile_brain_graph

graph = build_brain_graph()
print("Graph built OK")

# Test compile
compiled = compile_brain_graph()
print("Graph compiled OK, type:", type(compiled).__name__)

# Test a simple invocation
result = compiled.invoke(
    {
        "task_id": "test-1",
        "task_description": "Add a simple config field",
        "project_id": "proj-1",
    },
    config={"configurable": {"thread_id": "test-thread-1"}},
)
print("Graph invoke OK")
print("Final state keys:", list(result.keys()))
print("Complexity:", result.get("complexity"))
print("Plan subtasks:", len(result.get("plan", {}).subtasks) if result.get("plan") else 0)
print("Learned:", result.get("learned"))
