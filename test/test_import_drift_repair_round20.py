#!/usr/bin/env python3
"""#9 跨 feature 包布局漂移 → 确定性 import 重写（round20 治本 Candidate B）回归测试。

治本背景（round19 实测头号交付天花板）：脚手架/生产者把类落在【扁平】包
`com.ruoyi.alarm.domain`，消费者却 import 了独立猜的【嵌套】包
`com.ruoyi.alarm.robot.domain` → javac `package P does not exist`。旧路径把它当
"内部包未就绪"(internal_pkg_not_built) BLOCKED，等一个【永不到来的生产者】(#10 幽灵
生产者)，慢磨整条 transient 阶梯才 abandon（st-38 2h35m）。

Candidate B：在判 BLOCKED 前，据【被引类在项目树里的真实内部包】确定性重写
`import P.C;` → `import R.C;`（唯一解才改；零解=真未就绪/臆造，歧义=多解，都 fail-closed
交回 BLOCKED/快失败）。与 symbol-repair 同源：真理取自项目实际产出，无硬编码、跨 feature
通用、非项目写死。

本套用【真实临时 Java 树 + 本地 grep/perl】（无沙箱时 _run_* 回退本地 subprocess），
端到端验证：① 漂移唯一解重写；② 类真不存在→不动（交 BLOCKED）；③ 歧义多解→不动；
④ 第三方缺包→不动（交 dep-repair）；⑤ 内联 FQN 也重写；⑥ 幂等收敛；⑦ 纯规划器；
⑧ 与 _build_blocked_on_unbuilt_internal 的分流不打架（漂移可修则不再误报未就绪）。
"""
from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

import swarm.worker.l1_pipeline as l1  # noqa: E402


def _mk_class(root: Path, pkg: str, name: str, kind: str = "class", body: str = "{}") -> None:
    """在 root 下按 pkg 建一个 <name>.java（RuoYi 惯例：一文件一公开类，文件名=类名）。"""
    d = root / "src/main/java" / pkg.replace(".", "/")
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.java").write_text(f"package {pkg};\npublic {kind} {name} {body}\n")


def _mk_consumer(root: Path, pkg: str, name: str, imports: str, body: str = "{}") -> str:
    d = root / "src/main/java" / pkg.replace(".", "/")
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.java").write_text(
        f"package {pkg};\n{imports}\npublic class {name} {body}\n"
    )
    return f"src/main/java/{pkg.replace('.', '/')}/{name}.java"


# ── ① 漂移唯一解：消费者 import 嵌套错包，被引类真实在扁平内部包 → 确定性重写 ──

def test_drift_rewrite_unique_resolution():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        # 生产者：RobotSender 真实落在扁平 com.ruoyi.alarm.domain
        _mk_class(d, "com.ruoyi.alarm.domain", "RobotSender")
        # 消费者：AlarmTask 猜成嵌套 com.ruoyi.alarm.robot.domain（漂移）
        rel = _mk_consumer(
            d, "com.ruoyi.alarm.task", "AlarmTask",
            "import com.ruoyi.alarm.robot.domain.RobotSender;",
        )
        build_out = (
            f"[ERROR] {rel}:[2,35] package com.ruoyi.alarm.robot.domain does not exist\n"
        )
        n, files = l1._attempt_internal_import_drift_repair(str(d), build_out, timeout=30)
        assert n == 1, (n, files)
        assert files == [rel], files
        txt = (d / rel).read_text()
        assert "import com.ruoyi.alarm.domain.RobotSender;" in txt, txt
        assert "com.ruoyi.alarm.robot.domain" not in txt, txt
    print("  ✅ ① 漂移嵌套→扁平：import 唯一解确定性重写")


# ── ② 被引类项目里根本不存在 → 不动（真未就绪/臆造，交 BLOCKED/#10 快失败）──

def test_drift_absent_class_no_touch():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        _mk_class(d, "com.ruoyi.alarm.domain", "RobotSender")  # 存在别的类，锚定 own 前缀
        rel = _mk_consumer(
            d, "com.ruoyi.alarm.task", "AlarmTask",
            "import com.ruoyi.alarm.sender.dto.SenderDTO;",  # SenderDTO 全树不存在
        )
        build_out = (
            f"[ERROR] {rel}:[2,32] package com.ruoyi.alarm.sender.dto does not exist\n"
        )
        n, files = l1._attempt_internal_import_drift_repair(str(d), build_out, timeout=30)
        assert n == 0, (n, files)
        assert "com.ruoyi.alarm.sender.dto.SenderDTO" in (d / rel).read_text()
    print("  ✅ ② 类真不存在：不重写（交 BLOCKED/快失败）")


