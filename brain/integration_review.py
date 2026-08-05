"""L2 集成审查 — 合并后确定性编译 + 契约符号检查。"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

from swarm.brain.contract_utils import contract_symbols
from swarm.project.diff_apply import apply_git_diff, files_from_unified_diff

logger = logging.getLogger(__name__)


def _reset_worktree_to_head(project_path: str, merged_diff: str, base_ref: str | None = None) -> list[str]:
    """把 merged_diff 涉及的文件 reset 到干净的补丁基线（清除 worker pull-back 写入的脏改动）。

    精准处理补丁涉及的文件（不动工作区其他文件）。非 git 仓库跳过（返回 []）。
    - 【已跟踪文件】（base 有）：checkout 回 base 版本，撤销脏改动。
    - 【新建文件】（base 没有，但 worker pull-back 已写进工作区）：删除工作区残留——
      否则 git apply 要新建该文件时报"文件已存在/补丁未应用"（task 691c1670 实证：
      6 文件 CRUD 全是新建，pull-back 写入后 checkout 无效残留 → apply --check 全失败）。

    3rd#2：base_ref = 任务钉扎的 base commit（None → "HEAD"，零回归）。merged_diff 是【相对 base】
    生成的补丁，故复位基线必须同源=base，否则运行期 HEAD 漂移后 reset 到 HEAD → 补丁基线不符 →
    apply 失败或覆盖用户中途 commit。L2 与 learn_success 共用本函数，一处钉两处。

    返回 reset【失败】的文件清单（空=全净）。F5（20号文 merge 审计）：旧实现全程
    best-effort——per-file 非零不查、os.remove 吞 OSError、整段异常只 warning——半失败时
    下游双向误判（apply --check 假红归因代码失败 / 交付 commit 混入 pull-back 旧残留）。
    失败清单由调用方分流：L2 pre-check 按"结论不可信"infra 降级、沙箱 L2 以箱内
    porcelain 脏树判 infra None、交付临界区 fail-closed 不写毒树。整体异常=任意文件
    状态未知 → 全量记失败（fail-closed 宁多勿漏）。
    """
    import os
    from swarm.git_base import base_ref_exists, resolve_base_ref
    _base = resolve_base_ref(base_ref)
    # 对抗复核 H2：钉扎 base 若不可达（用户 reset --hard + GC 改写历史，罕见）→ 退回 HEAD，
    # 否则 `cat-file -e base:f` 全失败会把【已跟踪文件】误判为"新建"→ os.remove 删掉用户文件。
    # base 正常是 HEAD 祖先（只在其上追加 commit）恒可达，本守卫仅防历史被破坏性重写的极端场景。
    if base_ref and _base != "HEAD" and not base_ref_exists(project_path, base_ref):
        logger.warning("[L2] 钉扎 base %s 不可达(历史被重写/GC?)，reset 退回 HEAD 防误删跟踪文件", _base[:12])
        _base = "HEAD"
    failed: list[str] = []
    files: list[str] = []
    try:
        files = files_from_unified_diff(merged_diff) or []
        if not files:
            return []
        chk = subprocess.run(
            ["git", "-C", project_path, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=15,
        )
        if chk.returncode != 0:
            return []  # 非 git 仓=无可 reset（既有语义，非失败）
        for f in files:
            # 判断该文件在 base 是否存在（已跟踪 vs 新建）
            in_head = subprocess.run(
                ["git", "-C", project_path, "cat-file", "-e", f"{_base}:{f}"],
                capture_output=True, text=True, timeout=15,
            ).returncode == 0
            if in_head:
                # 已跟踪 → reset 到 base 版本
                r = subprocess.run(
                    ["git", "-C", project_path, "checkout", _base, "--", f],
                    capture_output=True, text=True, timeout=15,
                )
                if r.returncode != 0:
                    # F5：旧实现不查非零=半失败静默。文件被占用/权限时脏改残留 →
                    # 下游 apply --check 假红或交付混旧残留。
                    failed.append(f)
                    logger.warning("[L2] reset 半失败(已跟踪文件 checkout 非零): %s: %s",
                                   f, (r.stderr or "").strip()[:200])
            else:
                # 新建文件 → 删除工作区残留（pull-back 写入的），让 apply 能干净新建
                abs_f = os.path.join(project_path, f)
                if os.path.isfile(abs_f):
                    try:
                        os.remove(abs_f)
                    except OSError as _oe:
                        failed.append(f)
                        logger.warning("[L2] reset 半失败(新建文件删除失败): %s: %s", f, _oe)
                # 也从 git index 撤出（worker checkpoint 可能 git add 过）——先查 index
                # 再撤：未 add 过的新文件 `git rm --cached` 恒非零（pathspec 不匹配），
                # 直接记失败会把每个新建文件 reset 都误报成半失败。
                _idx = subprocess.run(
                    ["git", "-C", project_path, "ls-files", "-z", "--", f],
                    capture_output=True, text=True, timeout=15,
                )
                if _idx.returncode == 0 and _idx.stdout:
                    r = subprocess.run(
                        ["git", "-C", project_path, "rm", "--cached", "--force", "-q", f],
                        capture_output=True, text=True, timeout=15,
                    )
                    if r.returncode != 0 and f not in failed:
                        failed.append(f)
                        logger.warning("[L2] reset 半失败(index 撤出非零): %s: %s",
                                       f, (r.stderr or "").strip()[:200])
    except Exception as exc:  # noqa: BLE001
        # F5：整段异常=任意文件状态未知 → 全量记失败（fail-closed 宁多勿漏），
        # 绝不返回空清单让调用方误以为工作区已净。
        logger.warning("[L2] reset worktree to HEAD failed: %s", exc)
        return list(dict.fromkeys([*failed, *(files or ["<reset-exception>"])]))
    return failed


# ══════════════════════════════════════════════════════════════════
# B-4a（27 号文 V-C1）：构建面三态 —— "没有构建"与"这个栈的编译闸没实现"必须分得开
# ══════════════════════════════════════════════════════════════════

NO_BUILD_SURFACE = "no_build_surface"
"""磁盘上**真没有**任何构建清单（纯 docs/config 仓）→ 跳过编译是合理的，不产 issue。"""

UNSUPPORTED_STACK = "unsupported_stack"
"""磁盘上**有**构建面，但本函数派生不出编译命令 → 该栈的 L2 编译闸**未实现**。

