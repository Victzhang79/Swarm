"""统一栈驱动层（27 号文 §6）。

L0 键层 / L1 事实表 / L2 能力驱动三层。本包**只放栈知识**，不放业务编排：
调用方永远问"这个栈的 X 是什么"，绝不在自己那边写 `if stack == "maven"`。
"""

from swarm.stacks.spec import (  # noqa: F401
    DEPENDENCY_TREE_DIRS,
    STACK_SPEC,
    StackSpec,
    aggregate_manifest_of_stack,
    aggregate_manifests_of_stack,
    build_manifest_basenames,
    demote_safety_net,
    is_compilable_source,
    is_root_aggregate_manifest,
    is_structural_build_manifest,
    layout_segments_union,
    module_manifest_of_stack,
    module_manifests_of_stack,
    root_aggregate_manifests,
    root_manifests_by_stack,
    spec_for_stack,
    stack_of_manifest,
    stack_of_structural_manifest,
    structural_manifests,
    unregistered_aggregate_stacks,
    workspace_container_segments_union,
)