# ── ③ 被引类在【多个】内部包都存在 → 歧义 fail-closed 不动 ──

def test_drift_ambiguous_no_touch():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        _mk_class(d, "com.ruoyi.alarm.domain", "RobotSender")
        _mk_class(d, "com.ruoyi.alarm.core.domain", "RobotSender")  # 同名两处
        rel = _mk_consumer(
            d, "com.ruoyi.alarm.task", "AlarmTask",
            "import com.ruoyi.alarm.robot.domain.RobotSender;",
        )
        build_out = (
            f"[ERROR] {rel}:[2,35] package com.ruoyi.alarm.robot.domain does not exist\n"
        )
        n, files = l1._attempt_internal_import_drift_repair(str(d), build_out, timeout=30)
        assert n == 0, (n, files)
        assert "com.ruoyi.alarm.robot.domain.RobotSender" in (d / rel).read_text()
    print("  ✅ ③ 多解歧义：fail-closed 不赌，交 BLOCKED")


# ── ④ 第三方缺包（非自有前缀）→ 漂移修复不碰（交 dep-repair 防线④）──

def test_drift_thirdparty_skipped():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        _mk_class(d, "com.ruoyi.alarm.domain", "RobotSender")
        rel = _mk_consumer(
            d, "com.ruoyi.alarm.task", "AlarmTask",
            "import io.jsonwebtoken.Jwts;",
        )
        build_out = f"[ERROR] {rel}:[2,20] package io.jsonwebtoken does not exist\n"
        n, files = l1._attempt_internal_import_drift_repair(str(d), build_out, timeout=30)
        assert n == 0, (n, files)
        assert "import io.jsonwebtoken.Jwts;" in (d / rel).read_text()
    print("  ✅ ④ 第三方缺包：漂移修复不碰（交 dep-repair）")


# ── ⑤ 内联 FQN 使用（非 import 行）也一并重写，且不误伤更长同前缀类名 ──

def test_drift_inline_fqn_rewritten_no_overreach():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        _mk_class(d, "com.ruoyi.alarm.domain", "RobotSender")
        rel = _mk_consumer(
            d, "com.ruoyi.alarm.task", "AlarmTask",
            "import com.ruoyi.alarm.robot.domain.RobotSender;",
            body=(
                "{ void m() { com.ruoyi.alarm.robot.domain.RobotSender s = null; } }"
            ),
        )
        build_out = (
            f"[ERROR] {rel}:[2,35] package com.ruoyi.alarm.robot.domain does not exist\n"
        )
        n, _files = l1._attempt_internal_import_drift_repair(str(d), build_out, timeout=30)
        assert n == 1
        txt = (d / rel).read_text()
        # import + 内联 FQN 两处都改
        assert txt.count("com.ruoyi.alarm.domain.RobotSender") == 2, txt
        assert "com.ruoyi.alarm.robot.domain" not in txt, txt
    print("  ✅ ⑤ 内联 FQN 一并重写，边界安全")


# ── ⑥ 幂等收敛：重写后再跑（P 已消失）→ 零新增，不震荡 ──

def test_drift_idempotent():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        _mk_class(d, "com.ruoyi.alarm.domain", "RobotSender")
        rel = _mk_consumer(
            d, "com.ruoyi.alarm.task", "AlarmTask",
            "import com.ruoyi.alarm.robot.domain.RobotSender;",
        )
        build_out = (
            f"[ERROR] {rel}:[2,35] package com.ruoyi.alarm.robot.domain does not exist\n"
        )
        n1, _ = l1._attempt_internal_import_drift_repair(str(d), build_out, timeout=30)
        assert n1 == 1
        # 第二遍：文件已改对，同样的旧 build_out 里的 P 在文件里已不存在 → 零改动
        n2, _ = l1._attempt_internal_import_drift_repair(str(d), build_out, timeout=30)
        assert n2 == 0, "重写后应收敛，不得反复改"
    print("  ✅ ⑥ 幂等收敛：重写后零新增")


