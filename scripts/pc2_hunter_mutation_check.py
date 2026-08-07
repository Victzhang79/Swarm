#!/usr/bin/env python3
"""P-C2 复核（hunter F-1..F-5）整改的突变 harness。判据与前几批同源：

  · **先验基线全绿** —— 只验"突变→红"会让修得不全的整改蒙过去；
  · **逐条**跑 should_red，每条都必须红（"整组任一条红"会让零区分力的名字永不被发现）；
  · `rc=5`（`-k` 一条都没选到，如测试被重命名）**判失败**，不是"红了"；
  · 落点唯一性检查（出现多次＝突变不等价）；
  · `finally` 无条件还原 + 末尾基线复验（绝不把突变留在磁盘上）。

★绝不放进带超时的循环★ SIGKILL 会跳过 finally，突变源码留在磁盘上而下一步就是提交。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_pc2_review_hunter_findings.py",
         "test/test_pc2_explicit_version_is_a_claim.py",
         # code-reviewer A-4 整改把守卫改到这里，其基线必须一起验（否则 T65 的突变无从判红）
         "test/test_r65tr_t5_baseline_convention.py"]

GO = ROOT / "brain" / "go_registry.py"
NPM = ROOT / "brain" / "npm_registry.py"
MVN = ROOT / "brain" / "maven_registry.py"
CU = ROOT / "brain" / "contract_utils.py"
PF = ROOT / "brain" / "plan_finisher.py"
ST = ROOT / "brain" / "state.py"
DHC = ROOT / "brain" / "dep_http_cache.py"
MVN = ROOT / "brain" / "maven_registry.py"
RUN = ROOT / "brain" / "runner.py"
T65 = ROOT / "test" / "test_r65tr_t5_baseline_convention.py"

MUTATIONS = [
    # ── F-1：负缓存永久（退回旧写法）──
    (
        "F-1: go _http_get 退回无条件缓存（None 永久钉死坐标）",
        GO,
        "    hit, cached = text_cache_lookup(_http_cache, _http_neg_until, url)\n"
        "    if hit:\n        return cached",
        "    if url in _http_cache:\n        return _http_cache[url]",
        ["test_f1_transient_failure_does_not_stick_forever",
         "test_f1_all_three_registries_wired"],
    ),
    (
        "F-1: npm _http_get 退回无条件缓存（三处 sibling 之一漏改）",
        NPM,
        "    hit, cached = text_cache_lookup(_http_cache, _http_neg_until, url)\n"
        "    if hit:\n        return cached",
        "    if url in _http_cache:\n        return _http_cache[url]",
        ["test_f1_all_three_registries_wired"],
    ),
    (
        "F-1: maven _http_get 退回无条件缓存（sibling 之三）",
        MVN,
        "    hit, cached = text_cache_lookup(_http_cache, _http_neg_until, url)\n"
        "    if hit:\n        return cached",
        "    if url in _http_cache:\n        return _http_cache[url]",
        ["test_f1_all_three_registries_wired"],
    ),
    (
        # 只指接线测试：`test_f1_success_clears_negative_record` 已下沉成 dep_http_cache 的
        # 单元测试（因为经 _http_get 时它零区分力，见该测试 docstring），按设计不碰 go 的接线，
        # 本条突变改的正是 go 侧接线 ⇒ 拿它当判据是错的账（第二轮实测：它绿、接线测试红）。
        "F-1: 写入侧退回无条件（TTL 记不上 ⇒ 读侧的 TTL 判断恒无记录）",
        GO,
        "    text_cache_store(_http_cache, _http_neg_until, url, text)",
        "    _http_cache[url] = text",
        ["test_f1_all_three_registries_wired"],
    ),
    (
        # ★#29-3 T-1 落点更新★ 该段已重构：TTL 检查移进 `if val is None or
        # _is_mutable_endpoint(key):` 内（缩进 4→8），返回值从 `True, None` 改为 `True, val`
        # （正缓存也走这条路）。旧字面量自那次重构起落点未命中＝零覆盖。
        "F-1: 无 TTL 记录的 None 当成命中（来历不明的 None 复现永久误杀）",
        DHC,
        "        if neg_until.get(key, 0.0) > time.monotonic():\n"
        "            return True, val\n",
        "        if True:\n            return True, val\n",
        ["test_f1_transient_failure_does_not_stick_forever",
         "test_f1_unknown_none_without_ttl_is_revalidated",
         "test_f1_all_three_registries_wired"],
    ),
    (
        # ★这条突变第一轮实测"仍绿"★ 原因：走 `_http_get` 时 lookup 侧的过期清理已把
        # neg_until pop 掉，store 侧的 pop 无从被观测＝冗余防御两处都不可证伪。
        # 断言已下沉到单元层（直接调 text_cache_store）才隔离得出来。
        # ★#29-3 T-1 落点更新★ 注释已改写（"成功即清掉…" → "成功且不可变：清掉…/可变 TTL…"）。
        # ★必须盯 store 侧那一处★：`neg_until.pop(key, None)` 在本文件有**两处** ——
        # `text_cache_lookup()`:74（过期清理）与 `text_cache_store()`:92（成功后清负记录）。
        # 本条锁的是**后者**（上面注释里写明"断言已下沉到单元层直接调 text_cache_store"）。
        # 若图省事把落点改成无注释的 `        neg_until.pop(key, None)`，会打到 lookup 侧
        # ⇒ 锁的是另一个机制，测试还可能照绿＝比原来的死锁更难发现。
        "F-1: 成功后不清负记录（陈旧 TTL 残留）",
        DHC,
        "        neg_until.pop(key, None)   # 成功且不可变：清掉旧的负记录/可变 TTL，"
        "避免陈旧 TTL 影响后续判定",
        "        pass",
        ["test_f1_success_clears_negative_record"],
    ),
    # ── F-3：完全不缓存 None（照搬 F5 的办法 ⇒ 代价放大）──
    (
        # ★指名测试已修正（#29-3 W-25）★：原先第一名是
        # `test_f3_ttl_window_suppresses_repeat_network_cost`，但它的夹具走 `/@latest`
        # ——那 URL **同时**是可变端点，本突变下 `elif _is_mutable_endpoint(key)` 会替
        # `None` 记一条 300s TTL，缓存照旧命中 ⇒ 该条对本突变**零区分力**（当时全靠
        # 兄弟条 test_f1 抓住，等于这条锁在替它背书）。改为指名用**非可变** URL 的隔离锁。
        "F-3: 改成完全不缓存 None（退化成 F5 办法 ⇒ per-module 反复烧网）",
        DHC,
        "    cache[key] = value\n    if value is None:",
        "    cache[key] = value\n    if value is None and False:",
        ["test_f3_none_ttl_branch_isolated_on_non_mutable_url",
         "test_f1_all_three_registries_wired"],
    ),
    # ── F-4：> 臂退回拿补零 floor 当下界 ──
    (
        "F-4: > 臂退回 cur > floor（>1 当成 >1.0.0 ⇒ 1.0.1 假过）",
        NPM,
        "            if prec == 1:\n"
        "                if cur >= (floor[0] + 1, 0, 0):\n"
        "                    return True\n"
        "            elif prec == 2:\n"
        "                if cur >= (floor[0], floor[1] + 1, 0):\n"
        "                    return True\n"
        "            elif cur > floor:\n"
        "                return True",
        "            if cur > floor:\n                return True",
        ["test_f4_gt_arm_treats_missing_segments_as_xrange"],
    ),
    (
        "F-4: 只治 prec==1 漏 prec==2（修得不全：>1.2 仍假过）",
        NPM,
        "            elif prec == 2:\n"
        "                if cur >= (floor[0], floor[1] + 1, 0):\n"
        "                    return True\n",
        "",
        ["test_f4_gt_arm_treats_missing_segments_as_xrange"],
    ),
    # ── F-5：伪版本模式放宽过头 ──
    (
        "F-5: 前缀退回任意串+点（跳闸通道重开）",
        GO,
        r'    r"-(?:(?:[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*\.)?0\.)?\d{14}-[0-9a-f]{12}"'
        "\n"
        r'    r"(?:\+incompatible)?$")',
        r'    r"-(?:[0-9A-Za-z.-]+\.)?\d{14}-[0-9a-f]{12}(?:\+incompatible)?$", re.IGNORECASE)',
        ["test_f5_pseudo_pattern_matches_official_forms_only"],
    ),
    # ── F-2：机读账各环节 ──
    (
        "F-2: go 显式版本不分档（三种结局又塌成 explicit）",
        GO,
        'verified="unverified" if _unverified else "verified"))',
        'verified="verified"))',
        ["test_f2_three_outcomes_are_machine_distinguishable"],
    ),
    (
        "F-2: npm 不可达档不分（fail-open 与确证不可分）",
        NPM,
        '                kept.append(ResolvedNpmDep(name=name, spec=explicit, source="explicit",\n'
        '                                           verified="unverified"))\n'
        "                continue\n"
        "            if not _range_is_satisfiable(explicit, _vers):",
        '                kept.append(ResolvedNpmDep(name=name, spec=explicit, source="explicit"))\n'
        "                continue\n"
        "            if not _range_is_satisfiable(explicit, _vers):",
        ["test_f2_three_outcomes_are_machine_distinguishable"],
    ),
    (
        "F-2: 记账挪到 injected 之后（owner-backfill/无 self_path 两条出口漏记）",
        CU,
        "        # F-2：先于任何 continue 记账（本 driver 三条出口，只记注入那条＝又只接主调用点）\n"
        "        _record_unverified_deps(unverified_out, mod, kept)\n",
        "",
        ["test_f2_unverified_reaches_the_machine_readable_ledger"],
    ),
    (
        # ★这条突变第一轮实测"仍绿"★ 原因：指名的账测试只跑 npm，go 分支从未被执行
        # （夹具不触发目标分支＝假绿第二形态）。改指 go 侧那条专门的栈中立测试。
        # 字面量随 A-1 重写同步（coord 现在三栈三分支）。旧字面量已不在盘上 ⇒ 第四轮报"落点未命中"，
        # 那是 harness 刻意的失败态（拒绝在漂移代码上给假绿），不是误报。
        "F-2: 记账函数只认 npm 字段（go 侧记成 ?@? ⇒ 栈中立破了）",
        CU,
        '        coord = (getattr(k, "module", None) or getattr(k, "name", None)\n',
        '        coord = (getattr(k, "name", None) or getattr(k, "name", None)\n',
        ["test_f2_ledger_is_stack_neutral_go_side"],
    ),
    (
        "F-2: 记账函数只认 go 字段（反向：npm 侧记成 ?@?）",
        CU,
        '        coord = (getattr(k, "module", None) or getattr(k, "name", None)\n',
        '        coord = (getattr(k, "module", None) or getattr(k, "module", None)\n',
        ["test_f2_unverified_reaches_the_machine_readable_ledger"],
    ),
    (
        # 第二轮实测"仍绿"：原断言只查 `"axios" in s`，而突变后条目是 `axios@?(unverified)`
        # ——名字还在 ⇒ 断言过。已改成断完整坐标 `axios@^1.6.0(unverified)`。
        "F-2: 版本字段只认 go（npm spec 记成 ?）",
        CU,
        '        ver = getattr(k, "version", None) or getattr(k, "spec", None) or "<无版本>"',
        '        ver = getattr(k, "version", None) or "<无版本>"',
        ["test_f2_unverified_reaches_the_machine_readable_ledger"],
    ),
    (
        "F-2: 版本字段只认 npm（go version 记成 ?，反向）",
        CU,
        '        ver = getattr(k, "version", None) or getattr(k, "spec", None) or "<无版本>"',
        '        ver = getattr(k, "spec", None) or "<无版本>"',
        ["test_f2_ledger_is_stack_neutral_go_side"],
    ),
    (
        "F-2: plan_finisher 不发该键（账断在最后一环）",
        PF,
        '        out["dep_versions_unverified"] = {m: sorted(set(v)) for m, v in _unverified.items()}',
        "        pass",
        ["test_f2_plan_finisher_emits_the_key_always"],
    ),
    # ── code-reviewer 复核整改（A-1/A-3/A-7/A-4）──
    (
        "A-1: maven 不带 verified 三档（主栈 RuoYi 全部记成已证实）",
        MVN,
        '        _verified = "unjudgeable" if (version or "").startswith("${") else "unverified"',
        '        _verified = "verified"',
        ["test_a1_maven_dep_carries_verified_three_tiers"],
    ),
    (
        # ★第四轮实测"仍绿"★ 原指的账测试直接调 `_record_unverified_deps`，压根不经 driver
        # ⇒ 测的是函数不是接线。已改指走公开入口的那条（同一形状本会话第三次）。
        "A-1: maven driver 不记账（主栈整个不入账，{} 与全证实不可分）",
        CU,
        "        _record_unverified_deps(unverified_out, mod, _kept)\n",
        "",
        ["test_a1_maven_reaches_ledger_through_public_entry"],
    ),
    (
        "A-1: 记账函数不认 maven 字段（group:artifact ⇒ 记成 ?@）",
        CU,
        '                 or (f"{k.group}:{k.artifact}" if getattr(k, "artifact", None) else None)\n',
        "",
        ["test_a1_maven_record_fn_is_stack_neutral",
         "test_a1_maven_reaches_ledger_through_public_entry"],
    ),
    (
        "A-7: 缺 verified 属性时乐观默认（新栈静默漏账）",
        CU,
        '        v = getattr(k, "verified", "unverified")',
        '        v = getattr(k, "verified", "verified")',
        ["test_a7_missing_verified_attr_defaults_pessimistic"],
    ),
    (
        "A-3: 进度端点不消费该账（回到零读点＝没造）",
        RUN,
        '        "dep_versions_unverified": state.get("dep_versions_unverified") or {},',
        "",
        ["test_a3_ledger_has_a_real_consumer_in_progress_api"],
    ),
    (
        "A-3: 先例键仍零消费（循环背书未消除）",
        RUN,
        '        "dep_ban_reconciled": state.get("dep_ban_reconciled") or {},',
        "",
        ["test_a3_ledger_has_a_real_consumer_in_progress_api"],
    ),
    (
        "A-4: 摘要退回只取顶层+signals（jvm 子键改动触发不了守卫）",
        T65,
        '    parts.append("profile_key_paths_py=" + ",".join(_key_paths(prof_py)))\n'
        '    parts.append("profile_key_paths_jvm=" + ",".join(_key_paths(prof_jvm)))',
        '    parts.append("profile_keys=" + ",".join(sorted(prof_py)))\n'
        '    parts.append("signals_keys=" + ",".join(sorted(prof_py.get("signals") or {})))',
        ["test_stack_schema_version_paired_with_cached_payload"],
    ),
    (
        "F-2: BrainState 不声明（LangGraph 静默丢弃 ⇒ 账到不了 checkpoint）",
        ST,
        "    dep_versions_unverified: dict  ",
        "    _dep_versions_unverified_undeclared: dict  ",
        ["test_f2_state_key_is_declared_in_brainstate"],
    ),
    (
        "F-2: reducer 档位改成 append-only（愈合后陈旧值粘滞）",
        ST,
        '    "dep_versions_unverified": "round",',
        '    "dep_versions_unverified": "task",',
        ["test_f2_state_key_is_declared_in_brainstate"],
    ),
    # ── R3：go 侧 latest/分支名/裸 SHA 写不进 go.mod ──
    (
        # ★#29-3 T-1：机制被**重构**，落点必须重写而非只改字符串★
        # BRAIN-003（commit d3497e5 一带）把判据从「枚举正则 `_UNGO_MODDABLE_VERSION.match`」
        # 换成「`not _is_judgeable and not _is_pseudo`」——原枚举只列 latest/master/main/sha，
        # **分支名（dev / release-1.2）漏网**被误当伪版本原样保留。旧落点自那次重构起零覆盖。
        # 顺带（已登记 findings）：`_UNGO_MODDABLE_VERSION` 现已是**死代码** —— 全文件只剩
        # 定义与一处注释提及，零消费者。
        "R3: 写不进 go.mod 的形态判据整块消失（latest/分支名/裸SHA 原样保留 ⇒ go build 解析期全灭）",
        GO,
        '            if not _is_judgeable and not _is_pseudo:\n',
        '            if False:\n',
        ["test_go_ungomoddable_versions_are_corrected_or_dropped"],
    ),
    (
        # ★#29-3 T-1 落点更新★ 缩进外移一层（判据从嵌套 if 变成同层 if），日志前缀由
        # `P-C2-R3` 改成 `P-C2-R3/BRAIN-003`。旧字面量自 BRAIN-003 起落点未命中＝零覆盖。
        "R3: 校正不到时 fail-open 保留（把解析错误烤进权威 go.mod）",
        GO,
        '                else:\n'
        '                    logger.warning("[go-registry] P-C2-R3/BRAIN-003 %s@%s 是写不进 go.mod 的形态，"\n'
        '                                   "且 proxy 不可达无法校正 → 如实丢弃（绝不把解析错误"\n'
        '                                   "烤进权威 go.mod）", mod, explicit)\n'
        '                    dropped.append(str(raw).strip())',
        '                else:\n'
        '                    kept.append(ResolvedGoDep(module=mod, version=explicit,\n'
        '                                              source="explicit", verified="unverified"))',
        ["test_go_ungomoddable_versions_are_corrected_or_dropped"],
    ),
    (
        # ★#29-3 T-1 落点更新★ 缩进随上面同一段外移一层。
        "R3: 校正成功却记成 unverified（机读账与事实不符）",
        GO,
        '                                              verified="verified"))',
        '                                              verified="unverified"))',
        ["test_go_ungomoddable_versions_are_corrected_or_dropped"],
    ),
    # ── F3：npm 侧"包不存在"必须与"不可达"机读可辨 ──
    (
        "F3: probe 存在性判据整块消失（registry_all_versions=None 时不再问'包是否存在'）",
        NPM,
        '                _exists = registry_package_exists(name)\n',
        '                _exists = None\n',
        ["test_f3_npm_package_not_found_is_dropped_not_kept",
         "test_f3_npm_package_unreachable_is_fail_open_kept"],
    ),
    (
        "F3: 确证不存在却 fail-open 保留（幻觉包名原样烤进 package.json）",
        NPM,
        '                if _exists is False:\n',
        '                if False:\n',
        ["test_f3_npm_package_not_found_is_dropped_not_kept"],
    ),
    (
        "F3: probe 的 None 也进缓存（一次抖动把该包永久钉成不可达，npm 侧 F5 同型复发）",
        NPM,
        '    if out is not None:\n        _probe_cache[_key] = out',
        '    _probe_cache[_key] = out',
        ["test_f3_probe_cache_has_no_negative_stickiness"],
    ),
    # ── R4：go proxy 404/410 ≠ 包不存在（False 档取消）──
    (
        "R4: 恢复 False 档（两镜像都 404 ⇒ 确证查无 ⇒ 私有 module 被误杀丢弃）",
        GO,
        '    return None                    # 无一镜像能证实存在 → 证据不完整，绝不据此判幻觉',
        '    return False                   # 无一镜像答 True → 确证查无',
        ["test_proxy_version_exists_requires_all_mirrors_to_confirm_absence",
         "test_f1_second_mirror_is_actually_queried"],
    ),
    (
        "R4: 单镜像 404 即判不存在（F1 多镜像冗余与 R4 语义双破坏）",
        GO,
        '        if got is True:\n            return True\n',
        '        if got is True:\n            return True\n        if got is False:\n            return False\n',
        ["test_proxy_version_exists_requires_all_mirrors_to_confirm_absence",
         "test_f1_second_mirror_is_actually_queried"],
    ),
    (
        "R4: 不可达却记 verified=verified（unverified 机读账说谎 ⇒ dep_versions_unverified 永远空）",
        GO,
        '                verified="unverified" if _unverified else "verified"))',
        '                verified="verified"))',
        ["test_go_hallucinated_version_no_latest_is_dropped",
         "test_go_hallucinated_version_is_corrected"],
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
    print("步骤 0：基线必须全绿（只验突变→红会让修得不全的整改蒙过去）")
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
