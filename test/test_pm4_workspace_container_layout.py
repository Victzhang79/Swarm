"""P-M4（27 号文）：_SRC_LAYOUT_SEGMENTS 事实源化 + workspace 容器段不再塌模块。

治前两层病：
① 布局段是 contract_utils 手抄 12 段 frozenset——与 STACK_SPEC 无关，加栈不改它
   （两份手抄必漂移族，`_BUILD_MANIFESTS` 已实证两次）；
② npm workspace 容器（packages/apps）不在任何表里——`packages/api/index.ts`
   （源码不在 src/test 布局段内）判 _EV_WEAK_CODE → 模块根退成首段 "packages"
   ⇒ 全 workspace 塌成一个假模块（G1 物理根歧义/误并）。

治法定案：两类段分字段（血规 10③——消费后果不同绝不混一表）：
- layout_segments：命中=【切在它前面】；
- workspace_container_segments：命中=【容器+子目录一起算根】，position-0 判据
  （深处的 packages 可能是 Java 包名，误判=把包目录当 workspace）。
混进 layout 表会让 packages/api/src/x.ts 在 i=0 切出空根（设计期实证）。
"""
from __future__ import annotations

from swarm.brain.contract_utils import (
    _EV_STRONG,
    _EV_WEAK_CODE,
    _SRC_LAYOUT_SEGMENTS,
    _WORKSPACE_CONTAINER_SEGMENTS,
    _code_module_root,
    _common_module_prefix,
    _evidence_class,
)
from swarm.stacks import (
    STACK_SPEC,
    layout_segments_union,
    workspace_container_segments_union,
)


def test_layout_segments_union_equals_legacy():
    """★纯结构改动逐元素相等锁★：派生并集 == 旧手抄 12 段（不多不少——
    增/删任何一段都是独立行为变更，必须另开一条带自己判据的测试）。"""
    legacy = {"src", "main", "java", "kotlin", "scala", "resources",
              "test", "tests", "webapp", "cmd", "internal", "pkg"}
    assert layout_segments_union() == frozenset(legacy)
    assert _SRC_LAYOUT_SEGMENTS == frozenset(legacy), \
        "contract_utils 消费侧必须接派生视图，绝不留第二份手抄"


def test_every_stack_declares_layout_segments():
    """6 栈全声明 layout_segments（空=该栈文件永不参与切模块根=静默失明方向，
    与 unregistered_aggregate_stacks 同纪律：缺席机读可辨）。"""
    assert all(s.layout_segments for s in STACK_SPEC.values()), \
        {k for k, s in STACK_SPEC.items() if not s.layout_segments}


def test_workspace_container_segments_npm_only():
    """容器段只有 npm 声明（packages/apps=pnpm/turborepo 约定）；其余栈空=不适用，
    绝不给 JVM 栈发明容器概念。"""
    assert workspace_container_segments_union() == frozenset({"packages", "apps"})
    assert _WORKSPACE_CONTAINER_SEGMENTS == frozenset({"packages", "apps"})
    for key, s in STACK_SPEC.items():
        if key == "npm":
            assert s.workspace_container_segments == ("packages", "apps")
        else:
            assert s.workspace_container_segments == (), key


def test_module_root_workspace_package_without_layout_segment():
    """★主治锁★：workspace 包源码不在布局段内（包根直挂/lib 目录）也能切出真模块根。"""
    assert _code_module_root("packages/api/index.ts") == "packages/api"
    assert _code_module_root("packages/api/lib/util.ts") == "packages/api"
    assert _code_module_root("apps/web/components/Button.tsx") == "apps/web"


def test_module_root_workspace_package_with_layout_segment_unchanged():
    """兼容锁：布局段内的 workspace 文件答案与治前逐字节一致（两规则同答不冲突）。"""
    assert _code_module_root("packages/api/src/index.ts") == "packages/api"
    assert _code_module_root("ruoyi-alarm/alarm-core/src/main/java/X.java") == \
        "ruoyi-alarm/alarm-core"


def test_module_root_container_without_child_is_none():
    """fail-closed：容器直接含文件（无子目录）≠ workspace 包 → None，绝不造根。"""
    assert _code_module_root("packages/index.ts") is None


def test_module_root_deep_packages_segment_not_container():
    """★position-0 反误杀锁★：深处的 packages 是 Java 包名，绝不触发容器规则。"""
    assert _code_module_root("mod/src/main/java/com/x/packages/Foo.java") == "mod"


def test_evidence_class_workspace_package_is_strong():
    """workspace 包内代码=强证据（主张真模块根 packages/api，不再塌成首段假模块）。"""
    assert _evidence_class("packages/api/index.ts") == _EV_STRONG
    assert _evidence_class("packages/api/src/index.ts") == _EV_STRONG
    # 非容器首段的 flat 布局仍是 WEAK（行为不变）
    assert _evidence_class("web/index.js") == _EV_WEAK_CODE


def test_common_prefix_ending_at_container_is_none():
    """公共前缀恰止于容器=不造 "packages" 假模块根（fail-closed 交上层如实处理）。"""
    assert _common_module_prefix(
        ["packages/api/src/a.ts", "packages/web/src/b.ts"], None) is None
    # 同包内公共前缀照常（容器+子目录=合法模块根）
    assert _common_module_prefix(
        ["packages/api/src/a.ts", "packages/api/src/b.ts"], None) == "packages/api"
    # Maven 回归
    assert _common_module_prefix(
        ["mod/a/src/main/java/X.java", "mod/a/src/main/resources/y.xml"], None) == "mod/a"


def test_container_rule_excludes_artifact_dirs_and_declaration_suffix():
    """★hunter F3 锁★：容器档绝不提升产物/依赖目录（node_modules/dist/…）与纯声明
    后缀（.d.ts）——它们从不主张模块根。判据=STACK_SPEC source_exclude_dirs/suffixes
    并集；两处消费（_evidence_class/_code_module_root）同源同规则，绝不许分叉。"""
    assert _evidence_class("packages/api/node_modules/foo/index.ts") == _EV_WEAK_CODE
    assert _evidence_class("packages/api/lib/foo.d.ts") == _EV_WEAK_CODE
    assert _evidence_class("packages/api/dist/bundle.js") == _EV_WEAK_CODE
    assert _code_module_root("packages/api/node_modules/foo/index.ts") is None
    assert _code_module_root("packages/api/lib/foo.d.ts") is None
    # 正例不受影响（合法 workspace 包源码照常 STRONG/切根）
    assert _evidence_class("packages/api/lib/util.ts") == _EV_STRONG
    assert _code_module_root("packages/api/lib/util.ts") == "packages/api"