# ── ⑦ 纯规划器：唯一/零/多解判定 ──

def test_plan_pure_unique_zero_multi():
    plan = l1.plan_internal_import_drift_rewrites
    # 唯一解
    out = plan(
        {"F.java": [("com.x.robot.domain", "RobotSender")]},
        {"RobotSender": {"com.x.domain"}},
    )
    assert out == [("F.java", "com.x.robot.domain.RobotSender", "com.x.domain.RobotSender")]
    # 零解
    assert plan({"F.java": [("com.x.robot.domain", "Ghost")]}, {"Ghost": set()}) == []
    # 多解 fail-closed
    assert plan(
        {"F.java": [("com.x.robot.domain", "RobotSender")]},
        {"RobotSender": {"com.x.domain", "com.x.core.domain"}},
    ) == []
    # 真实包恰等于漂移包（不该出现，但防御）→ 候选剔除自身 → 零解
    assert plan(
        {"F.java": [("com.x.robot.domain", "RobotSender")]},
        {"RobotSender": {"com.x.robot.domain"}},
    ) == []
    print("  ✅ ⑦ 纯规划器：唯一→改 / 零→不改 / 多解→fail-closed")


# ── ⑧ 分流对偶：漂移可修的包，_build_blocked_on_unbuilt_internal 仍会把它当"未建出"标记，
#    但漂移修复【先跑】→ 重写后重跑 build 时该 P 已不复存在，不会再进 BLOCKED。这里验证
#    修复前该 P 确会被 BLOCKED 逻辑捕获（证明旧路径确实误 BLOCKED），修复后消解。 ──

def test_drift_dissolves_would_be_blocked():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        _mk_class(d, "com.ruoyi.alarm.domain", "RobotSender")
        rel = _mk_consumer(
            d, "com.ruoyi.alarm.task", "AlarmTask",
            "import com.ruoyi.alarm.robot.domain.RobotSender;",
        )
        build_out = (
            f"[ERROR] {rel}:[2,35] package com.ruoyi.alarm.robot.domain does not exist\n"
        )
        # 修复前：BLOCKED 逻辑会把这个漂移包误当"内部包未就绪"（旧误判，st-38 之源）
        blocked = l1._build_blocked_on_unbuilt_internal(str(d), build_out, timeout=30)
        assert "com.ruoyi.alarm.robot.domain" in blocked, "漂移包在修复前确会被误标 BLOCKED"
        # 漂移修复消解它
        n, _ = l1._attempt_internal_import_drift_repair(str(d), build_out, timeout=30)
        assert n == 1
    print("  ✅ ⑧ 漂移包旧路径确被误 BLOCKED；漂移修复先跑即消解")


# ── ⑨ 已接线进 _attempt_build_repair 的 Java 分支（端到端 dispatcher 也能触发）──

def test_wired_into_build_repair_dispatcher():
    with tempfile.TemporaryDirectory() as dd:
        d = Path(dd)
        _mk_class(d, "com.ruoyi.alarm.domain", "RobotSender")
        rel = _mk_consumer(
            d, "com.ruoyi.alarm.task", "AlarmTask",
            "import com.ruoyi.alarm.robot.domain.RobotSender;",
        )
        build_out = (
            f"[ERROR] {rel}:[2,35] package com.ruoyi.alarm.robot.domain does not exist\n"
        )
        # project_stack 显式声明 java → dispatcher 走 Java 分支
        n, files = l1._attempt_build_repair(
            str(d), build_out, [rel], timeout=30, project_stack={"backend": "java"}
        )
        assert rel in files, files
        assert "import com.ruoyi.alarm.domain.RobotSender;" in (d / rel).read_text()
    print("  ✅ ⑨ 已接线进 _attempt_build_repair Java 分支")


if __name__ == "__main__":
    test_drift_rewrite_unique_resolution()
    test_drift_absent_class_no_touch()
    test_drift_ambiguous_no_touch()
    test_drift_thirdparty_skipped()
    test_drift_inline_fqn_rewritten_no_overreach()
    test_drift_idempotent()
    test_plan_pure_unique_zero_multi()
    test_drift_dissolves_would_be_blocked()
    test_wired_into_build_repair_dispatcher()
    print("\n✅ 全部通过：#9 漂移 import 确定性重写（Candidate B）")
