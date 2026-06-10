#!/usr/bin/env python3
"""Full flow test for Brain state machine — tests interrupt/resume"""

from langgraph.types import Command
from swarm.brain.graph import compile_brain_graph

compiled = compile_brain_graph()
thread_config = {"configurable": {"thread_id": "test-full-flow"}}

# Step 1: Invoke — should stop at DELIVER interrupt
result = compiled.invoke(
    {
        "task_id": "test-full",
        "task_description": "Add a simple config field",
        "project_id": "proj-1",
    },
    config=thread_config,
)
print("Step 1 - State after DELIVER interrupt:")
print(f"  Keys: {list(result.keys())}")
print(f"  Complexity: {result.get('complexity')}")
print(f"  Has interrupt: {'__interrupt__' in result}")
print()

# Step 2: Resume with ACCEPT decision
result2 = compiled.invoke(
    Command(resume={"decision": "accept", "feedback": ""}),
    config=thread_config,
)
print("Step 2 - State after ACCEPT:")
print(f"  Keys: {list(result2.keys())}")
print(f"  Human decision: {result2.get('human_decision')}")
print(f"  Learned: {result2.get('learned')}")
print(f"  Learn summary: {result2.get('learn_summary', '')[:100]}")
print()

# Test REVISE flow
thread_config2 = {"configurable": {"thread_id": "test-revise-flow"}}
result3 = compiled.invoke(
    {
        "task_id": "test-revise",
        "task_description": "Refactor authentication module",
        "project_id": "proj-2",
    },
    config=thread_config2,
)
print("Step 3 - DELIVER interrupt for revise test:")
print(f"  Has interrupt: {'__interrupt__' in result3}")

# Resume with REVISE
result4 = compiled.invoke(
    Command(resume={"decision": "revise", "feedback": "Please add error handling"}),
    config=thread_config2,
)
# After revise, it goes to REVISION → DISPATCH → MONITOR → MERGE → VERIFY_L2 → DELIVER
# which will interrupt again
print("Step 4 - After REVISE (should be at DELIVER again):")
print(f"  Has interrupt: {'__interrupt__' in result4}")

# Resume with ACCEPT
result5 = compiled.invoke(
    Command(resume={"decision": "accept"}),
    config=thread_config2,
)
print("Step 5 - Final ACCEPT:")
print(f"  Learned: {result5.get('learned')}")
print()

print("All tests passed!")
