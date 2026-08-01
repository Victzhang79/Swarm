#!/usr/bin/env python3
"""P-C3 突变 harness（判据与前八批同源，那些自伤一开始就带上）：

  · **先验基线全绿**（只验"突变→红"会让**修得不全**的整改全绿通过：B-4b I-1 实证）；
  · **逐条**跑 should_red，每条都必须红；· `rc=5`（`-k` 选不到，如测试被重命名）**判失败**；
  · 落点唯一性检查；· 突变后源码必须仍能 `ast.parse`（否则 rc≠0 只是 collection error）。

★锁的两组命题★
① **事实表扩栈**：五栈的服务端渲染必须认得出来，且认出来的**目的**（prompt 发"禁止产独立
   SPA"硬约束）必须真的达成——只锁 `frontend_kind` 会漏"分类对了但指令没发"。
② **fail-closed 方向双向**：未收录引擎必须触发"认不得"（机读键+needs_adj+WARNING），而真
   API 工程 / 真 SPA **必须不被误伤**。闸过宽的代价是每个纯 API 工程都去烧一次模型裁决、
   且收到一段误导指令——故误伤方向的突变与漏判方向一样多。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_pc3_template_engine_multistack.py",
         "test/test_stack_detect.py"]

SD = ROOT / "brain" / "stack_detect.py"
PN = ROOT / "brain" / "planning_nodes.py"

MUTATIONS = [
    # ── ① 事实表扩栈 ──
    (
        'P-C3：复合后缀档整块失效（`.blade.php` 退回 splitext ⇒ 判成 .php 源码，Laravel 整栈'
        '判不出前端形态）',
        SD,
        '    for suf in sorted(_TEMPLATE_COMPOUND_SUFFIX, key=len, reverse=True):\n'
        '        if low.endswith(suf):\n'
        '            return _TEMPLATE_COMPOUND_SUFFIX[suf]',
        '    for suf in ():\n'
        '        if low.endswith(suf):\n'
        '            return _TEMPLATE_COMPOUND_SUFFIX[suf]',
        # 首版还指名了 `test_template_suffix_table_covers_common_engines` → 突变后仍绿：
        # 它那些多段用例（`.html.erb`/`.html.heex`）splitext 后就是模板后缀，主表直接认得，
        # **不经复合表**。这个"仍绿"暴露的是我自己的冗余表项（已删两条），不是测试的错。
        ['test_blade_compound_suffix_is_recognized',
         'test_server_rendered_stacks_are_recognized'],
    ),
    (
        'P-C3：复合表塞回冗余条目（`.html.heex` 主表已认得 ⇒ 该条目删掉没人会红＝不可证伪）',
        SD,
        '_TEMPLATE_COMPOUND_SUFFIX = {\n    ".blade.php": "Blade",\n}',
        '_TEMPLATE_COMPOUND_SUFFIX = {\n    ".blade.php": "Blade",\n    ".html.heex": "HEEx",\n}',
        ['test_compound_table_holds_only_suffixes_splitext_would_lose'],
    ),
    # ★复合表的"按长度降序"今天不可证伪★ 表里只剩一条（`.blade.php`），单元素表的排序对任何
    # 输入都无区别 ⇒ 把 `key=len, reverse=True` 删掉没有任何测试会红。不给它造假锁；等表里
    # 真出现前缀相含的两条（如假想的 `.php` 与 `.blade.php` 同时登记）时再补。这条突变改为
    # 打**返回值**（那是真有区分力的一半）。
    (
        'P-C3：复合表命中却返回错的引擎名（引擎名要进 prompt 指导 worker 沿用既有引擎，'
        '写错＝让它去用项目里没有的引擎）',
        SD,
        '        if low.endswith(suf):\n            return _TEMPLATE_COMPOUND_SUFFIX[suf]',
        '        if low.endswith(suf):\n            return "HTML"',
        ['test_blade_compound_suffix_is_recognized'],
    ),
    (
        'P-C3：go 模板后缀 `.tmpl` 从表里消失（Gin 工程回到判 none）',
        SD,
        '    ".tmpl": "Go template",\n',
        '',
        ['test_server_rendered_stacks_are_recognized',
         'test_template_suffix_table_covers_common_engines'],
    ),
    (
        'P-C3：node 模板后缀 `.ejs` 从表里消失（Express 工程回到判 none）',
        SD,
        '    ".ejs": "EJS",\n',
        '',
        ['test_server_rendered_stacks_are_recognized',
         'test_template_suffix_table_covers_common_engines'],
    ),
    (
        'P-C3：python 模板依赖 marker 消失（Django/Flask 的 templates/*.html 无从判定——'
        '这类栈模板后缀就是 .html，只能靠依赖坐标佐证）',
        SD,
        '    "django": "Django Template",\n    "jinja2": "Jinja2",\n',
        '',
        ['test_server_rendered_stacks_are_recognized'],
    ),
    (
        'P-C3：认出形态却不发"禁止产独立 SPA"硬约束（分类对了、8537fa5e 死代码病照旧）',
        SD,
        '    if kind == "server-template":\n        lines.append(',
        '    if False:\n        lines.append(',
        ['test_server_rendered_stacks_get_the_no_spa_instruction'],
    ),
    # ── marker 命中面积（误伤方向）──
    (
        'P-C3：★误伤★ marker 匹配退回裸子串（`tera`←iterate/literal、`slim`←slimmer、'
        '`django`←djangorestframework ⇒ 把静态 .html / 前端产物判成服务端模板）',
        SD,
        '        if _SERVER_TEMPLATE_DEP_RE[dep].search(all_manifest_text):   # P-C3：词边界，非裸子串',
        '        if dep in all_manifest_text:',
        # 首版只指名 `..._is_word_bounded` → 突变后仍绿：那条**直接调正则表**，测的是实现
        # 正确、不是被接上了。补了走完整 detect() 的接线测试才锁得住调用点。
        ['test_word_boundary_is_actually_wired_into_detection'],
    ),
    (
        'P-C3：★误伤★ 词边界只加左侧（`django` 仍命中 `djangorestframework`）',
        SD,
        '    dep: re.compile(r"\\b" + re.escape(dep) + r"\\b") for dep in _SERVER_TEMPLATE_DEP',
        '    dep: re.compile(r"\\b" + re.escape(dep)) for dep in _SERVER_TEMPLATE_DEP',
        ['test_dep_marker_matching_is_word_bounded'],
    ),
    (
        'P-C3：收窄 marker 时把既有 JVM 坐标弄丢（`taglibs-standard-jstlel` 从"认得"退成'
        '"认不得"＝误杀方向的静默回归）',
        SD,
        '    "jstlel": "JSP/JSTL",\n',
        '',
        ['test_word_boundary_is_zero_regression_for_jvm_markers'],
    ),
    # ── ② fail-closed 方向：漏判 ──
    (
        'P-C3：「认不得」档整块消失（未收录栈回到"真没有前端"的高置信错答案）',
        SD,
        '    elif unengined_tmpl_dir_files > 0:',
        '    elif False:',
        ['test_unrecognized_engine_is_distinguishable_from_no_frontend',
         'test_unrecognized_signal_has_a_prompt_consumer',
         'test_unrecognized_lowers_confidence_below_the_adjudication_threshold'],
    ),
    # ★刻意不锁 `or tmpl_engine_unrecognized` 那条子句★ 突变实验证明它今天不可隔离：
    # `unrecognized` 蕴含 `frontend_kind=="none"`（拿不到 +0.2/+0.1 前端加分），置信度上界
    # 0.5+0.25-0.2=0.55 恒 < 0.65 ⇒ 前一个条件恒真，删掉它没有任何测试会红。给它编一条
    # "能红"的测试＝把"哪道闸在生效"这一维从命题里抹掉（[[swarm-redundant-defense-
    # unfalsifiable]]）。改为锁**可观测的那一半**：置信度确实被压到兜底线以下。
    (
        'P-C3：认不得却不降置信（画像以 0.75 高置信定案「无前端」＝病灶本体的另一半）',
        SD,
        '    if tmpl_engine_unrecognized:\n        # P-C3：不是答案而是"没看清"，必须降到兜底线以下（needs_adj 阈值 0.65）。\n        confidence -= 0.2',
        '    if tmpl_engine_unrecognized:\n        # P-C3：不是答案而是"没看清"，必须降到兜底线以下（needs_adj 阈值 0.65）。\n        confidence -= 0.0',
        ['test_unrecognized_lowers_confidence_below_the_adjudication_threshold'],
    ),
    (
        'P-C3：机读键恒 False（下游永远收不到"认不得"，层内自吞＝外层永远不知道）',
        SD,
        '        tmpl_engine_unrecognized = True',
        '        tmpl_engine_unrecognized = False',
        ['test_unrecognized_engine_is_distinguishable_from_no_frontend',
         'test_unrecognized_signal_has_a_prompt_consumer'],
    ),
    (
        'P-C3：降级路径不留 WARNING（血规 10 第四条：机读键 + 一次 WARNING 缺一不可）',
        SD,
        '        logger.warning(\n            "[STACK-DETECT] P-C3 模板目录下有 %d 个网页文件',
        '        logger.debug(\n            "[STACK-DETECT] P-C3 模板目录下有 %d 个网页文件',
        ['test_unrecognized_engine_is_distinguishable_from_no_frontend'],
    ),
    (
        'P-C3：机读键无 prompt 消费者（新账没人消费＝没造；`none` 档仍什么都不发 ⇒ 默许'
        'LLM 规划独立 SPA 工程）',
        SD,
        '    elif (profile.get("signals") or {}).get("tmpl_engine_unrecognized"):',
        '    elif False:',
        ['test_unrecognized_signal_has_a_prompt_consumer'],
    ),
    (
        'P-C3：人读画像仍谎称"无独立前端"（机读键对了但人读面骗人——两个面都进 prompt）',
        SD,
        '        frontend = "无法判明前端形态（模板目录下有网页文件，但未识别出模板引擎）"',
        '        frontend = "无独立前端（API/后端为主，或前端未在本仓）"',
        ['test_unrecognized_engine_is_distinguishable_from_no_frontend'],
    ),
    # ── ② fail-closed 方向：误伤 ──
    (
        'P-C3：★误伤★ 闸只要"有网页文件"就报认不得（不再要求在模板目录下）⇒ 散落的 '
        'docs/coverage.html 就让纯 API 工程被判"没看清"、烧一次模型裁决 + 收到误导指令',
        SD,
        '                _segs = {s.lower() for s in rel.replace(os.sep, "/").split("/") if s}\n'
        '                if _segs & _TEMPLATE_DIR_NAMES:\n'
        '                    unengined_tmpl_dir_files += 1',
        '                unengined_tmpl_dir_files += 1',
        # 首版还指名了 `..._real_api_only_project_...` → 突变后仍绿：那两个夹具**一个网页
        # 文件都没有**（纯 go / 纯 Maven API），去掉目录条件也改不动它们。真正锁住这条的是
        # 带散落 `docs/coverage.html` 的那个夹具。
        ['test_no_template_dir_means_no_flag_even_with_stray_html'],
    ),
    (
        'P-C3：★误伤★ 网页后缀表放宽到收 .css/.js（静态资源目录叫 views 的项目把噪声当证据）',
        SD,
        '_WEBPAGE_EXTS = frozenset({".html", ".htm", ".xhtml", ".php", ".tpl", ".shtml"})',
        '_WEBPAGE_EXTS = frozenset({".html", ".htm", ".xhtml", ".php", ".tpl", ".shtml",\n'
        '                           ".css", ".js"})',
        # 首版指名 SPA 那条 → 突变后仍绿，两重原因：① 夹具里没有 .css/.js 文件；② 即便有，
        # `has_spa` 先命中就走不到"认不得"分支。故另立一条**非 spa 非 server-template** 的
        # 纯 API 夹具（views/ 下放静态资源），那里分支真的可达。
        ['test_webpage_ext_table_excludes_asset_extensions'],
    ),
    (
        'P-C3：★误伤★ 判定序把"认不得"提到 SPA 之前（前端是 Vue、后端另有未收录引擎的邮件'
        '模板 ⇒ 形态明确的工程收到"别当没前端、先取证"的误导指令）',
        SD,
        '    elif has_spa:\n        frontend_kind = "spa"',
        '    elif has_spa and not unengined_tmpl_dir_files:\n        frontend_kind = "spa"',
        # 落点改了两次都因**不等价**恒绿：v1 选 `elif has_server_tmpl` 而夹具 unengined==0；
        # v2 把 `elif has_spa` 换成 `elif unengined>0` —— 夹具里 unengined==1，仍进同一分支
        # 设同一个值 = 等于没突变。现在这条让 unengined 抢在 SPA 前面（真实危害形状）。
        ['test_spa_wins_over_unrecognized_when_both_present'],
    ),
    (
        'P-C3：★误伤★ 扫描失败画像也带上"认不得"（连目录都没读到却发"别当没前端"，'
        '且可能把空证据推给 LLM 裁决＝幻觉画像产地）',
        SD,
        '                    "tmpl_engine_unrecognized": False,\n'
        '                    "unengined_template_dir_files": 0},',
        '                    "tmpl_engine_unrecognized": True,\n'
        '                    "unengined_template_dir_files": 1},',
        ['test_scan_failed_profile_has_the_same_signals_shape'],
    ),
    # ── 粘滞信号（读裁决路径时逮到的自伤）──
    (
        'P-C3：★粘滞★ 模型裁决落定后不清"认不得"标（触发它的机制已把问题答完，它还在响 ⇒ '
        'prompt 对已裁决的画像继续发"我看不清、别当没前端"）',
        PN,
        '                if (profile.get("signals") or {}).get("tmpl_engine_unrecognized"):\n'
        '                    profile["signals"]["tmpl_engine_unrecognized"] = False',
        '                if False:\n'
        '                    profile["signals"]["tmpl_engine_unrecognized"] = False',
        ['test_adjudication_clears_the_flag_so_it_is_not_sticky'],
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
        mutated = src.replace(old, new, 1)
        try:
            ast.parse(mutated)
        except SyntaxError as _e:
            print(f"[{i}/{len(MUTATIONS)}] {name}\n"
                  f"    ✗ 突变后源码无法编译（{_e.msg} @line {_e.lineno}）⇒ pytest 只会报 "
                  f"collection error，rc≠0 是假信号。")
            failures.append((name, "突变产生语法错"))
            continue
        path.write_text(mutated)
        try:
            per = [(n, _pytest(["-k", n])) for n in should_red]
            missing = [n for n, r in per if r == 5]
            green = [n for n, r in per if r == 0]
            print(f"[{i}/{len(MUTATIONS)}] {name}")
            if not missing and not green:
                print(f"    ✓ 指名的 {len(per)} 条全红")
            else:
                if missing:
                    print(f"    ✗ 测试名选不到（rc=5）: {missing}")
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
