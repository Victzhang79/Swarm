#!/usr/bin/env python3
"""P-C2 突变 harness（判据与前七批同源，那些自伤一开始就带上）：

  · **先验基线全绿**（只验"突变→红"会让**修得不全**的整改全绿通过：B-4b I-1 实证）；
  · **逐条**跑 should_red，每条都必须红；· `rc=5`（`-k` 选不到，如测试被重命名）**判失败**；
  · 落点唯一性检查；· 突变后源码必须仍能 `ast.parse`（否则 rc≠0 只是 collection error）。

★锁的命题★ P-C2＝"显式版本是待验证的主张，绝非证据"，且**误杀方向**必须封死：
判幻觉的能力天然带来"把不可达当不存在 ⇒ 离线一次清空所有显式依赖"的风险，故 R56-6
（证据缺失≠否定证据）的每一处实现都单独上锁——半数突变打的正是这个方向。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = str(ROOT / ".venv" / "bin" / "python")
TESTS = ["test/test_pc2_explicit_version_is_a_claim.py",
         "test/test_npm_registry_p2b.py",
         "test/test_go_registry_p2c.py",
         "test/test_scaffold_npm_go_driver_p2.py"]

NPM = ROOT / "brain" / "npm_registry.py"
GO = ROOT / "brain" / "go_registry.py"

MUTATIONS = [
    # ── npm：治理整块失效 ──
    (
        'P-C2/npm：显式区间退回"直采/尊重之"（整个治理消失 ⇒ ^99.0.0 又被烤进权威模板）',
        NPM,
        '            _kind = _range_kind(explicit)',
        '            _kind = "protocol"   # 突变：一律不判＝直采',
        ['test_npm_hallucinated_range_is_corrected_to_latest',
         'test_npm_hallucinated_range_with_no_stable_is_dropped_honestly',
         'test_npm_dist_tag_latest_is_resolved_not_kept'],
    ),
    # ── npm：误杀方向（R56-6）──
    (
        'P-C2/npm：★误杀★ registry 不可达被当成"版本不存在"（离线一次清空所有显式依赖）',
        NPM,
        '            _vers = registry_all_versions(name)\n            if _vers is None:',
        '            _vers = registry_all_versions(name) or frozenset()\n            if False:',
        ['test_npm_unreachable_registry_fails_open_and_keeps_the_claim'],
    ),
    (
        'P-C2/npm：★误杀★ 全量版本集空文档返空集合而非 None（该包所有版本恒判幻觉）',
        NPM,
        '        if isinstance(versions, dict) and versions:\n            return frozenset(v for v in versions if isinstance(v, str) and v)',
        '        if isinstance(versions, dict):\n            return frozenset(v for v in versions if isinstance(v, str) and v)',
        ['test_registry_all_versions_empty_or_broken_doc_is_none_not_empty'],
    ),
    (
        'P-C2/npm：★误杀★ 全量版本集过滤掉预发布（LLM 写 1.8.0-beta.1 被判幻觉）',
        NPM,
        '            return frozenset(v for v in versions if isinstance(v, str) and v)',
        '            return frozenset(v for v in versions if isinstance(v, str) and _is_stable(v))',
        ['test_registry_all_versions_includes_prereleases'],
    ),
    (
        'P-C2/npm：★误杀★ 协议/复合区间也拿去判可满足性（workspace:* 必然不可满足）',
        NPM,
        '            if _kind in ("protocol", "complex"):',
        '            if _kind in ("protocol",):',
        ['test_npm_unjudgeable_forms_are_never_touched'],
    ),
    (
        'P-C2/npm：内部 workspace 包分流移到判定之后（兄弟包不在 registry ⇒ 被判幻觉/丢弃）',
        NPM,
        '        if name in internal:\n            seen.add(name)\n            kept.append(ResolvedNpmDep(name=name, spec="workspace:*", source="workspace"))\n            continue',
        '        if name in internal and False:\n            seen.add(name)\n            kept.append(ResolvedNpmDep(name=name, spec="workspace:*", source="workspace"))\n            continue',
        ['test_npm_workspace_internal_still_wins_over_version_judgement'],
    ),
    # ── npm：semver 语义 ──
    (
        'P-C2/npm：caret 的 major-0 特例被抹掉（^0.3.0 会命中 0.2.9 ⇒ 放过幻觉）',
        NPM,
        # ★#29-3 T-1 落点更新★ 该分支前面插入了新臂，`if` 已变成 `elif`（`^1`=`1.x` / `^0`
        # 那条判据先走）。仅一个关键字之差，旧字面量却自那次起落点未命中＝零覆盖 ——
        # 这类"一个 token 的漂移"最不显眼，也正是静态审计存在的理由。
        '            elif floor[0] == 0:\n                # 0.x：次版本即破坏性；0.0.z 更严（精确）',
        '            elif False:\n                # 0.x：次版本即破坏性；0.0.z 更严（精确）',
        ['test_range_satisfiability_semantics'],
    ),
    (
        'P-C2/npm：精确形态退化成前缀匹配（1.6.1 会命中 1.6.0 ⇒ 放过幻觉）',
        NPM,
        '                if cur == floor:                       # `1.2.3` = 精确',
        '                if cur[0] == floor[0] and cur[1] == floor[1]:',
        ['test_range_satisfiability_semantics'],
    ),
    # ── go：三态探测（误杀核心）──
    (
        'P-C2/go：★误杀★ except 顺序写反（HTTPError 是 URLError 子类 ⇒ 404 被吞成 None，'
        '治理整体失效且无声）',
        GO,
        '    except urllib.error.HTTPError as exc:      # 必须在 URLError 之前——它是其子类\n'
        '        out = False if getattr(exc, "code", None) in (404, 410) else None',
        '    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:\n'
        '        out = None',
        ['test_http_probe_is_tristate'],
    ),
    (
        'P-C2/go：★误杀★ 5xx/429 也算"确证不存在"（服务端抽风即判幻觉）',
        GO,
        '        out = False if getattr(exc, "code", None) in (404, 410) else None',
        '        out = False',
        ['test_http_probe_is_tristate'],
    ),
    (
        # ★这条的落点只有"零镜像"能触发★ 首版指名镜像语义那条测试 → 突变后仍绿：那条走的是
        # 循环内早返（`[False, None]`），末行没执行。两处防御互相兜底 = 单点突变不可证伪，
        # 故另立一条零镜像入口的测试来锁末行（见测试里的说明）。
        # ★#29-3 T-1 落点更新★ R4（d3497e5）把 `False` 这一档**刻意取消**了：go proxy 的 404
        # 语义是"可能别处有"而非"包不存在"，旧行为把"两镜像都 404"升格成确证查无 ⇒ 私有 module
        # 被误判幻觉丢弃。故 `saw_false` 变量与 `return False if saw_false else None` 一并消失，
        # 末行现在是无条件 `return None`。突变意图不变：把 `False` 档塞回去 ⇒ 误杀复发。
        'P-C2/go：★误杀★ 零证据（镜像表为空）塌成"确证不存在"',
        GO,
        '    return None                    # 无一镜像能证实存在 → 证据不完整，绝不据此判幻觉',
        '    return False  # 突变：False 档塞回来',
        ['test_proxy_version_exists_with_no_mirrors_is_none_not_false'],
    ),
    (
        # ★#29-3 T-1 落点重写★ 原落点是"不可达即早返 None"那条 else 臂 —— R4 把 `False` 与
        # `None` 两档合并后，循环里已不存在"早返 vs continue"的分叉（所有非 True 结局都归 None）。
        # 突变意图（拿不完整证据判幻觉）现在的等价形态＝**在循环内**把首个 404 升格成 False：
        # 首个镜像 404 就返回 False ⇒ 不再要求"所有镜像都确认缺席"⇒ 私有 module 误杀复发。
        # 与上一条（#11 压**末行**的零镜像出口）落在不同位置、由不同测试指名，两者不互相兜底。
        'P-C2/go：★误杀★ 首个镜像 404 即判确证不存在（不再要求所有镜像确认缺席）',
        GO,
        '        got = _http_probe(tpl.format(mod=enc, ver=encv))\n'
        '        if got is True:\n'
        '            return True',
        '        got = _http_probe(tpl.format(mod=enc, ver=encv))\n'
        '        if got is True:\n'
        '            return True\n'
        '        if got is False:\n'
        '            return False  # 突变：首个 404 即确证不存在',
        ['test_proxy_version_exists_requires_all_mirrors_to_confirm_absence'],
    ),
    # ── go：治理面 ──
    (
        'P-C2/go：显式版本退回"直采/契约已给定"（整个治理消失）',
        GO,
        # ★#29-3 T-1 落点重写★ BRAIN-003 把判据从「`not _JUDGEABLE_VERSION.match(_exp)
        # or _PSEUDO_ANY.search(_exp)`」换成先算两个中间量、再判「`not _is_judgeable and
        # not _is_pseudo`」——原枚举漏了分支名（dev / release-1.2）。旧字面量自那次起零覆盖。
        '            if not _is_judgeable and not _is_pseudo:',
        '            if True:',
        ['test_go_hallucinated_version_is_corrected',
         'test_go_hallucinated_version_no_latest_is_dropped'],
    ),
    (
        'P-C2/go：★误杀★ proxy 不可达被当成"版本不存在"',
        GO,
        # ★#29-3 T-1 落点更新★ 该判据已抽成命名中间量（`_unverified = _exists is None`，
        # 随后 `if _unverified:`）—— 旧的 `if _exists is None:` + 注释那段字面量不复存在。
        # 突变意图不变：让"不可达"不再算 unverified ⇒ 当成"版本不存在" ⇒ fail-open 失效、误杀。
        '            _unverified = _exists is None',
        '            _unverified = False  # 突变：不可达不再算未验证',
        ['test_go_unreachable_proxy_fails_open'],
    ),
    (
        'P-C2/go：★误杀★ 伪版本也拿去探测（伪版本是真实可用形态，探必然 404）',
        GO,
        # ★#29-3 T-1 落点重写★ 原落点是判据里的 ` or _PSEUDO_ANY.search(_exp)` 这半截
        # （BRAIN-003 重构后不存在）。伪版本豁免现由中间量 `_is_pseudo` 承载，故压它：
        # 置 False ⇒ 伪版本落进"拿去探测"的路 ⇒ 探必然 404 ⇒ 被当幻觉校正/丢弃（原误杀）。
        '            _is_pseudo = bool(_PSEUDO_ANY.search(_exp))',
        '            _is_pseudo = False  # 突变：伪版本豁免删除',
        # ★#29-3 T-1 测试名同步★ 原名 `test_go_unjudgeable_versions_are_never_probed` 已随
        # R3/BRAIN-003 改成 `..._pseudo_...`（那次把"不可判"与"伪版本"拆成两个概念）。
        # 测试名选不到会被 harness 判 rc=5＝失败（而不是"红了"），这道判据本轮正好抓住了它。
        ['test_go_pseudo_versions_are_never_probed'],
    ),
    (
        'P-C2/go：★误杀★ 伪版本判别退回窄口径 `_PSEUDO`（漏掉 `v0.0.0-<ts>-<hash>` 这种'
        '最常见形态 ⇒ 无前置 tag 的伪版本全被送去探测、当幻觉校正掉）',
        # ★#29-3 T-1 落点更新★ `_PSEUDO_ANY` 已被**重写**（单行 → 三行，并支持带前置 tag 的
        # 预发布伪版本形态，且不再需要 IGNORECASE）。旧单行字面量自那次起零覆盖。
        # 突变意图不变：退回窄口径 ⇒ `v0.0.0-<ts>-<hash>` 这类最常见形态漏判 ⇒ 被送去探测。
        GO,
        '_PSEUDO_ANY = re.compile(\n'
        r'    r"-(?:(?:[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*\.)?0\.)?\d{14}-[0-9a-f]{12}"' + '\n'
        r'    r"(?:\+incompatible)?$")',
        '_PSEUDO_ANY = re.compile(\n'
        r'    r"-\d+\.\d{14}-[0-9a-f]{12}$")',
        ['test_go_pseudo_versions_are_never_probed'],
    ),
    (
        'P-C2/go：`+incompatible` 被划进"不判"（这类库的幻觉版本从此免检，漏一大片 Go 生态）',
        GO,
        r'_JUDGEABLE_VERSION = re.compile(r"^v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+incompatible)?$")',
        r'_JUDGEABLE_VERSION = re.compile(r"^v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")',
        ['test_go_incompatible_suffix_is_judgeable'],
    ),
    (
        'P-C2/go：内部 module 分流移到判定之后（没发布 ⇒ 探测必 404 ⇒ 误杀）',
        GO,
        '        if mod in internal_set:\n            seen.add(mod)\n            internal.append(mod)\n            continue',
        '        if mod in internal_set and False:\n            seen.add(mod)\n            internal.append(mod)\n            continue',
        ['test_go_internal_module_still_wins_over_version_judgement'],
    ),
    (
        # ★首版只改"读"不改"写" ⇒ 突变后仍绿★ 写还落在 `_probe_cache`，于是"两个 dict 不同一"
        # 和"探测没污染文本缓存"两句断言都照样成立。不变量由**读+写两行共同编码**，突变落点
        # 必须整块换（[[swarm-redundant-defense-unfalsifiable]]），测试也同步补成"文本缓存里
        # 同 URL 的 None 绝不被探测读成不可达"。
        'P-C2/go：探测缓存与文本缓存合用一个 dict（两种缺席又塌成一个值）',
        GO,
        '    _key = f"probe::{url}"\n    if _key in _probe_cache:\n        return _probe_cache[_key]',
        '    _key = url\n    if _key in _http_cache:\n        return _http_cache[_key]  # type: ignore[return-value]',
        ['test_go_probe_cache_is_separate_from_text_cache'],
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
                    print(f"    ✗ 测试名选不到（rc=5）: {missing}")
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
