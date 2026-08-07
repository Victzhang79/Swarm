#!/usr/bin/env python3
"""P-H3 突变 harness（判据与前批同源）：

  · **先验基线全绿**（只验"突变→红"会让修得不全的整改全绿通过）；
  · **逐条**跑 should_red，每条都必须红；· `rc=5`（`-k` 选不到）**判失败**；
  · 落点唯一性检查；· 突变后源码必须仍能 `ast.parse`。

★锁的命题★ P-H3（27 号文）：npm/go 裸名解析补上「工程自身清单」零网络证据层——
npm: node_modules → package.json 声明 → registry；go: module cache → go.mod 钉版 → proxy。
十一条突变分别压：层删除 ×2 / LOOKUP 门控（消费契约）×2 / 判定序 ×2 / 根优先 ×2 /
node_modules 跳过谓词 / 降级可观测（硬检查④）/ 调用方接线（血规 10 第一条）。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_npm_registry_p2b.py",
         "test/test_go_registry_p2c.py",
         "test/test_pc2_review_hunter_findings.py"]

NPMR = ROOT / "brain" / "npm_registry.py"
GOR = ROOT / "brain" / "go_registry.py"
CU = ROOT / "brain" / "contract_utils.py"
# ★#29-3 T-1★ go 脚手架叶簇已从 contract_utils.py 拆到 brain/go_scaffold.py（纪律#9），
# contract_utils 只留顶层 re-export。落点必须跟着**定义模块**走——打 re-export 那个地址
# 的突变恒未命中＝静默零覆盖（本仓已登记「拆函数迁模块后落点随簇漂移」这一类）。
GS = ROOT / "brain" / "go_scaffold.py"

_GATE_BLOCK = ("    if not _lookup_enabled():\n"
               "        return {}\n"
               "    out: dict[str, str] = {}")

MUTATIONS = [
    (
        "P-H3a：npm 清单证据层被摘（裸名回退「无 node_modules 即问 registry」——新 clone "
        "沙箱 + registry 抖动 ⇒ 契约依赖照旧被如实丢弃，P-H3 等于没治）",
        NPMR,
        "        _decl = (_manifest_specs or {}).get(name)",
        "        _decl = None",
        ["test_ph3_bare_name_resolves_from_project_manifest"],
    ),
    (
        "P-H3b：npm 清单层的 LOOKUP 门控被摘（离线模式静默破约——开关文档口径「关闭后="
        "解析不到→如实丢弃」，复用单一事实源≠复用消费契约）",
        NPMR,
        _GATE_BLOCK,
        "    out: dict[str, str] = {}",
        ["test_ph3_manifest_layer_respects_lookup_switch"],
    ),
    (
        "P-H3c：npm 判定序被翻——node_modules 层失效后清单层直接答（「确定能装的已装版本」"
        "被换成「声明区间」，证据强度降档）",
        NPMR,
        "        if _local:",
        "        if False:",
        ["test_ph3_node_modules_beats_manifest"],
    ),
    (
        "P-H3d：清单解析失败的 WARNING 降成 DEBUG（「清单坏了」与「真没有声明」退回不可分，"
        "硬检查④）",
        NPMR,
        '            logger.warning("[npm-registry] P-H3 工程清单解析失败，其依赖声明不参与取证: %s", pj)',
        '            logger.debug("[npm-registry] P-H3 工程清单解析失败，其依赖声明不参与取证: %s", pj)',
        ["test_ph3_manifest_specs_malformed_json_warns_and_skips"],
    ),
    (
        "P-H3e：node_modules 跳过谓词被摘（安装产物清单混进「工程声明」取证面——"
        "node_modules/package.json 的依赖会冒充工程声明）",
        NPMR,
        '                  if d.name != "node_modules" and (d / "package.json").is_file()]',
        "                  if True]",
        ["test_ph3_manifest_specs_root_and_one_level"],
    ),
    (
        "P-H3f：npm 根优先被翻成后写覆盖（子模块同名声明覆盖根——「就近覆盖」语义反转）",
        NPMR,
        "                    out.setdefault(name, spec.strip())",
        "                    out[name] = spec.strip()",
        ["test_ph3_manifest_specs_root_and_one_level"],
    ),
    (
        "P-H3g：go 的 go.mod 钉版层被摘（裸 module 回退「cache 空即问 proxy」——新 clone "
        "沙箱 + proxy 抖动 ⇒ 照旧如实丢弃，P-H3 等于没治）",
        GOR,
        "        _pinned = (_go_mod_pins or {}).get(mod)",
        "        _pinned = None",
        ["test_ph3_bare_module_resolves_from_go_mod_pin",
         "test_ph3_go_mod_pin_reaches_scaffold_via_real_caller"],
    ),
    (
        "P-H3h：go 钉版层的 LOOKUP 门控被摘（离线模式静默破约，同 P-H3b）",
        GOR,
        _GATE_BLOCK,
        "    out: dict[str, str] = {}",
        ["test_ph3_go_mod_layer_respects_lookup_switch"],
    ),
    (
        "P-H3i：go 判定序被翻——本地 cache 层失效后钉版层直接答（「确定能拉的已下载版本」"
        "被换成「go.mod 声明钉版」）",
        GOR,
        "        if _cached:",
        "        if False:",
        ["test_ph3_local_cache_beats_go_mod_pin"],
    ),
    (
        "P-H3j：go 根优先被翻成后写覆盖（子目录同名钉版覆盖根）",
        GOR,
        "                out.setdefault(m.group(1), m.group(2))",
        "                out[m.group(1)] = m.group(2)",
        ["test_ph3_go_mod_requires_parses_both_require_forms"],
    ),
    (
        # ★#29-3 T-1：落点死于**模块迁移**，代码逐字未变★ 该调用点随 go 脚手架叶簇迁到
        # `brain/go_scaffold.py`，字符串一个字节没改、地址变了 ⇒ 打 CU 恒未命中＝零覆盖。
        # 只换路径（CU → GS）。讽刺的是这条锁本身锁的就是"接线覆盖≠机制存在"，而它自己
        # 正因接线地址漂移而失效——同一条纪律在 harness 层复发。
        "P-H3k：调用方不传 project_path（证据层加在 resolve_go_deps 里而唯一生产调用点"
        "不带它 ⇒ 零调用点死代码——接线覆盖≠机制存在，血规 10 第一条）",
        GS,
        "        kept, internal_mods, dropped = resolve_go_deps(\n"
        "            _norm_arts, internal_modules=internal_ids, project_path=project_path)",
        "        kept, internal_mods, dropped = resolve_go_deps(\n"
        "            _norm_arts, internal_modules=internal_ids, project_path=None)",
        ["test_ph3_go_mod_pin_reaches_scaffold_via_real_caller"],
    ),
    (
        "P-H3l：go 解析退回「非 require 上下文的裸行也收」（复核 R1-1 的病根：exclude 块/"
        "retract 行的 `mod vX.Y.Z` 与 require 行逐字相同，默认拒收才是治）",
        GOR,
        "            else:\n"
        "                # module/go/toolchain/单行 exclude/retract/replace（后者尾部 `=>` 或\n"
        "                # 无前缀版本本就过不了 _REQ_LINE，这里一并挡）——非 require 上下文绝不收\n"
        "                continue",
        "            else:\n"
        "                candidate = s",
        ["test_ph3_go_mod_requires_parses_both_require_forms"],
    ),
    (
        "P-H3m：npm 子目录枚举失败的 WARNING 降成 DEBUG（「目录坏了」与「真没有子包」退回"
        "不可分，硬检查④——注意 Path.glob 会静默吞 OSError，iterdir 才抛）",
        NPMR,
        '        logger.warning("[npm-registry] P-H3 子目录 package.json 枚举失败（目录不可读），"',
        '        logger.debug("[npm-registry] P-H3 子目录 package.json 枚举失败（目录不可读），"',
        ["test_ph3_manifest_enum_oserror_warns_and_degrades"],
    ),
    (
        "P-H3n：go.mod 读取失败的 WARNING 降成 DEBUG（「文件坏了」与「真没有 require」退回"
        "不可分，硬检查④）",
        GOR,
        '            logger.warning("[go-registry] P-H3 go.mod 读取失败，其 require 钉版不参与取证: %s", gm)',
        '            logger.debug("[go-registry] P-H3 go.mod 读取失败，其 require 钉版不参与取证: %s", gm)',
        ["test_ph3_go_mod_read_oserror_warns_and_skips"],
    ),
    (
        "P-H3o：单行 require 的空白容忍退回只认单空格（复核 R2-1：tab/多空格分隔的合法 "
        "go.mod 钉版整层漏收——R0 前缀剥除法本来认 `\\s+`，这是整改引入的回归形状）",
        GOR,
        '            elif s[:7] == "require" and len(s) > 7 and s[7].isspace():',
        '            elif s[:7] == "require" and len(s) > 7 and s[7] == " ":',
        ["test_ph3_go_mod_requires_parses_both_require_forms"],
    ),
    (
        "P-H3p：块结束退回只认裸 `)`（复核 R2-2：`) // 注释` 不退出 in_require ⇒ 后续 "
        "exclude/retract 块被当 require 收，坏版本冒充钉版）",
        GOR,
        '    _BLOCK_CLOSE = re.compile(r"^\\)\\s*(?://.*)?$")   # R2-2：`) // 注释` 也是合法块结束',
        '    _BLOCK_CLOSE = re.compile(r"^\\)$")',
        ["test_ph3_go_mod_requires_parses_both_require_forms"],
    ),
]


def _pytest(args: list[str]) -> int:
    p = subprocess.run([PY, "-m", "pytest", *TESTS, "-p", "no:warnings", "-q",
                        "--tb=no", *args], cwd=ROOT, capture_output=True, text=True)
    return p.returncode


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