★这是 V-C1 那条 CRITICAL 的根★ 旧实现把两者都写成 `compile_ok=None` 且**不产任何 issue**
→ `passed = len(issues) == 0` → **判 PASS**。于是 PHP/Ruby/C#/Elixir/仅 requirements.txt 的
Python/无 build script 的 Node **L2 永久 no-op 且判通过**，坏产物直达交付。
全仓无消费者能区分这两种 None —— 现在用 `details["compile_skip_reason"]` 机读可辨。
"""

# 本函数派生不出编译命令、但**确实是个工程**的清单 → 归 UNSUPPORTED_STACK（fail-closed）。
# 这些栈根本不在 `stacks.STACK_SPEC` 里（B-7 的"新栈准入闸"要对账的正是这张表）；
# 在此显式列出，是为了让"没实现"与"没有构建"分开，绝不让前者伪装成后者。
#
# ★刻意【不收】`Makefile` 与 `CMakeLists.txt`（双复核 HIGH-3 实证误杀）★
# 它们是**构建工具**而非**栈**，且 `Makefile` 作任务运行器在纯 docs 仓/脚本仓极常见
# （Sphinx 的 `make html` 就是）。实测：纯 docs 仓 + Sphinx Makefile → `unsupported_stack:make`
# → 按当时的产 issue 实现会烧完 replan 后 FAILED。要正确收它们必须有"该栈源码在场"的旁证
# （`*.c`/`*.cpp`/`*.h`），那是 B-7 源码证据闸的活，不在本批。宁可漏报不误杀——漏报由
# `unregistered_aggregate_stacks()` 一类的覆盖面登记继续兜（B-7）。
_UNSUPPORTED_STACK_MANIFESTS: tuple[tuple[str, str], ...] = (
    ("composer.json", "php"),
    ("Gemfile", "ruby"),
    ("mix.exs", "elixir"),
    ("pubspec.yaml", "dart"),
    ("build.sbt", "scala-sbt"),
)
_UNSUPPORTED_STACK_GLOBS: tuple[tuple[str, str], ...] = (
    ("*.sln", "csharp"),
    ("*.csproj", "csharp"),
    ("*.fsproj", "fsharp"),
)


def detect_build_surface(project_path: str) -> tuple[str | None, str, list[str]]:
    """构建面三态：返回 `(build_cmd | None, reason, matched_manifests)`。

    - `(cmd, "ok", [...])` —— 派生出全量编译命令
    - `(None, NO_BUILD_SURFACE, [])` —— 磁盘上真没有构建清单（纯 docs）→ 合理跳过
    - `(None, "unsupported_stack:<key>", [...])` —— **有**构建面但没有编译闸

    第三态的存在就是 V-C1 的治本：`compile_ok=None` 此前同时表示"合理跳过"与"没实现"，
    而 `passed=len(issues)==0` 让后者静默变成 PASS。

    ★第三个返回值是全部命中清单（双复核 MEDIUM-3）★ 短路首命中会让混栈仓报错栈：实测
    `Rails(Gemfile)+requirements.txt → python`、`Laravel(composer.json)+package.json → npm`
    ——人按 python/npm 去查，真问题在 ruby/php。栈键仍取确定性首命中（判序稳定），但把全集
    一并交出，让判读的人看得见"其实同时命中了这些"。

    ★只查工程根（诚实边界，双复核 HIGH-1）★ 后端在子目录的 monorepo（根上无清单、
    `backend/pom.xml`）仍落 `NO_BUILD_SURFACE` → 仍是假过，**与治本前逐字相同**。
    §1 矩阵"任意栈 + 后端在子目录"那一行本批**未动**，归 B-4b 的 V-H1（有界子树探测）。
    本批关的是"根上有清单的未收录栈"，不是 V-C1 全集。
    """
    cmd = _detect_build_cmd_generic(project_path)
    j = os.path.join
    import glob as _glob

    matched: list[str] = []
    first_key: str | None = None

    # ① 已收录栈（`stacks.STACK_SPEC` 单一事实源）的根清单
    try:
        from swarm.stacks import STACK_SPEC
        for spec in STACK_SPEC.values():
            for m in spec.root_manifests:
                if os.path.isfile(j(project_path, m)):
                    matched.append(m)
                    if first_key is None:
                        first_key = spec.key
    except Exception as exc:  # noqa: BLE001 — 事实表不可用不该让 L2 崩，但要留痕
        logger.warning("[integration_review] V-C1 STACK_SPEC 探测异常（降级为仅查未收录栈表）: %s",
                       exc)

    # ② 未收录栈的清单（PHP/Ruby/Elixir/Dart/sbt + C# 的 glob）
    for name, key in _UNSUPPORTED_STACK_MANIFESTS:
        if os.path.isfile(j(project_path, name)):
            matched.append(name)
            if first_key is None:
                first_key = key
    for pattern, key in _UNSUPPORTED_STACK_GLOBS:
        # LOW-1：路径含 `[`/`]` 时 glob 会把它当字符类 → 漏检 → 退回假过。
        if _glob.glob(j(_glob.escape(project_path), pattern)):
            matched.append(pattern)
            if first_key is None:
                first_key = key

    if cmd:
        return cmd, "ok", matched
    if first_key:
        return None, f"{UNSUPPORTED_STACK}:{first_key}", matched
    return None, NO_BUILD_SURFACE, matched


def _detect_build_cmd_generic(project_path: str) -> str | None:
    """据构建文件确定【整工程全量编译命令】——**不** gate 本机工具可用性（编译在项目沙箱按检测
    版本工具链跑；本机是退回路径）。返回 None 仅当【无任何已知构建文件】(如纯 docs)→合理跳过
    编译，非降级。治本 round21：原 `_detect_build_cmd` 把"本机没装工具"和"没有构建"混为一谈都返
    None → L2 静默跳过编译→假绿。现分离二者：有构建文件即返命令，工具在哪跑由调用方决定。

    ★返回 None 的**语义已不足以判断"能否跳过"**★ —— 用 `detect_build_surface()` 拿三态。

    ★M-1/M-6（用户拍板"L2 复用 L1 的实现"）★ 本函数原是一条**手写 if 链**，与
    `l1_pipeline._derive_full_build_command` 同职责却各存一份 ⇒ 必然漂移：实测它当时仍是
    root-only、无锚定、且不认 C#/PHP/Ruby/Elixir/Dart（L1 已认）。本会话被同型分叉咬过三次
    （`_manifest_present` 本地/沙箱、`_build_cmd_applicable` 漏调用点、两探针排除表）。

    治法＝**共享事实、各自投影**：栈→整工程命令这份事实搬进 `stacks.STACK_SPEC`
    （`whole_project_build_cmd`），本函数只做"匹配根清单 → 取该栈命令"。
    ★两层 scope 语义不同，故不直接互调★：L1 是子任务级（锚到改动文件所在清单目录，python 只编
    改动文件），L2 是整工程级（merge 后必须编译全部代码）。共享的是命令事实，不是 scope 策略。

    Maven/Gradle/Go/Cargo/Python 路径与旧实现**逐字等价**（命令串是从旧 if 链原样搬进表的）。
    """
    j = os.path.join
    try:
        from swarm.stacks import STACK_SPEC
    except Exception as exc:  # noqa: BLE001 — 事实表不可用不该让 L2 崩，但要留痕
        logger.warning("[integration_review] STACK_SPEC 不可用，构建命令探测降级为 None: %s", exc)
        return None

    # 判序按**清单特异性**：maven/gradle/go/cargo 的清单互不重叠，python 的 requirements.txt
    # 可能与别的栈共存（Django + 前端），故 python 放最后——与旧 if 链的先后一致。
    for key in ("maven", "gradle", "go", "cargo", "npm", "python"):
        spec = STACK_SPEC.get(key)
        if spec is None:
            continue
        if not any(os.path.isfile(j(project_path, m)) for m in spec.root_manifests):
            continue
        if key == "npm":
            # npm 是**条件式**、要读 package.json 内容，不是纯静态事实，故不进表：
            # I1-#13（round38c 主题I·外部深审）：旧命令尾部 `|| true` 把 Node 真编译失败全部
            # 吞成 exit 0=L2 假绿假 DONE。改确定性选择：有 build script 用 npm run build；
            # 否则有 tsconfig 用 tsc；两者皆无=纯 JS 无确定性编译面，诚实返回 None。
            try:
                import json as _json
                with open(j(project_path, "package.json"), encoding="utf-8") as _f:
                    _pkg = _json.loads(_f.read() or "{}")
                if (_pkg.get("scripts") or {}).get("build"):
                    return "npm run build"
            except Exception:  # noqa: BLE001 — package.json 坏 → 按无 build script 处理
                pass
            if os.path.isfile(j(project_path, "tsconfig.json")):
                return "npx tsc --noEmit --pretty false"
            return None
        if spec.whole_project_build_cmd:
            return spec.whole_project_build_cmd
        # 表里有这个栈却没给整工程命令 ⇒ 事实缺失，必须可见（硬检查④），不静默当"没有构建"
        logger.warning(
            "[integration_review] STACK_SPEC['%s'] 匹配到根清单但 whole_project_build_cmd 为空"
            "⇒ L2 拿不到整工程编译命令。请补表（M-1：本表是 L1/L2 共享的单一事实源）", key)
        return None
    return None


def _local_tool_available(build_cmd: str) -> bool:
    """build_cmd 的首个可执行(mvn/go/cargo/npm/python/gradle/./gradlew)是否在【本机】可用。"""
    import shutil
    first = (build_cmd or "").strip().split()[0] if build_cmd.strip() else ""
    if first.startswith("./"):
        return True  # ./gradlew 等项目内脚本，交由 shell 判定
    return bool(first) and shutil.which(first) is not None


def _run_cmd(project_path: str, cmd: str, *, timeout: int = 300) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=project_path,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        ok = proc.returncode == 0
        detail = (proc.stderr or proc.stdout or "").strip()[:2000]
        return ok, detail
    except subprocess.TimeoutExpired:
        return False, "compile timeout"
    except Exception as exc:
        return False, str(exc)


# API 路径末段退化出的泛词——它们在任何 diff 里都必然命中，作为"契约已实现"的证据
# 判别力为零（26 号文 I-H3）。本表只用于**如实报告闸门的失明面**，不用于剔除符号：
# 剔除会缩小分母把通过率做得更好看，与诚实相反。栈中立（纯 REST/CRUD 通用动词与名词）。
_NON_DISCRIMINATING_SYMBOLS = frozenset({
    "list", "add", "edit", "get", "set", "put", "post", "delete", "remove", "save",
    "update", "create", "new", "index", "detail", "info", "data", "item", "items",
    "query", "search", "find", "all", "one", "page", "count", "check", "test",
    "export", "import", "upload", "download", "id", "type", "name", "value", "status",
})


def check_contract_in_diff(
    merged_diff: str,
    shared_contract: dict[str, Any] | None,
) -> tuple[bool, list[str]]:
    """检查共享契约中的符号是否出现在变更 diff 中（启发式）。

    D5（阶段6，登记册 §五）：旧判定二态皆坏——【全部】符号缺失才 fail（缺 90% 也放行=
    形同虚设），且 issue 不带逐符号明细无从归因。改缺失率阈值（默认 0.4，
    SWARM_CONTRACT_MISSING_RATIO 可调；0=任一缺失即 fail，1=回退旧全缺语义），
    issue 首条带全部缺失符号清单（供 verify 侧按符号归因 owner 定向重派）。
    仍为启发式子串匹配（存量语义，防误杀由阈值+归因收窄兜）。"""
    symbols = contract_symbols(shared_contract)
    if not symbols:
        return True, []
    # ★只在【新增行与上下文行】里找，绝不认删除行（26 号文 I-H3）★
    # 原判据是整份 diff 的大小写不敏感子串——**删除行与注释都算"存在"**。
    # 复核实测：5/5 契约符号全部只出现在 `-` 行（即这次变更把它们删了），闸门仍判 True。
    # 符号只在删除行出现，语义恰恰是"它被移除了"，与"契约已实现"完全相反。
    # 上下文行（前导空格）保留为有效证据：symbol 已在基线、本次只改其内部实现是合法形态。
    _hay: list[str] = []
    for _ln in (merged_diff or "").splitlines():
        if not _ln:
            continue
        _c = _ln[0]
        if _c == "-" or _ln.startswith("---"):
            continue                      # 删除行 / 文件头
        if _ln.startswith("+++") or _ln.startswith("@@") or _ln.startswith("diff --git"):
            continue                      # diff 元信息（文件名里的词不算实现证据）
        _hay.append((_ln[1:] if _c in "+ " else _ln).lower())
    diff_lower = "\n".join(_hay)
    # R43 复核 F4：I 前缀符号接受基名子串（与 C1 惯例等价口径对称，防两张皮位移到 L2）
    from swarm.brain.contract_utils import symbol_diff_variants
    missing = [s for s in symbols
               if not any(v in diff_lower for v in symbol_diff_variants(s))]
    # ★无判别力符号必须如实报告，绝不假装验过（26 号文 I-H3 第一重）★
    # `contract_symbols` 对 API 条目取路径末段：`GET /system/device/list` → `list`。
    # 这种泛词在整份 diff 里必然命中，等于该契约条目**根本没被验证**——而它此前被计入
    # "已覆盖"的分子，把闸门的通过率虚高。
    # ★刻意不改 contract_symbols 本身★：它是 C1 规划期对账的共享单一事实源，C1 的消费
    # 契约（做符号 owner 归属）里泛词是可用的。共享表不动，**消费契约随后果分档**——
    # 这正是本轮 W1 复核 HIGH-2 的教训（详见 swarm-reuse-contract-not-just-source）。
    _blind = sorted({s for s in symbols
                     if s.lower() in _NON_DISCRIMINATING_SYMBOLS and s not in missing})
    if _blind:
        logger.warning(
            "[CONTRACT] %d 个契约符号无判别力（API 路径末段退化成泛词，本闸对其结构性失明，"
            "不构成'已实现'证据）：%s", len(_blind), _blind[:10])
    if not missing:
        return True, []
    import os
    try:
        _ratio = float(os.environ.get("SWARM_CONTRACT_MISSING_RATIO", "0.4") or "0.4")
    except ValueError:
        _ratio = 0.4
    if len(missing) / len(symbols) > _ratio:
        return False, [
            f"契约符号缺失率 {len(missing)}/{len(symbols)} 超阈值({_ratio:.0%})，"
            f"缺失清单: {missing[:20]}"]
    logger.info("[CONTRACT] 契约符号缺失 %d/%d（≤阈值 %.0f%%，放行）: %s",
                len(missing), len(symbols), _ratio * 100, missing[:10])
    return True, []


def run_integration_review(
    project_path: str,
    merged_diff: str,
    shared_contract: dict[str, Any] | None = None,
    *,
    timeout: int = 600,
    compile_runner=None,
    base_ref: str | None = None,
) -> tuple[bool, list[str], dict[str, Any]]:
    """L2.1 全量编译 + L2.3 契约一致性（确定性）。

    compile_runner(build_cmd) -> (ran: bool, ok: bool, output: str)：可选【沙箱编译器】。给定则优先在
    项目沙箱(按检测栈版本烤的工具链)跑全 reactor 编译；沙箱不可用退回本机(仅当本机装了该栈工具)。
    治本 round21：二者都不行 → **fail-loud 拒绝假绿**(不再像旧版本机缺 mvn 就静默跳过编译当通过)。
    timeout 默认 600s(全 reactor 编译 + 首轮依赖解析,Blocker C)。"""
    details: dict[str, Any] = {"stage": "integration_review"}
    issues: list[str] = []

    if not merged_diff.strip():
        return False, ["empty merged_diff"], details

    if not project_path or not os.path.isdir(project_path):
        return False, ["no project path"], details

    contract_ok, contract_issues = check_contract_in_diff(merged_diff, shared_contract)
    issues.extend(contract_issues)
    details["contract_check"] = contract_ok

    # ── 关键(task fdaa1932)：先把工作区 reset 到干净 HEAD 再做 git apply --check ──
    # merged_diff 是【相对 HEAD】生成的补丁。但 worker pull-back 已把改动写进了本地
    # project_path 工作区文件（isXxx/toXxx 方法已存在）→ 工作区是【脏】的。直接在脏工作区
    # git apply --check 会因 "改动已存在、context 已变" 报 "补丁未应用"（假阴性，task
    # fdaa1932 实测）。reset 到 HEAD 后工作区与补丁基线一致，check 才有意义。worker 的脏改动
    # 已被 merged_diff 完整捕获，reset 不丢信息（真正 apply 在下方 build_cmd 分支重新做）。
    #
    # F2（merge 审计 HIGH）：整个工作树变更窗口（reset→apply-check→apply→reconcile→编译
    # →finally reset）收进 _ProjectGitFlock——与交付临界区 _deliver_merged_diff_locked、
    # executor_sync pull-back 同一把跨进程锁（同 canon_path）。E3 降级后同项目不同模块的两任务
    # 设计上可并行走到 verify_l2（交付侧 docstring 自证），本窗口裸跑会把兄弟任务交付 flock 内
    # apply 与 commit 之间的树 reset 回 base、或把兄弟 pull-back 半成品扫进本任务的 L2 编译输入。
    # 锁粒度取"整段持锁"（正确性优先；沙箱编译等待≤timeout，兄弟侧最坏排队同额时长）。
    from swarm.worker.git_flock import _ProjectGitFlock

    _flk = _ProjectGitFlock(project_path)
    with _flk:
        details["worktree_flock"] = bool(getattr(_flk, "_locked", False))
        return _run_worktree_phase(
            project_path, merged_diff, details, issues,
            timeout=timeout, compile_runner=compile_runner, base_ref=base_ref,
        )


def _run_worktree_phase(
    project_path: str,
    merged_diff: str,
    details: dict[str, Any],
    issues: list[str],
    *,
    timeout: int,
    compile_runner,
    base_ref: str | None,
) -> tuple[bool, list[str], dict[str, Any]]:
    """run_integration_review 的工作树变更段（F2：调用方已持 _ProjectGitFlock）。"""
    _reset_failed = _reset_worktree_to_head(project_path, merged_diff, base_ref=base_ref)
    if _reset_failed:
        # F5（20号文）：reset 半失败 → 工作区残留未知 → apply --check/编译结论双向不可信
        # （假红=残留致 "补丁未应用" 误判代码失败进重试；假绿=脏基线上编译）。按
        # compile_unverified 同口径 infra 降级：fail-loud 不判代码失败、绝不假绿放行。
        details["reset_failed_files"] = _reset_failed
        details["compile_ok"] = None
        details["compile_unverified"] = True
        issues.append(
            f"L2 infra：工作区 reset 半失败（{len(_reset_failed)} 文件: {_reset_failed[:5]}）"
            "→ apply/编译结论不可信，按未核验降级（非代码失败；查文件占用/权限后重试）"
        )
        logger.warning("[integration_review] F5 reset 半失败 → infra 降级: %s", _reset_failed[:8])
        return False, issues, details

    apply_result = apply_git_diff(project_path, merged_diff, check_only=True)
    if not apply_result.get("ok"):
        issues.append(f"git apply --check failed: {apply_result.get('stderr', '')[:500]}")
        details["apply_check"] = False
        return False, issues, details
    details["apply_check"] = True

    build_cmd, _surface_reason, _surface_manifests = detect_build_surface(project_path)
    details["build_cmd"] = build_cmd
    details["compile_skip_reason"] = None if build_cmd else _surface_reason
    if build_cmd:
        applied = apply_git_diff(project_path, merged_diff)
        if not applied.get("ok"):
            issues.append(f"git apply failed: {applied.get('stderr', '')[:500]}")
            return False, issues, details
        # 通用治本：merged_diff apply 后磁盘上所有成员模块目录都在场(ground truth)。在【集成构建前】
        # 对账聚合清单(Maven/Gradle/Cargo/.NET/Go)，使其枚举真实存在的成员——杜绝并行子任务 pull-back
        # 整文件覆盖把成员注册【冲掉】致集成构建【假失败】(找不到模块)。此处是验证态(下方 finally 会
        # reset 工作区)，持久化由交付 commit 处的同一对账器保证(learn_success)。
        try:
            from swarm.worker.workspace_manifest import reconcile_workspace_manifests
            _wm = reconcile_workspace_manifests(project_path)
            if _wm.get("modified_manifests"):
                details["manifest_reconciled"] = _wm.get("added")
                logger.info("[integration_review] 集成构建前对账聚合清单成员: %s", _wm.get("added"))
        except Exception as _exc:  # noqa: BLE001
            logger.debug("[integration_review] 聚合清单对账跳过(异常,不致命): %s", _exc)
        # D2 版本完整性闸门：reconcile 后仍有【内部模块依赖版本无处可得】者 → reactor 解析必失败。
        # 交付前确定性判死(fail-closed)，别等 900s 编译超时才现形。仅 Maven 内部模块，不碰外部依赖。
        try:
            from swarm.worker.workspace_manifest import missing_intra_project_module_versions
            _missing_ver = missing_intra_project_module_versions(project_path)
            if _missing_ver:
                details["missing_intra_module_versions"] = _missing_ver
                issues.append(
                    "L2 pom 版本完整性: 内部模块依赖缺版本且无 dependencyManagement 兜底(reactor 解析必失败): "
                    + "; ".join(_missing_ver[:8])
                )
                logger.warning("[integration_review] D2 版本闸门报缺: %s", _missing_ver[:8])
        except Exception as _exc:  # noqa: BLE001
            logger.debug("[integration_review] D2 版本闸门跳过(异常,不致命): %s", _exc)
        try:
            # 编译执行：优先【项目沙箱】(按检测栈版本烤的工具链,多栈/多版本自动正确)；沙箱不可用
            # 退回【本机】(仅当本机装了该栈工具)。治本 round21：二者都不行 → fail-loud 拒绝假绿。
            ran = False
            ok = False
            out = ""
            if compile_runner is not None:
                try:
                    ran, ok, out = compile_runner(build_cmd)
                except Exception as _cexc:  # noqa: BLE001
                    logger.warning("[integration_review] 沙箱集成编译异常，尝试本机退回: %s", _cexc)
                    ran = False
                if ran:
                    details["compile_env"] = "sandbox"
            if not ran and _local_tool_available(build_cmd):
                ok, out = _run_cmd(project_path, build_cmd, timeout=timeout)
                ran = True
                details["compile_env"] = "local"
            if ran:
                details["compile_ok"] = ok
                details["compile_output"] = out
                if not ok:
                    issues.append(f"L2.1 集成编译失败: {out[:300]}")
            else:
                # 无沙箱 + 本机无该栈工具链 → 无法验证集成编译。绝不假绿放行(round19 死因之一：
                # 本机缺 mvn→静默跳过编译→L2 假绿→把没编译过的代码当"生产级"交付)。
                details["compile_ok"] = None
                details["compile_unverified"] = True
                issues.append(
                    "L2 集成编译无法执行(沙箱不可用且本机缺该栈工具链)——拒绝假绿放行；"
                    "请确保集成验证沙箱/宿主装有目标栈工具链(见 README 运行环境依赖)"
                )
        finally:
            # R1：限定回滚到 merged_diff 涉及的文件（复用 _reset_worktree_to_head 的 scoped 逻辑：
            # 已跟踪→checkout HEAD，新建→删除），不再用整库 `checkout -- .` + `clean -fd`——
            # 后者会抹掉用户在该项目里无关的未提交改动/未跟踪文件。
            # F5：回滚半失败=工作区残留 → 后续沙箱 L2/冒烟 sync 的是脏树（箱内 apply 假红）。
            # 入账 details（消费端=_run_l2_in_sandbox 箱内 porcelain 判据的旁证），绝不静默。
            _rb_failed = _reset_worktree_to_head(project_path, merged_diff, base_ref=base_ref)
            if _rb_failed:
                details["rollback_reset_failed"] = _rb_failed
                # 闸门 R2（reviewer MEDIUM②/hunter LOW-6 实锤）：只入 details 时无 test_cmd
                # 的任务 verify_l2 直接 l2_passed=True——工作区残留被记成 L2 通过=假绿，
                # 且脏树留共享工作区污染后续任务（M-3 家族）。与 compile_unverified 同口径
                # 升级为 infra 降级 issue（passed=len(issues)==0 自动翻 False；verify.py 侧
                # 对称 guard 拦在归因前，绝不误定向写者子任务）。
                issues.append(
                    "L2 回滚半失败（工作区残留，后续 sync/冒烟将带脏树）: "
                    + ", ".join(_rb_failed[:8])
                    + " ——infra 降级（非代码失败），按归因不出全量 replan（查文件占用/权限）")
                logger.warning("[integration_review] F5 L2 回滚半失败（工作区残留，infra "
                               "降级翻转 passed）: %s", _rb_failed[:8])
    elif _surface_reason.startswith(UNSUPPORTED_STACK + ":"):
        # ★V-C1 治本（B-4a，双复核整改后的形态）★ 磁盘上**有**构建面，但本仓没给这个栈实现
        # L2 编译闸。旧行为：与"纯 docs"共用 `compile_ok=None` 且**不产 issue** →
        # `passed=len(issues)==0` → **判 PASS** → 坏产物直达交付（§1 矩阵那一列 ✖判PASS）。
        #
        # ★为什么这里【不产 issue】（双复核 CRITICAL-3，两路独立证伪我上一版）★
        # 产 issue → `passed=False` → `l2_passed=False` → `after_verify_l2` **强制**
        # `handle_failure` → replan ×2 → escalate → DELIVER → FAILED/PARTIAL。而"这个栈的
        # 编译闸没实现"是**磁盘事实**，与 worker 产出无关，replan **零修复力**：重规划后 L2
        # 必逐字复现同一 issue，每轮都在烧钱重跑一件确定修不好的事。更坏的是 escalate 出态
        # 必带 `failure_escalated=True`，人看到的拒因变成"子任务重试耗尽已升级人工"——把
        # "我们没验"说成"worker 没干好"，归因错得比原来更远，还会连坐重派无辜子任务。
        #
        # §6.3 原则 3 原文要的就是另一条路：**不是拦交付，是拒绝 auto_accept、强制人工确认**。
        # 所以这里只留【机读事实】，由 verify_l2 转成 `degraded_reasons`（state 键、进
        # checkpoint、已有 build_degraded_summary 与 deliver payload 两个现成消费者），
        # 再由 `can_auto_accept_delivery` 据此拒放行。`record_degrade` 保留作运维计数，
        # 但**它不是那条通道**（进程内 counter、重启即丢、无任务维度）。
        _stack_key = _surface_reason.split(":", 1)[1] or "unknown"
        details["compile_ok"] = None
        details["compile_gate_unsupported_stack"] = _stack_key
        details["compile_surface_manifests"] = _surface_manifests
        try:
            from swarm.infra.degrade import record_degrade
            record_degrade(f"brain.l2.compile_gate_unsupported_stack.{_stack_key}")
        except Exception as _dexc:  # noqa: BLE001 — 计数失败绝不影响主判定，但不静默
            logger.warning("[integration_review] degrade 计数失败（不影响判定）: %s", _dexc)
        logger.warning(
            "[integration_review] V-C1 L2 编译闸对栈 %s 未实现（有构建面 %s 却无编译命令）"
            "→ 不产 issue（replan 修不了磁盘事实），转 degraded_reasons 拒 auto_accept",
            _stack_key, _surface_manifests)
    else:
        details["compile_ok"] = None  # 真无构建文件(纯 docs/config) → 合理跳过，非降级
        logger.info("[integration_review] 无构建文件(纯 docs/config)，跳过全量编译")

    modified = files_from_unified_diff(merged_diff)
    details["modified_files"] = modified

    # audit #25：passed 判定改用结构化标志——issues 本就是"问题列表"，非空即未通过。
    # 原 `not any("failed" in i.lower() ...)` 靠子串匹配，既会漏判(问题描述里无 "failed"
    # 字样的真问题被放行)，又会误判("No test failed" 这类描述含 "failed" 被判失败)。
    passed = len(issues) == 0
    return passed, issues, details
