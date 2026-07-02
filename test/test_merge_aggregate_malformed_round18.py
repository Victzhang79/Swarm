"""round18 P0-A 复现 + 治本：聚合清单(根 pom)双写者【各自整段结构重写】的 3-way 合并
不得伪造重复的结构单例行（</modules>/</dependencyManagement>/<packaging>），否则产出畸形
pom → git apply「补丁未应用」→ 整包 apply_ok=False 连坐回滚 40+ 文件（MERGE#2 现场）。

铁证：logs_archive/process/merged_diff_996db614_1782973787.diff 第 57–98 行——root pom
hunk 里 </dependencyManagement>/</modules>/<packaging>pom 各出现 2–3 次（背靠背拼接两份
结构重写）。治本：合并伪造重复单例 → 拒绝自动消解 → 落 rebase（保留上游写者干净版）。
"""
from swarm.brain.merge_engine import (
    _aggregate_merge_duplicated_singleton,
    _is_aggregate_manifest,
    merge_diffs,
)

# 干净 base 根 pom 片段（结构单例：一个 </modules> / </dependencyManagement> / <packaging>）。
_BASE = """<project>
    <modules>
        <module>ruoyi-common</module>
    </modules>
    <packaging>pom</packaging>
    <dependencyManagement>
        <dependencies>
            <dependency>
                <artifactId>ruoyi-common</artifactId>
            </dependency>
        </dependencies>
    </dependencyManagement>
</project>
"""

# 分支 A（st-1）：干净地加 ruoyi-alarm（单例仍各 1 次）。
_VER_A = _BASE.replace(
    "        <module>ruoyi-common</module>\n",
    "        <module>ruoyi-common</module>\n        <module>ruoyi-alarm</module>\n",
)
# 分支 B（st-30）：干净地加 ruoyi-alarm-sdk（单例仍各 1 次）。
_VER_B = _BASE.replace(
    "        <module>ruoyi-common</module>\n",
    "        <module>ruoyi-common</module>\n        <module>ruoyi-alarm-sdk</module>\n",
)

# 畸形合并（复刻 dump）：两份结构重写背靠背拼接 → 单例行重复。
_MERGED_GARBAGE = """<project>
    <modules>
        <module>ruoyi-common</module>
        <module>ruoyi-alarm</module>
    </modules>
    <packaging>pom</packaging>
        <module>ruoyi-common</module>
        <module>ruoyi-alarm-sdk</module>
    </modules>
    <packaging>pom</packaging>
    <dependencyManagement>
        <dependencies>
            <dependency>
                <artifactId>ruoyi-common</artifactId>
            </dependency>
        </dependencies>
    </dependencyManagement>
</project>
"""

# 合法并集：两个新 module 并存，结构单例各 1 次。
_MERGED_CLEAN = """<project>
    <modules>
        <module>ruoyi-common</module>
        <module>ruoyi-alarm</module>
        <module>ruoyi-alarm-sdk</module>
    </modules>
    <packaging>pom</packaging>
    <dependencyManagement>
        <dependencies>
            <dependency>
                <artifactId>ruoyi-common</artifactId>
            </dependency>
        </dependencies>
    </dependencyManagement>
</project>
"""


def test_detects_duplicated_singleton_garbage():
    """畸形合并（复刻 dump）：重复的结构单例被检出。"""
    dup = _aggregate_merge_duplicated_singleton(_BASE, [_VER_A, _VER_B], _MERGED_GARBAGE)
    assert dup is not None
    # 被检出的是一个真·结构单例（base 与各分支都 ≤1 次，畸形合并里 >1 次）
    assert dup in (
        "</modules>", "<packaging>pom</packaging>", "<module>ruoyi-common</module>",
    ), dup


def test_clean_union_not_flagged():
    """合法并集：单例各 1 次 → 不误伤（per-entry union 照常）。"""
    assert _aggregate_merge_duplicated_singleton(_BASE, [_VER_A, _VER_B], _MERGED_CLEAN) is None


def test_repeatable_lines_not_flagged():
    """可重复行（</dependency>/</module>/空行）合法多次出现，不算单例，不误伤。"""
    # merged 里 </dependency> 出现多次是合法（多依赖），base/分支里它就不止 1 次时不入单例集。
    base = "<a>\n<dependency>x</dependency>\n<dependency>y</dependency>\n</a>\n"
    merged = "<a>\n<dependency>x</dependency>\n<dependency>y</dependency>\n<dependency>z</dependency>\n</a>\n"
    assert _aggregate_merge_duplicated_singleton(base, [merged], merged) is None


def test_is_aggregate_manifest_root_pom():
    assert _is_aggregate_manifest("pom.xml")
    assert _is_aggregate_manifest("ruoyi-alarm/pom.xml")
    assert _is_aggregate_manifest("settings.gradle")
    assert not _is_aggregate_manifest("src/main/java/Foo.java")


def _mk_diff(path: str, base: str, new: str, sid: str) -> str:
    """构造一份把 base→new 的整段结构重写 unified diff（覆盖式：删整块、加整块）。"""
    import difflib
    b = base.splitlines(keepends=True)
    n = new.splitlines(keepends=True)
    return "".join(difflib.unified_diff(b, n, fromfile=f"a/{path}", tofile=f"b/{path}"))


def test_merge_diffs_never_emits_duplicated_singleton():
    """端到端：两份对根 pom 同区段【重叠结构重写】的 diff 合并后，绝不含重复的结构单例；
    要么干净并集、要么 rebase（保留上游、下游待重生成）——都不产畸形。"""
    # 让两分支各自【重写整个 <modules> 块】(同锚点重叠 hunk) → 触发 _try_three_way_resolve。
    ver_a = _BASE.replace(
        "        <module>ruoyi-common</module>\n",
        "        <module>ruoyi-common</module>\n        <module>ruoyi-alarm</module>\n",
    )
    ver_b = _BASE.replace(
        "    </modules>\n",
        "        <module>ruoyi-alarm-sdk</module>\n    </modules>\n",
    )
    diff_a = _mk_diff("pom.xml", _BASE, ver_a, "st-1")
    diff_b = _mk_diff("pom.xml", _BASE, ver_b, "st-30")

    def base_reader(p: str):
        return _BASE if p == "pom.xml" else None

    res = merge_diffs(
        [("st-1", diff_a), ("st-30", diff_b)],
        base_reader=base_reader,
        subtask_order=["st-1", "st-30"],
    )
    merged = res.merged_diff
    # 关键判据：合并 patch 里绝不出现重复的结构单例（畸形 pom 的指纹）。
    added = [ln[1:] for ln in merged.splitlines() if ln.startswith("+")]
    assert added.count("    </modules>") <= 1, f"重复 </modules>:\n{merged}"
    assert added.count("    <packaging>pom</packaging>") <= 1, f"重复 <packaging>:\n{merged}"
    assert "<<<<<<<" not in merged and ">>>>>>>" not in merged, "不应残留冲突标记"
