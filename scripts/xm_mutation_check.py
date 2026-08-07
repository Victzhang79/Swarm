#!/usr/bin/env python3
"""X-M 簇突变 harness（判据与前九批同源，那些自伤一开始就带上）：

  · **先验基线全绿**（只验"突变→红"会让修得不全的整改全绿通过：B-4b I-1 实证）；
  · **逐条**跑 should_red，每条都必须红；· `rc=5`（`-k` 选不到）**判失败**；
  · 落点唯一性检查；· 突变后源码必须仍能 `ast.parse`（否则 rc≠0 只是 collection error）。

★锁的命题★ X-M 批一（27 号文 §3.2 X-M2/M5/M7）：多栈覆盖的「快赢」三条——
.kt 包声明对账 / 混编逐栈自测 / 未知工具链降级可观测。
X-M 批二（X-M8）：.vue 进类型闸触发集 + vue-tsc 优选 + 缺 vue-tsc 降级 WARNING。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_image_builder.py",
         "test/test_e_theme_supply_gates_batch1.py",
         "test/test_xm8_vue_type_gate.py",
         "test/test_xc3_error_drivers.py",
         "test/test_r56_dep_legality.py"]

IMG = ROOT / "worker" / "image_builder.py"
PIPE = ROOT / "worker" / "l1_pipeline.py"
DL = ROOT / "worker" / "dep_legality.py"

MUTATIONS = [
    (
        "X-M2：.kt 规则从分派表删除（Kotlin 包声明对账回退到「抽不到声明 ⇒ 静默跳过」——"
        "与没接上同效：表驱动机制加了栈但没进表＝接线覆盖 ≠ 机制存在）",
        PIPE,
        '    ".kt": (re.compile(r"(?:^|/)(?:src/main/kotlin|src/test/kotlin|src/main/java|src/test/java)"\n'
        '                       r"/(?P<rel>.+\\.kt)$"),\n'
        '            re.compile(r"^\\+\\s*package\\s+([A-Za-z_][\\w.]*?)\\s*;?\\s*(?://.*)?$")),\n',
        '',
        ['test_e6_kotlin_package_decl_mismatch_caught'],
    ),
    (
        "X-M5：混编自测退回首个命中即 return（java+npm 只自测 maven，npm 侧坏掉等运行时炸）",
        IMG,
        '    return " && ".join(cmds) if cmds else None',
        '    return cmds[0] if cmds else None',
        # 配对守卫也锁得住：混编 selftest 输出变了 ⇒ 摘要变 ⇒ (10, 摘要) 配对破。
        ['test_selftest_covers_every_toolchain_in_a_mixed_spec',
         'test_builder_version_bumped_so_old_images_are_invalidated'],
    ),
    (
        "X-M7：未知工具链降级 WARNING 降成 DEBUG（回退到「只在 Dockerfile 里留一行注释」——"
        "降级不可观测，血规 3）",
        IMG,
        '    logger.warning("[IMAGE-BUILD] X-M7 未知工具链 %r（build_tool=%r）：镜像不装它的构建"',
        '    logger.debug("[IMAGE-BUILD] X-M7 未知工具链 %r（build_tool=%r）：镜像不装它的构建"',
        ['test_unknown_toolchain_emits_warning'],
    ),
    (
        "X-M8a：触发集删掉 .vue（.vue 改动回退到「js_ts 为空 → 类型闸整段跳过」＝零覆盖）",
        PIPE,
        'js_ts = [f for f in files if f.endswith((".ts", ".tsx", ".js", ".jsx", ".vue"))]',
        'js_ts = [f for f in files if f.endswith((".ts", ".tsx", ".js", ".jsx"))]',
        ['test_vue_change_is_type_checked_by_vue_tsc'],
    ),
    (
        "X-M8b：vue-tsc 优选被摘（.vue 直接喂 tsc —— tsc 解析不了 SFC，要么假红要么靠"
        " infra 豁免假绿，两种都是错答案）",
        PIPE,
        'if any(f.endswith(".vue") for f in js_ts):',
        'if False:',
        ['test_vue_change_is_type_checked_by_vue_tsc'],
    ),
    (
        "X-M8c：缺 vue-tsc 的降级 WARNING 降成 DEBUG（.vue 无类型覆盖这一降级不可观测，血规 3）",
        PIPE,
        '''logger.warning(
                        "[L1.2] X-M8 项目缺 vue-tsc''',
        '''logger.debug(
                        "[L1.2] X-M8 项目缺 vue-tsc''',
        ['test_missing_vue_tsc_falls_back_to_tsc_with_warning'],
    ),
    (
        "X-M4a：warmup 分派表删 rust 条目（cargo registry 永不预热 ⇒ --offline 自测回到"
        "恒 degraded 死信；摘要守卫同红——生成物变了）",
        IMG,
        '    "rust": "cargo fetch",\n',
        '',
        ['test_xm4_warmup_covers_go_rust_python',
         'test_builder_version_bumped_so_old_images_are_invalidated'],
    ),
    (
        "X-M4b：python 的 requirements.txt 形态闸被摘（pyproject.toml 也臆造 pip install -r"
        " ⇒ 血规 2 破；且 pyproject 工程连 requirements.txt 都没有 ⇒ warmup 必假败）",
        IMG,
        '    if name == "python":\n'
        '        dep = (tc.dep_source or "").replace("\\\\", "/").rsplit("/", 1)[-1]\n'
        '        if dep != "requirements.txt":\n'
        '            return None\n',
        '    if name == "python":\n'
        '        pass\n',
        ['test_xm4_python_warmup_only_for_requirements_txt',
         'test_builder_version_bumped_so_old_images_are_invalidated'],
    ),
    (
        "X-M4c：go/rust/python warmup 的 S-5 子目录安全闸被摘（dep_source 来自被扫描仓库"
        " ⇒ `..`/注入目录名以 root 拼进 RUN，与 npm 段 S-5 同型）",
        IMG,
        '            if sub and not _SAFE_SUBDIR_RE.match(sub):\n'
        '                logger.warning(\n'
        '                    "跳过 %s warmup：dep_source 子目录名不安全（可能是注入载荷或路径逃逸）: %r",\n',
        '            if sub and _SAFE_SUBDIR_RE.match(sub):\n'
        '                logger.warning(\n'
        '                    "跳过 %s warmup：dep_source 子目录名不安全（可能是注入载荷或路径逃逸）: %r",\n',
        ['test_xm4_warmup_unsafe_subdir_skipped_with_warning'],
    ),
    (
        "X-M4d：npm warmup 退回管道形态（`npm ci | tail || npm install` ⇒ 判的是 tail 的"
        "退出码，install 兜底臂回死代码，H-1 同型复发）",
        IMG,
        'f"RUN cd {_wd_q} && (npm ci > {shlex.quote(_npm_log)} 2>&1 || "',
        'f"RUN cd {_wd_q} && (npm ci 2>&1 | tail -5 || "',
        ['test_xm4_npm_warmup_fallback_arm_is_live',
         'test_builder_version_bumped_so_old_images_are_invalidated'],
    ),
    (
        "X-M4e：_BUILDER_VERSION 11→10（改了镜像生成物不递增版本 ⇒ 复用老镜像，修复一行"
        "到不了生产——X-C2/P-C3/X-M5 同形状第四次，配对守卫必须响）",
        IMG,
        '_BUILDER_VERSION = "11"',
        '_BUILDER_VERSION = "10"',
        ['test_builder_version_bumped_so_old_images_are_invalidated'],
    ),
    (
        "X-M3a：`_build_error_modules` 的 pom 解析错抽取被摘（JVM 模块归属回退道残废——"
        "X-M3 定案锁的是「JVM 方向照常工作 + 非 JVM 不臆造」两个方向，本条压第一方向）",
        PIPE,
        '    mods = {m.group(1) for m in _POM_ERR_MODULE_RE.finditer(build_output or "")}',
        '    mods = set()',
        ['test_xm3_module_extraction_is_jvm_only_by_design'],
    ),
    (
        "X-M3b：`_ERR_FILE_RE` 摘 .go（非 JVM 归属主力=文件级通道；摘了它 go 构建错"
        "跌回模块回退道=恒空 ⇒ 连坐假 FAIL——X-M3 定案「恒空可接受」的前提塌了）",
        PIPE,
        '(?:java|kt|scala|go|rs|ts|tsx|js|vue|xml|py)',
        '(?:java|kt|scala|rs|ts|tsx|js|vue|xml|py)',
        ['test_xm3_file_level_attribution_covers_non_jvm_stacks'],
    ),
    (
        "X-M10a：NpmDriver 从 DRIVERS 摘除（npm 工程依赖合法性回退零覆盖；"
        "接线锁必须红——driver_for('npm') → None → 臂直接返回）",
        DL,
        # ★落点更新（#29-2 复跑时发现已死）★：DRIVERS 表从两项长到六项、写法改成多行，
        # 原来那条单行字面量落点**在本仓已不存在** ⇒ 这条锁自那次扩表起一直报"落点未命中"
        # ＝零覆盖。改成只摘 npm 那一行（表继续长也不会再漂）。
        '    "npm": NpmDriver(),\n',
        '',
        ['test_xm10_gate_dispatches_to_npm_by_manifest_presence'],
    ),
    (
        "X-M10b：self_hosted_prefixes 摘 file:（本地协议依赖被送探针 ⇒ registry 必 E404"
        " ⇒ 误剪合法 file: 依赖——本批最重要的分档）",
        DL,
        '''self_hosted_prefixes = ("${", "$", "file:", "link:", "git+", "git:",''',
        '''self_hosted_prefixes = ("${", "$", "link:", "git+", "git:",''',
        ['test_xm10_npm_protocol_versions_never_probed'],
    ),
    (
        "X-M10c：npm 臂 namespace=None 回传 @scope（分档①被违反 ⇒ 规则②「非成员即剪」"
        "误剪合法已发布 scoped 包——Maven 消费契约硬套 npm）",
        PIPE,
        "        texts, root_text=root_text, namespace=None,   # 分档①：@scope ≠ 工程命名空间",
        '        texts, root_text=root_text, namespace="@acme",',
        ['test_xm10_gate_keeps_published_scoped_package'],
    ),
    (
        "X-M10d：探针先判 _tool_missing 后判 E404（A1 同型复发：404 响应体的 "
        "'Not Found' 把「确证查无」吞成「工具缺失」⇒ 确证剪除被旁路）",
        PIPE,
        '''    if "E404" in body or "404 Not Found" in body:
        return [], True    # registry 确证答复"没有它"——可据以剪除的肯定证据
    if _tool_missing(body):
        return [], False''',
        '''    if _tool_missing(body):
        return [], False
    if "E404" in body or "404 Not Found" in body:
        return [], True    # registry 确证答复"没有它"——可据以剪除的肯定证据''',
        ['test_xm10_npm_probe_contract'],
    ),
    (
        "X-M10e：probe_without_namespace 门回退成 `if ns`（无 scope 包永不送探针 ⇒ "
        "无 scope 幻影包零判定——npm 的名字即完整坐标这一分档被抹）",
        DL,
        "\n    vers = (registry_versions(ns, name)\n                   if (ns or probe_without_namespace) else None)",
        "\n    vers = registry_versions(ns, name) if ns else None",
        ['test_xm10_gate_dispatches_to_npm_by_manifest_presence',
         'test_xm10_npm_enforce_prunes_phantom_keeps_legit'],
    ),
]


def _pytest(args: list[str]) -> int:
    p = subprocess.run([PY, "-m", "pytest", *TESTS, "-p", "no:warnings", "-q",
                        "--tb=no", *args], cwd=ROOT, capture_output=True, text=True)
    return p.returncode



def _clear_pyc(path: Path) -> None:
    """删被突变模块的 pyc（T-2，#29-3 统一补齐）。

    CPython 判 pyc 是否有效看的是源码 **mtime（整秒粒度）+ 字节数**。故当「等长突变
    （len(old)==len(new)）」与「同秒写入」同时成立时，第二条突变写完，pyc 仍被判有效
    ⇒ 子进程加载的是【上一条】的字节码。双向危害：既造"突变后仍绿"（冤报测试没牙），
    也造"红的是上一条"（假背书——这条锁其实没被验证）。
    每条突变前与还原后都必须清。
    """
    cache = path.parent / "__pycache__"
    if cache.is_dir():
        for f in cache.glob(path.stem + ".*.pyc"):
            try:
                f.unlink()
            except OSError:
                pass

def main() -> int:
    print("═" * 70)
    print("步骤 0：基线必须全绿")
    print("═" * 70)
    rc = _pytest([])
    if rc != 0:
        print(f"✗ 基线是红的 (exit={rc}) —— 突变结果全部无意义。先修基线。")
        return 1
    print(f"✓ 基线全绿 (exit={rc})\n")

    failures = []
    for i, (name, path, old, new, should_red) in enumerate(MUTATIONS, 1):
        src = path.read_text()
        if old not in src:
            print(f"[{i}/{len(MUTATIONS)}] {name}\n    ✗ 落点未命中（代码已漂移）")
            failures.append((name, "落点未命中"))
            continue
        if src.count(old) != 1:
            print(f"[{i}/{len(MUTATIONS)}] {name}\n"
                  f"    ✗ 落点出现 {src.count(old)} 次（非唯一，突变不等价）")
            failures.append((name, "落点非唯一"))
            continue
        path.write_text(src.replace(old, new, 1))
        _clear_pyc(path)
        try:
            if path.suffix == ".py" and ast.parse(path.read_text()) is None:
                print(f"[{i}/{len(MUTATIONS)}] {name}\n    ✗ 突变后 ast.parse 失败")
                failures.append((name, "突变后不可解析"))
                continue
            per = [(n, _pytest(["-k", n])) for n in should_red]
            missing = [n for n, r in per if r == 5]
            green = [n for n, r in per if r == 0]
            print(f"[{i}/{len(MUTATIONS)}] {name}")
            if not missing and not green:
                print(f"    ✓ 指名的 {len(per)} 条全红")
            else:
                if missing:
                    print(f"    ✗ 测试名选不到（rc=5，重命名/typo）: {missing}")
                    failures.append((name, "测试名选不到"))
                if green:
                    print(f"    ✗ 突变后仍绿 = 零区分力: {green}")
                    failures.append((name, "突变后仍绿"))
        finally:
            path.write_text(src)
            _clear_pyc(path)

    print("\n" + "═" * 70)
    rc_r = _pytest([])
    print(f"步骤 N：还原后基线复验 exit={rc_r}")
    if rc_r != 0:
        print("✗ 还原后基线不绿 —— harness 污染了工作树")
        return 1
    if failures:
        print(f"\n✗ {len(failures)} 条未达标：")
        for n, why in failures:
            print(f"  · [{why}] {n}")
        return 1
    print(f"\n✓ 全部 {len(MUTATIONS)} 条突变都被锁住，且基线前后皆绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
