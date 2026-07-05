"""Worker 沙箱同步 / git / scope 混入 —— 从 worker/executor.py 抽出（round26 god-file 治理 Step2）。

SYNC 连通分量（16 方法，god-class 最大簇）：本地↔沙箱文件双向同步（_sync_to/from_sandbox）、
scope 文件枚举（_scope_files/_module_source_files/_build_manifest_files/_writable_files）、
git 基线/复位/diff（_git_baseline_text/_reset_scope_to_head/_get_git_diff/_try_local_git_diff）、
路径归一（_norm_rel）、沙箱删除传播/存在性/清单（_apply_local_deletions/_sandbox_file_exists/
_list_sandbox_workspace_files）、JVM 命名空间归一（_normalize_jvm_namespace）、本地快照
（_snapshot_scope_local）。

以【混入类】外置（同 _PromptBuildingMixin，见 executor_prompts.py 模块 docstring 的理由）：
方法共享 self._sandbox/_sandbox_manager/_pre_sync_contents/_post_sync_contents/_repaired_extra_paths/
_sync_skipped_count/_sync_error_rels/_deleted_local_paths/effective_scope/project_path（均由
WorkerExecutor.__init__ 初始化），且测试大量 patch.object(ex,"_get_git_diff")/
WorkerExecutor._writable_files.__get__(stub)/inspect.getsource(...._reset_scope_to_head) 钉方法可寻址，
mixin 经 MRO 全部保持可寻址、测试零改动。跨簇调用 self._log / self._resolve_project_stack 靠
composed 实例 MRO 解析，本 mixin 不持有它们。

本模块【禁】eager import worker.executor（防 A6 循环）——依赖直接从源模块导入；difflib/shlex/
shutil/subprocess/tempfile/rewrite_jvm_namespace 保持方法内 lazy import。
"""

from __future__ import annotations

import asyncio  # noqa: F401  # 供 async 方法体内 asyncio.* 使用
import logging
import os

from pathlib import Path

from swarm.config.settings import get_config
from swarm.git_base import resolve_base_ref
from swarm.models.errors import TransientInfraError
from swarm.paths import is_within_root
from swarm.worker.git_flock import _ProjectGitFlock

logger = logging.getLogger(__name__)


class _SandboxSyncMixin:
    """WorkerExecutor 的沙箱同步 / git / scope 方法簇（见模块 docstring）。

    仅读写 self 上由 WorkerExecutor.__init__ 初始化的沙箱/同步/scope 状态；不持有自身状态。
    """

    def _scope_files(self) -> list[str]:
        """上传到沙箱的文件清单（readable ∪ writable(modify) ∪ 构建清单，去重保序）。

        注意：排除 create_files——它们是【待新建】文件，本地不存在，强行 read/upload
        会 FileNotFoundError（曾导致 worker 把"新建 readme"当成读取不存在文件而卡住）。
        delete_files 也不上传（要删的没必要传）。

        关键：必须额外带上【构建清单文件】(pom.xml/build.gradle/go.mod/Cargo.toml/
        package.json 等)，否则 mvn/gradle/go build/cargo 在沙箱里因找不到工程描述
        文件而失败（实测 RuoYi: "no POM in /workspace"）。
        """
        scope = self.effective_scope
        files: list[str] = []
        create = set(getattr(scope, "create_files", []) or [])
        delete = set(getattr(scope, "delete_files", []) or [])
        for f in list(getattr(scope, "readable", []) or []) + list(getattr(scope, "writable", []) or []):
            rel = str(f).strip()
            if rel and rel not in files and rel not in create and rel not in delete:
                files.append(rel)
        # 追加构建清单（沙箱编译/测试的前提）
        for rel in self._build_manifest_files():
            if rel not in files and rel not in create and rel not in delete:
                files.append(rel)
        # 追加【改动所在模块的完整源码树】——仅当 harness 需真实编译时。
        # 精准 scope 同步只传选中文件，但 mvn/gradle 编译整模块会因缺同级类
        # (DateUtils 依赖 Constants/StringUtils 等)报 cannot find symbol 秒挂。
        # 编译型语言必须带齐改动模块的全部源码。
        for rel in self._module_source_files():
            if rel not in files and rel not in create and rel not in delete:
                files.append(rel)
        return files

    def _module_source_files(self) -> list[str]:
        """改动文件所在【构建模块】的完整源码树(仅编译型语言需要)。

        判据：harness.build_command 存在(说明要真实编译)。从改动文件向上找最近的
        构建清单(pom.xml/build.gradle)确定模块根，再收该模块 src/ 下全部源文件。
        防超大：单模块上限 800 文件。非编译型(无 build_command)返回空，保持精准同步。
        """
        harness = getattr(self.subtask, "harness", None)
        build_cmd = getattr(harness, "build_command", "") if harness else ""
        if not build_cmd or not self.project_path:
            return []
        # 仅对 JVM 系(mvn/gradle)启用整模块同步；其他语言模块边界不同，暂不扩展
        if not any(t in build_cmd for t in ("mvn", "gradle")):
            return []
        root = Path(self.project_path).resolve()
        scope = self.effective_scope
        changed = (list(getattr(scope, "writable", []) or [])
                   + list(getattr(scope, "create_files", []) or [])
                   + list(getattr(scope, "readable", []) or []))
        module_roots: set[Path] = set()
        for f in changed:
            cur = (root / str(f).strip()).resolve().parent
            # 向上找最近含 pom.xml/build.gradle 的目录 = 模块根
            while True:
                if (cur / "pom.xml").is_file() or (cur / "build.gradle").is_file() or (cur / "build.gradle.kts").is_file():
                    module_roots.add(cur)
                    break
                if cur == root or root not in cur.parents:
                    break
                cur = cur.parent
        out: list[str] = []
        _SRC_EXT = (".java", ".kt", ".scala", ".groovy")
        for mroot in module_roots:
            src_dir = mroot / "src"
            base = src_dir if src_dir.is_dir() else mroot
            count = 0
            for p in base.rglob("*"):
                if count >= 800:
                    break
                if not p.is_file() or p.suffix not in _SRC_EXT:
                    continue
                if "target" in p.relative_to(root).parts or "build" in p.relative_to(root).parts:
                    continue
                try:
                    out.append(str(p.relative_to(root)))
                    count += 1
                except ValueError:
                    continue
        return out

    def _build_manifest_files(self) -> list[str]:
        """发现项目里的构建清单文件，确保沙箱编译有工程描述。

        覆盖 5 语言主流构建系统。从【已 scope 的文件路径】向上回溯各级目录找清单
        (多模块项目根 + 模块各有 pom.xml)，再补项目根的清单。只返回真实存在的相对路径。
        """
        if not self.project_path:
            return []
        root = Path(self.project_path).resolve()
        manifest_names = (
            "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
            "settings.gradle.kts", "go.mod", "go.sum", "Cargo.toml", "Cargo.lock",
            "package.json", "tsconfig.json", "pyproject.toml", "setup.py",
            "requirements.txt", "build.xml",
        )
        found: list[str] = []

        def _add_dir_manifests(d: Path) -> None:
            for name in manifest_names:
                p = d / name
                if p.is_file():
                    try:
                        rel = str(p.relative_to(root))
                    except ValueError:
                        continue
                    if rel not in found:
                        found.append(rel)

        # 1) 项目根清单（多模块工程的父 pom / 聚合构建）
        _add_dir_manifests(root)
        # 2) 沿已 scope 文件向上回溯到根，收集每级目录的清单（覆盖各子模块）
        scope = self.effective_scope
        scoped = (list(getattr(scope, "readable", []) or [])
                  + list(getattr(scope, "writable", []) or [])
                  + list(getattr(scope, "create_files", []) or []))
        seen_dirs: set[Path] = set()
        for f in scoped:
            try:
                cur = (root / str(f).strip()).resolve().parent
            except (OSError, ValueError):
                continue
            # 向上回溯到 root（含中间各级模块目录）
            while True:
                if cur in seen_dirs:
                    break
                seen_dirs.add(cur)
                if root not in cur.parents and cur != root:
                    break
                _add_dir_manifests(cur)
                if cur == root:
                    break
                cur = cur.parent
        # 3) 多模块工程：聚合父 pom 会引用【所有】子模块，mvn -pl/聚合构建需要全部
        #    模块的构建清单在场。项目级 glob 收集所有清单(限 60 个，防超大 monorepo)。
        #    只收清单文件本身(小)，不碰源码，开销可忽略。
        _SKIP = {".git", "node_modules", "target", "build", ".venv", "venv",
                 "dist", ".gradle", "__pycache__", ".codegraph"}
        manifest_set = set(manifest_names)
        count = 0
        for p in root.rglob("*"):
            if count >= 60:
                break
            if p.name not in manifest_set or not p.is_file():
                continue
            if any(part in _SKIP for part in p.relative_to(root).parts):
                continue
            try:
                rel = str(p.relative_to(root))
            except ValueError:
                continue
            if rel not in found:
                found.append(rel)
                count += 1
        return found


    def _writable_files(self) -> list[str]:
        """pull-back 范围：可修改文件 + 新建文件（都要从沙箱拉回）；不含删除文件。"""
        out: list[str] = []
        scope = self.effective_scope
        for f in list(getattr(scope, "writable", []) or []) + list(getattr(scope, "create_files", []) or []):
            rel = str(f).strip()
            if rel and rel not in out:
                out.append(rel)
        return out

    def _apply_local_deletions(self, local_root: Path, exists_in_sandbox) -> list[str]:
        """A1 治本：把 worker 在沙箱里执行的删除【传播到本地工作树】。

        delete_files 不在 _writable_files（不上传/不拉回），历史上无任何机制把删除落到本地 →
        git diff 永远看不到删除 → 交付漏删 + 纯删除子任务恒空 diff 假绿。判据：scope 声明要删的
        文件，若【沙箱里已不存在】(worker 真删了)且【本地仍存在】→ 本地 unlink，使 git diff 如实
        显示删除；沙箱里仍在 = worker 没删 → 保留本地(diff 空)→ 上游 expects_changes 判未完成。

        exists_in_sandbox(rel)->bool 是【逐文件精确探测】(见 _sandbox_file_exists 的 test -f)。
        ★复核 CR-2 修正：绝不用 head-200 截断的全量列举比对——否则沙箱 >200 文件时位次 201+ 的
          文件虽仍在却被判"已删"→ 误 unlink 数据丢失(RuoYi 数百文件必触发)。
        ★复核 CR-4 修正：unlink 前强制 containment 到 local_root，`..` 越界路径拒删(unlink 不可逆)。
        探测失败保守视为"仍在"(不删)——删除是不可逆方向，宁可漏删触发重试，绝不误删。
        """
        deleted: list[str] = []
        scope = self.effective_scope
        for f in (getattr(scope, "delete_files", []) or []):
            rel = self._norm_rel(local_root, f)
            if not rel:
                continue
            lp = local_root / rel
            # CR-4：containment——解析后必须在 local_root 内，杜绝 `../x` 越界 unlink（A5 归一原语）。
            if not is_within_root(local_root, rel, join=True):
                self._log(f"删除路径越界（不在项目根内），拒删: {rel}")
                continue
            if exists_in_sandbox(rel):
                continue  # 沙箱里还在 → worker 没删 → 保留本地
            try:
                if lp.is_file():
                    lp.unlink()
                    self._deleted_local_paths.add(rel)
                    deleted.append(rel)
            except OSError as exc:
                logger.warning(
                    "删除传播失败 %s（保留本地，需核查权限/占用）: %s", rel, exc, exc_info=True)
        return deleted

    def _sandbox_file_exists(self, rel: str) -> bool:
        """A1(复核 CR-2)：逐文件精确探测沙箱是否仍有该文件(test -f)，替代 head-200 截断全量列举。
        无沙箱/探测失败 → 保守返回 True(视为仍在→不删)，绝不因抖动/截断误删本地文件。"""
        if not self._sandbox or not self._sandbox_manager:
            return True
        rc = getattr(self._sandbox_manager, "run_command", None)
        if rc is None:
            return True
        import shlex
        remote = get_config().sandbox.sandbox_remote_workdir
        # 复核 R23-4：shlex.quote 全路径（不再只剥 '/换行）——文件名含 $()/;/空格等不破坏引号边界。
        _qp = shlex.quote(f"{remote}/{rel}")
        try:
            result = rc(self._sandbox,
                        f"test -f {_qp} && echo __Y__ || echo __N__", timeout=15)
            return "__Y__" in (getattr(result, "stdout", "") or "")
        except Exception:  # noqa: BLE001
            return True  # 探测失败 → 保守不删

    @staticmethod
    def _norm_rel(local_root: Path, f: str) -> str:
        """把 scope 里的文件路径归一化为相对 local_root 的 posix 路径。"""
        p = Path(f)
        if p.is_absolute():
            try:
                return p.resolve().relative_to(local_root).as_posix()
            except ValueError:
                return p.name  # 越界则退化为文件名
        return p.as_posix().lstrip("/")

    def _git_baseline_text(self, local_root: Path, rel: str) -> str | None:
        """从 git HEAD 读取文件的提交版作为 diff 基线（防本地工作副本被前序运行污染）。

        sandbox-first 模式下，前一个相同任务的 pull-back 会把改动写回本地工作副本，
        导致下次运行的 _pre_sync_contents 基线already含该改动 → diff 空 → 误判"无变更"
        → 重试死循环(实测 c592c562 连跑 3 次 diff 均为"(无变更)")。
        用 git HEAD 的提交版做基线，diff 永远相对干净的已提交状态，杜绝污染。
        返回 None 表示无法用 git(非 git 仓库/文件未跟踪)，调用方回退本地工作副本。
        """
        try:
            import subprocess
            proc = subprocess.run(
                ["git", "show", f"{resolve_base_ref(getattr(self, 'base_ref', None))}:{rel}"],
                cwd=str(local_root), capture_output=True, text=True, timeout=15,
            )
            if proc.returncode == 0:
                return proc.stdout
            # 文件在 HEAD 不存在(新建文件)→ 基线为空串
            if "exists on disk, but not in" in proc.stderr or "does not exist" in proc.stderr or "fatal: path" in proc.stderr:
                return ""
            return None
        except Exception:
            return None

    def _snapshot_scope_local(
        self, local_root: Path, files: list[str] | None = None
    ) -> dict[str, str | None]:
        """读取本地文件内容快照，作为 difflib diff 的基线/产出。

        files 为 None 时用 writable scope（diff 只关心可写文件的前后变化）。
        值为文件文本；不存在的文件记为空串；二进制/不可读记为 None。

        基线优先用 git HEAD 提交版(防前序运行 pull-back 污染本地工作副本)；
        git 不可用时回退本地工作副本。仅在 baseline 模式(files is None)下用 git。
        """
        use_git_baseline = files is None  # 只有基线快照需要防污染；产出快照读真实本地
        rel_files = [
            self._norm_rel(local_root, f)
            for f in (files if files is not None else self._writable_files())
        ]
        snapshot: dict[str, str | None] = {}
        for rel in rel_files:
            lp = local_root / rel
            if use_git_baseline:
                git_text = self._git_baseline_text(local_root, rel)
                if git_text is not None:
                    snapshot[rel] = git_text
                    continue
            try:
                snapshot[rel] = lp.read_text("utf-8") if lp.is_file() else ""
            except (UnicodeDecodeError, OSError):
                snapshot[rel] = None
        return snapshot

    def _reset_scope_to_head(self) -> int:
        """批次2-B（Bug：跨任务/重试 workspace 累积脏）：子任务起点把本 scope 内的
        git【跟踪】文件 reset 到 HEAD，杜绝上一轮 pull-back 写回的改动累积叠加。

        - 只 reset writable ∪ scope 内【被 git 跟踪】的文件（git ls-files 白名单）；
          untracked / 新建产物（create_files 尚未提交）一律不碰，零误删风险。
        - per-project 文件锁（fcntl.flock）串行化：并发子任务共享同一 project_path 时，
          同一时刻只有一个 executor 在 reset（dispatch 用 asyncio.gather 真并发，
          scope 可能重叠，见 task 0f93f1fc）。reset 是毫秒级 git 操作，串行无实质开销。
        - SWARM_WORKER_RESET_SCOPE=false 可回退旧行为（默认 true）。
        - 非 git 仓库 / 无 project_path 优雅跳过，返回 0。

        返回被 reset 的文件数。
        """
        import subprocess

        if os.environ.get("SWARM_WORKER_RESET_SCOPE", "true").lower() in ("false", "0", "no"):
            return 0
        if not self.project_path:
            return 0
        local_root = Path(self.project_path).resolve()
        if not (local_root / ".git").exists():
            return 0

        # 候选：仅本子任务【会写】的文件（writable ∪ create_files；只 reset 已被 git 跟踪者）。
        # 根因修复(69d34b1b)：【不再 reset readable / 构建清单文件】——它们本子任务不写，却可能
        # 含【上游子任务的产物】(脚手架建的模块 pom、注册了新模块的父 pom)。把这些 reset 到 HEAD
        # 会抹掉上游改动 → 本子任务沙箱缺依赖 → `mvn -pl <module>` 报 reactor not found（实测）。
        candidates = set()
        for f in self._writable_files():
            candidates.add(self._norm_rel(local_root, f))
        if not candidates:
            return 0

        # 只保留 git 跟踪的文件（ls-files --error-unmatch 逐个判定）
        tracked = []
        for rel in candidates:
            try:
                r = subprocess.run(
                    ["git", "ls-files", "--error-unmatch", rel],
                    cwd=str(local_root), capture_output=True, text=True, timeout=10,
                )
                if r.returncode == 0:
                    tracked.append(rel)
            except Exception:  # noqa: BLE001
                continue
        if not tracked:
            return 0

        # TD2606-B5/C5：reset 与 diff/add-N 共用同一 per-project 锁（_ProjectGitFlock），
        # 串行化所有 git 临界操作，杜绝并发 worker 在共享工作树/索引上互踩。
        try:
            with _ProjectGitFlock(local_root):
                r = subprocess.run(
                    ["git", "checkout", resolve_base_ref(getattr(self, 'base_ref', None)), "--", *tracked],
                    cwd=str(local_root), capture_output=True, text=True, timeout=30,
                )
            if r.returncode == 0:
                self._log(f"bootstrap 前 workspace reset：{len(tracked)} 个 tracked 文件恢复到钉扎 base（防跨轮脏叠加）")
                return len(tracked)
            self._log(f"workspace reset 警告（git checkout 非零）: {r.stderr.strip()[:200]}")
            return 0
        except Exception as exc:  # noqa: BLE001
            self._log(f"workspace reset 跳过（异常）: {exc}")
            return 0

    async def _sync_to_sandbox(self, reason: str) -> None:
        """精准上传：只把子任务 scope 内的文件推送到沙箱 /workspace。

        同时保存上传前内容快照 self._pre_sync_contents，供 difflib 生成 diff。
        本地执行模式（无沙箱）下仅记录本地快照作为 diff 基线。
        """
        local_root = Path(self.project_path).resolve()
        self._pre_sync_contents = self._snapshot_scope_local(local_root)
        if not self._sandbox or not self._sandbox_manager:
            return
        cfg = get_config()

        # 上传范围：
        # - 项目专属沙箱（镜像自带完整源码，方案 B）→ 只传被改的 writable/create_files；
        #   readable 镜像已有，传了反而可能用本地覆盖镜像基线（且浪费 I/O）。
        # - 通用池沙箱（/workspace 空）→ 传完整 scope_files（readable ∪ writable ∪ 构建清单），
        #   否则编译找不到依赖源文件/pom。
        if getattr(self, "_sandbox_has_source", False):
            rel_files = [self._norm_rel(local_root, f) for f in self._writable_files()]
            # 根因修复(69d34b1b)：自带源码模式默认不传 readable（baked 镜像=git HEAD 已有）。
            # 但【上游子任务改过/新建的文件】(脚手架建的模块 pom、注册了模块的父 pom)在本依赖
            # 子任务里常列为 readable，其本地内容 ≠ git HEAD（镜像里是旧版/没有）→ 不补传则本
            # 子任务沙箱看不到上游产物 → `mvn -pl <module>` 报 reactor not found。
            # 判据：readable 文件【本地存在】且【本地内容 ≠ git HEAD 版】= 被上游改动 → 补传。
            _seen = set(rel_files)
            _extra: list[str] = []
            if (local_root / ".git").exists():
                import subprocess as _sp
                for f in (getattr(self.effective_scope, "readable", []) or []):
                    rel = self._norm_rel(local_root, f)
                    if rel in _seen:
                        continue
                    abs_p = local_root / rel
                    if not abs_p.is_file():
                        continue
                    try:
                        in_head = _sp.run(
                            ["git", "cat-file", "-e", f"{resolve_base_ref(getattr(self, 'base_ref', None))}:{rel}"],
                            cwd=str(local_root), capture_output=True, timeout=10,
                        ).returncode == 0
                    except Exception:  # noqa: BLE001
                        continue
                    if not in_head:
                        # 上游新建（base 无、本地有，如脚手架建的模块 pom）→ 补传
                        rel_files.append(rel)
                        _seen.add(rel)
                        _extra.append(rel)
                        continue
                    # 在 HEAD：比对内容，本地 ≠ HEAD = 上游改动（如父 pom 注册了模块）→ 补传
                    head_text = self._git_baseline_text(local_root, rel)
                    if head_text is None:
                        continue
                    try:
                        local_text = abs_p.read_text(encoding="utf-8")
                    except (UnicodeDecodeError, OSError):
                        continue
                    if local_text != head_text:
                        rel_files.append(rel)
                        _seen.add(rel)
                        _extra.append(rel)
                # FINDING-11(task 0847c303)：build-critical 清单(root/模块 pom、settings/build.gradle)
                # 任何 `mvn -pl`/reactor 构建都隐式依赖父 pom 的 <modules> 注册，但这些文件常【不在本
                # 子任务 scope】(上面 readable 循环漏掉)→ 上游脚手架注册的父 pom 不传到本沙箱 → reactor
                # not found(实测 st-3 跨 replan/retry 全败)。故【始终】补传变更的 build 清单(local≠HEAD)，
                # 不限 scope——是 69d34b1b 修复的泛化(从"传 scope 内变更"扩到"额外始终传 build-critical")。
                _BUILD_MANIFESTS = (
                    "pom.xml", "settings.gradle", "build.gradle",
                    "settings.gradle.kts", "build.gradle.kts",
                )
                try:
                    _ch = _sp.run(
                        ["git", "diff", "--name-only", resolve_base_ref(getattr(self, 'base_ref', None))],
                        cwd=str(local_root), capture_output=True, text=True, timeout=15,
                    ).stdout.splitlines()
                    _ut = _sp.run(
                        ["git", "ls-files", "--others", "--exclude-standard"],
                        cwd=str(local_root), capture_output=True, text=True, timeout=15,
                    ).stdout.splitlines()
                except Exception:  # noqa: BLE001
                    _ch, _ut = [], []
                for rel in (_ch + _ut):
                    rel = (rel or "").strip()
                    if not rel or rel in _seen:
                        continue
                    if rel.rsplit("/", 1)[-1] not in _BUILD_MANIFESTS:
                        continue
                    if not (local_root / rel).is_file():
                        continue
                    rel_files.append(rel)
                    _seen.add(rel)
                    _extra.append(rel)
            if _extra:
                self._log(
                    f"{reason} 自带源码：补传 {len(_extra)} 个上游产物(本地≠HEAD，如模块/父 pom): {_extra[:5]}"
                )
            self._log(
                f"{reason} 专属沙箱自带源码 → 上传 {len(rel_files)} 个文件（改动 + 上游产物）"
            )
        else:
            rel_files = [self._norm_rel(local_root, f) for f in self._scope_files()]
        if not rel_files:
            self._log(f"{reason} scope 为空，跳过文件上传（无目标文件）")
            return

        # 记录上传前内容快照（用于 diff 基线）。writable 文件的基线已由
        # _snapshot_scope_local 用 git HEAD 填好(防污染)，这里【不覆盖】已有键，
        # 只为额外的 scope 文件(readable/构建清单)补基线，且同样优先 git。
        for rel in rel_files:
            if rel in self._pre_sync_contents:
                continue  # 已有 git 基线，绝不用本地工作副本覆盖(否则前序污染复现)
            git_text = self._git_baseline_text(local_root, rel)
            if git_text is not None:
                self._pre_sync_contents[rel] = git_text
                continue
            lp = (local_root / rel)
            try:
                self._pre_sync_contents[rel] = lp.read_text("utf-8") if lp.is_file() else ""
            except (UnicodeDecodeError, OSError):
                self._pre_sync_contents[rel] = None  # 二进制/不可读

        # ── 批次2-A（Bug：跨重试改动叠加）：上传 git HEAD 内容而非脏磁盘 ──
        # 根因：上一个 executor 的 pull-back 把改动写回本地 project_path，重新派发时
        # bootstrap 上传脏文件 → LLM 在脏版本上叠加修改（docstring 重复等）。
        # _git_baseline_text 此前只兜住了 diff 基线，没兜上传内容（半个修复）。
        # 这里把 writable(modify) 中【git 跟踪】的文件改用 HEAD 版写入临时 staging
        # 目录上传；untracked/新建/readable 仍从真实磁盘上传（HEAD 取不到）。
        # SWARM_WORKER_CLEAN_UPLOAD=false 可回退旧行为（默认 true）。
        import shutil
        import tempfile

        clean_upload = os.environ.get(
            "SWARM_WORKER_CLEAN_UPLOAD", "true"
        ).lower() not in ("false", "0", "no")
        writable_set = {self._norm_rel(local_root, f) for f in self._writable_files()}
        upload_root = local_root
        staging_dir: str | None = None
        if clean_upload:
            import subprocess as _sp
            try:
                staging_dir = tempfile.mkdtemp(prefix="swarm_clean_upload_")
                staging_root = Path(staging_dir)
                cleaned = 0
                for rel in rel_files:
                    dst = staging_root / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    # 仅当 rel 是 writable 且【确实被 git 跟踪】时用 HEAD 版。
                    # 用 ls-files 显式判定，区分 "tracked"（含空文件）与 "untracked/新建"
                    # （_git_baseline_text 对两者都返回 ""，无法区分 → 会把新建文件写空）。
                    is_tracked = False
                    if rel in writable_set:
                        try:
                            _r = _sp.run(
                                ["git", "ls-files", "--error-unmatch", rel],
                                cwd=str(local_root), capture_output=True, text=True, timeout=10,
                            )
                            is_tracked = _r.returncode == 0
                        except Exception:  # noqa: BLE001
                            is_tracked = False
                    git_text = self._git_baseline_text(local_root, rel) if is_tracked else None
                    if git_text is not None:
                        # writable 且 git 跟踪 → 用 HEAD 干净版（杜绝脏磁盘叠加）
                        dst.write_text(git_text, encoding="utf-8")
                        cleaned += 1
                    else:
                        # readable / untracked / 新建 → copy 真实磁盘（HEAD 无此版）
                        src = local_root / rel
                        if src.is_file():
                            shutil.copy2(src, dst)
                        # 源不存在（待新建）→ staging 也不建，上传层跳过
                upload_root = staging_root
                if cleaned:
                    self._log(f"{reason} 干净上传：{cleaned} 个 writable 文件用 git HEAD 版上传（防脏叠加）")
            except Exception as stage_exc:  # noqa: BLE001
                self._log(f"{reason} staging 构造失败，回退脏磁盘上传: {stage_exc}")
                upload_root = local_root
                if staging_dir:
                    shutil.rmtree(staging_dir, ignore_errors=True)
                    staging_dir = None

        try:
            sync_stats = await asyncio.to_thread(
                self._sandbox_manager.sync_files_to_sandbox,
                self._sandbox,
                upload_root,
                rel_files,
                cfg.sandbox.sandbox_remote_workdir,
            )
            err_count = len(sync_stats.get("errors") or [])
            self._log(
                f"{reason} 本地→沙箱精准上传: "
                f"uploaded={sync_stats.get('uploaded', 0)}, "
                f"errors={err_count}, files={sync_stats.get('files')}"
            )
            for err in (sync_stats.get("errors") or [])[:5]:
                self._log(f"上传警告: {err}")
        except Exception as sync_exc:
            # N-06：bootstrap 上传失败若吞掉，agent 会对【缺文件的沙箱】空跑→被误判能力失败
            # （空 diff）→错误触发换模型。这是基础设施瞬时失败，显式抛 TransientInfraError →
            # run() 归类 transient → 退避重试同模型（自愈）。
            self._log(f"{reason} 本地→沙箱精准上传失败（infra 瞬时，将退避重试）: {sync_exc}")
            raise TransientInfraError(
                f"sandbox upload failed ({reason}): {sync_exc}"
            ) from sync_exc
        finally:
            if staging_dir:
                shutil.rmtree(staging_dir, ignore_errors=True)

    async def _sync_from_sandbox(self, reason: str) -> None:
        """精准拉回：只把子任务可写文件从沙箱拉回本地 project_path。

        拉回内容存入 self._post_sync_contents，供 difflib 生成 diff。
        本地执行模式（无沙箱）下读取本地 writable 文件当前内容作为产出快照
        （agent 已直接改本地文件），从而本地模式也能正确产出 diff。
        """
        local_root = Path(self.project_path).resolve()
        # TD2606-C9：闸门在沙箱里确定性修复的文件（含 scope 外，如父 pom）也要回传。
        extra_repaired = sorted(self._repaired_extra_paths)
        if not self._sandbox or not self._sandbox_manager:
            # 本地模式：直接快照本地 writable 文件（agent 已就地修改）+ 被修复文件
            self._post_sync_contents = self._snapshot_scope_local(
                local_root, files=self._writable_files() + extra_repaired
            )
            await self._normalize_jvm_namespace(local_root, reason)
            return
        self._post_sync_contents = {}
        cfg = get_config()
        rel_files = [self._norm_rel(local_root, f) for f in self._writable_files()]
        # greenfield/allow_any 模式：scope 没有预设文件，worker 自由创建。
        # 列出沙箱 workspace 实际文件作为 pull-back 清单，否则新建文件拉不回来。
        if not rel_files and getattr(self.effective_scope, "allow_any", False):
            try:
                rel_files = await asyncio.to_thread(self._list_sandbox_workspace_files)
                self._log(f"{reason} allow_any 模式：枚举沙箱产物 {len(rel_files)} 个文件")
            except Exception as exc:
                self._log(f"{reason} allow_any 枚举沙箱文件失败: {exc}")
        # 并入被确定性修复的文件（去重保序），使其无论是否在写权 scope 内都被拉回本地。
        if extra_repaired:
            rel_files = list(dict.fromkeys(
                rel_files + [self._norm_rel(local_root, p) for p in extra_repaired]
            ))
        # ★H-exec1 治本(round21 假绿门)★：worker 常自建【未声明】的同包 helper/config/枚举/内部类——
        # 在沙箱编过→L1 绿，但只回传【声明 scope】会漏掉它们→本地树缺→MERGE/集成期 cannot find symbol
        # (L1 假绿+产物不落盘)。故在【声明文件的父目录】下按源扩展名枚举沙箱里【本地尚无】的新文件，
        # 纳入回传。有界(仅子任务自己的包目录 + 只补本地缺失文件，不拉全仓/构建产物/不碰既有文件)。
        if rel_files and not getattr(self.effective_scope, "allow_any", False):
            try:
                _decl_dirs = {
                    str(Path(f).parent).replace("\\", "/")
                    for f in rel_files if "/" in f.replace("\\", "/")
                }
                if _decl_dirs:
                    _all_sb = await asyncio.to_thread(self._list_sandbox_workspace_files)
                    _SRC_EXT = (".java", ".kt", ".kts", ".go", ".rs", ".ts", ".tsx",
                                ".js", ".jsx", ".vue", ".py", ".xml", ".sql", ".proto")
                    _rel_set = set(rel_files)
                    _extra_new = [
                        f for f in _all_sb
                        if f not in _rel_set
                        and f.lower().endswith(_SRC_EXT)
                        and any(f.replace("\\", "/").startswith(d + "/") for d in _decl_dirs)
                        and not (local_root / f).exists()  # 只补本地【尚无】的新文件，不碰既有
                    ]
                    if _extra_new:
                        rel_files = list(dict.fromkeys(rel_files + _extra_new))
                        self._log(
                            f"{reason} H-exec1：纳入 {len(_extra_new)} 个未声明沙箱新建源文件"
                            f"(同包，防 L1 绿但产物不落盘): {_extra_new[:5]}"
                        )
            except Exception as _hexc:  # noqa: BLE001
                self._log(f"{reason} H-exec1 枚举沙箱新增文件失败(非致命): {_hexc}")
        # A1：删除传播——必须在 rel_files 空的 early-return 之前，纯删除 scope 才不被跳过。
        # 复核 CR-2 修正：逐文件 test -f 精确探测(不再 head-200 截断全量列举比对，杜绝误删)。
        if getattr(self.effective_scope, "delete_files", []):
            try:
                _deleted = await asyncio.to_thread(
                    self._apply_local_deletions, local_root, self._sandbox_file_exists)
                if _deleted:
                    self._log(f"{reason} 删除传播：worker 已在沙箱删除 → 本地同步删除 {_deleted}")
            except Exception as _dexc:  # noqa: BLE001
                self._log(f"{reason} 删除传播失败（非致命）: {_dexc}")
        if not rel_files:
            self._log(f"{reason} 无可写文件，跳过 pull-back")
            return
        try:
            sync_stats = await asyncio.to_thread(
                self._sandbox_manager.sync_files_from_sandbox,
                self._sandbox,
                local_root,
                rel_files,
                cfg.sandbox.sandbox_remote_workdir,
            )
            self._post_sync_contents = sync_stats.get("contents") or {}
            # A3：记录本轮 pull-back 完整性信号（skip/err），供 L1 闸门 fail-closed。
            self._sync_skipped_count = int(sync_stats.get("skipped") or 0)
            self._sync_error_rels = list(sync_stats.get("errors") or [])
            err_count = len(sync_stats.get("errors") or [])
            self._log(
                f"{reason} 沙箱→本地精准 pull-back: "
                f"downloaded={sync_stats.get('downloaded', 0)}, "
                f"errors={err_count}"
            )
            for err in (sync_stats.get("errors") or [])[:5]:
                self._log(f"pull-back 警告: {err}")
            await self._normalize_jvm_namespace(local_root, reason)
        except Exception as sync_exc:
            # N-07：pull-back 失败若吞掉，成功执行的产出拉不回来→diff 空→报"无变更"→
            # 错误触发换模型降级。这是基础设施瞬时失败，显式抛 → run() 归类 transient → 退避重试。
            self._log(f"{reason} 沙箱→本地 pull-back 失败（infra 瞬时，将退避重试）: {sync_exc}")
            raise TransientInfraError(
                f"sandbox pull-back failed ({reason}): {sync_exc}"
            ) from sync_exc

    async def _normalize_jvm_namespace(self, local_root: Path, reason: str) -> None:
        """确定性 Jakarta/Javax 命名空间归一（治本：短路模型复读死循环）。

        worker 写代码后 pull-back 到本地，这里据 project_stack 的权威命名空间把改动文件里
        【写错的】Jakarta EE 包前缀（如本项目用 jakarta 却写成 javax.servlet）确定性改对，
        并把改过的文件【回写本地 + 重新上传沙箱】，使随后的 L1 build 闸门在沙箱里直接编过，
        不再让本地小模型对着 `package javax.servlet does not exist` 空转到迭代上限。
        - 仅当 project_stack.jvm.servlet_namespace ∈ {jakarta,javax} 时生效；非 JVM/未判明→no-op。
        - 只动 .java 文件、只改整包迁移的 Jakarta EE 前缀（见 rewrite_jvm_namespace），JDK 自带
          的 javax.*（sql/crypto/naming…）一律不碰。SWARM_WORKER_JVM_NS_FIX=false 可关。
        """
        if os.environ.get("SWARM_WORKER_JVM_NS_FIX", "true").lower() in ("false", "0", "no"):
            return
        contents = getattr(self, "_post_sync_contents", None)
        if not contents:
            return
        profile = self._resolve_project_stack() or {}
        target_ns = (profile.get("jvm") or {}).get("servlet_namespace")
        if target_ns not in ("jakarta", "javax"):
            return
        from swarm.worker.l1_pipeline import rewrite_jvm_namespace

        fixed: dict[str, int] = {}
        for rel, text in list(contents.items()):
            if not rel.endswith(".java") or not isinstance(text, str):
                continue
            new_text, n = rewrite_jvm_namespace(text, target_ns)
            if n <= 0:
                continue
            other = "javax" if target_ns == "jakarta" else "jakarta"
            # 回写本地（diff 源）+ 更新快照
            try:
                lp = (local_root / rel)
                lp.parent.mkdir(parents=True, exist_ok=True)
                data = new_text.encode("utf-8")
                data = self._sandbox_manager._preserve_line_endings(lp, data) \
                    if self._sandbox_manager else data
                lp.write_bytes(data)
                contents[rel] = new_text
                fixed[rel] = n
            except OSError as exc:
                self._log(f"{reason} 命名空间归一回写本地失败 {rel}: {exc}")
        if not fixed:
            return
        self._log(
            f"{reason} 命名空间确定性归一（→{target_ns}.*，治本短路死循环）："
            + ", ".join(f"{r}×{c}" for r, c in list(fixed.items())[:5])
            + (f" 等 {len(fixed)} 文件" if len(fixed) > 5 else "")
        )
        # 沙箱模式：把改对的文件重新上传，使 L1 build 在沙箱里见到 jakarta 版
        if self._sandbox and self._sandbox_manager:
            try:
                cfg = get_config()
                await asyncio.to_thread(
                    self._sandbox_manager.sync_files_to_sandbox,
                    self._sandbox,
                    local_root,
                    list(fixed.keys()),
                    cfg.sandbox.sandbox_remote_workdir,
                )
            except Exception as exc:  # noqa: BLE001
                self._log(f"{reason} 命名空间归一回传沙箱失败（不致命，build 闸门会暴露）: {exc}")

    def _list_sandbox_workspace_files(self) -> list[str]:
        """递归列出沙箱 /workspace 下的相对文件路径（allow_any/greenfield pull-back 用）。

        走 shell 端点(run_command + find)——不依赖 Jupyter kernel(自建语言镜像无
        kernel 会 502)。过滤常见噪声目录，返回相对 remote_workdir 的路径(上限 200)。
        """
        if not self._sandbox or not self._sandbox_manager:
            return []
        cfg = get_config()
        remote = cfg.sandbox.sandbox_remote_workdir
        # find 排除噪声目录 + 限 2MB；-printf 输出相对路径
        prune = r"\( -name .git -o -name __pycache__ -o -name node_modules -o -name .venv -o -name venv -o -name .codegraph -o -name dist -o -name build -o -name .pytest_cache \)"
        cmd = (
            f"cd {remote} 2>/dev/null && "
            f"find . {prune} -prune -o -type f -size -2000k -print 2>/dev/null "
            f"| sed 's|^\\./||' | head -200"
        )
        rc = getattr(self._sandbox_manager, "run_command", None)
        if rc is None:
            return []
        result = rc(self._sandbox, cmd, timeout=30)
        if getattr(result, "error", None) and not getattr(result, "stdout", ""):
            return []
        out = (result.stdout or "").strip()
        if not out:
            return []
        return [line.strip() for line in out.splitlines() if line.strip()]

    def _get_git_diff(self) -> str:
        """生成子任务改动的 unified diff。

        ── 优先用【本地 git diff】(task 1a49aa66 治本)──
        diff 在【本地】生成(worker 在沙箱改文件→pull-back 写回本地→在此比对)。
        若本地 project_path 是 git 仓库(本机开发的真实项目几乎都是)，直接用 `git diff`
        生成——它与 git apply 同源，产出的补丁【必被 git apply 接受】，从根上消除 difflib
        手工拼 unified diff 的格式错乱(hunk 行数/前导符错位→"补丁损坏")。
        仅当无 git 仓库(greenfield/无 .git)时回退到 difflib(已修正 keepends/lineterm 用法)。

        基线 = HEAD（项目模板/本地工作区的干净基线）；新值 = pull-back 后的工作区当前内容。
        """
        # ── 路径1：本地 git 仓库 → git diff（治本，必被 git apply 接受）──
        git_diff = self._try_local_git_diff()
        if git_diff is not None:
            return git_diff if git_diff.strip() else "(无变更)"

        # ── 路径2：difflib fallback（无 git 仓库时）──
        import difflib

        pre = getattr(self, "_pre_sync_contents", None) or {}
        post = getattr(self, "_post_sync_contents", None) or {}

        if not post:
            return "(无变更)"

        diff_parts: list[str] = []
        for rel in sorted(post.keys()):
            new_text = post.get(rel)
            old_text = pre.get(rel, "")
            # 二进制文件
            if new_text is None or old_text is None:
                if new_text != old_text:
                    diff_parts.append(f"二进制文件变更: {rel}")
                continue
            # 行尾归一化：基线(git HEAD/本地)可能是 LF，pull-back 回来可能是 CRLF
            # (RuoYi 原始文件即 Windows CRLF)。不归一会让 difflib 把每行都判为变更，
            # 产出整文件 churn 的垃圾 diff(实测 StringUtils 862 行全变 44KB)，淹没真实改动。
            old_norm = old_text.replace("\r\n", "\n").replace("\r", "\n")
            new_norm = new_text.replace("\r\n", "\n").replace("\r", "\n")
            if old_norm == new_norm:
                continue
            # ── 关键修复(task 1a49aa66)：difflib unified_diff 的正确用法 ──
            # 实测唯一能被 git apply 接受的组合：splitlines(keepends=True)[内容行自带\n] +
            # lineterm=""[difflib 不给 hunk头/文件头加换行] + 逐元素规范化补换行 + "".join。
            # 旧代码 keepends=True + lineterm="" + "\n".join 会给本已含\n的内容行再加\n（行尾翻倍）；
            # 而 keepends=False + lineterm="\n" 会让内容行【没有】换行符（全挤一行）。两者都让
            # git apply 报"补丁损坏"。下面的 normalize 方案兼顾：内容行用自带\n，头部行补\n。
            old_lines = old_norm.splitlines(keepends=True)
            new_lines = new_norm.splitlines(keepends=True)
            ud = difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                lineterm="",
            )
            # 逐元素规范化：hunk头/文件头(lineterm="" 故无换行)补\n；内容行(keepends 已含\n)不动。
            block = "".join(x if x.endswith("\n") else x + "\n" for x in ud)
            block = block.rstrip("\n")
            if block.strip():
                diff_parts.append(block)

        if not diff_parts:
            return "(无变更)"
        return "\n".join(diff_parts)

    def _try_local_git_diff(self) -> str | None:
        """本地 git 仓库 → 用 git diff 生成子任务 scope 文件的 unified diff。

        返回 None 表示不可用（无 project_path / 非 git 仓库 / git 调用失败）→ 上层回退 difflib。
        返回 "" 或 diff 文本表示成功（""=无变更）。

        关键：worker 已把沙箱改动 pull-back 写回本地工作区，所以工作区当前内容就是改动后状态，
        git diff 基线为 HEAD。只 diff 子任务 scope 文件（writable∪create∪delete），避免把
        .codegraph/ 等无关变更带进来。新建文件用 `git diff --no-index /dev/null <file>` 或
        `git add -N` 让其出现在 diff 中。
        """
        import subprocess as _sp
        from pathlib import Path as _P

        root = getattr(self, "project_path", None)
        if not root:
            return None
        root = str(_P(root).resolve())
        if not (_P(root) / ".git").exists():
            return None

        scope = self.effective_scope
        # scope 内所有受影响文件（相对路径）
        modify = [f for f in (getattr(scope, "writable", []) or []) if f]
        create = [f for f in (getattr(scope, "create_files", []) or []) if f]
        delete = [f for f in (getattr(scope, "delete_files", []) or []) if f]
        # TD2606-C9：把闸门在沙箱里修复的文件（含 scope 外，如父 pom）纳入 diff，
        # 否则修复进了本地工作区却因不在 scope 而被 `-- <files>` 过滤掉 → merged_diff 缺失。
        repaired = [f for f in sorted(self._repaired_extra_paths) if f]
        targets = list(dict.fromkeys(modify + create + delete + repaired))
        if not targets:
            return None

        try:
            # 让新建/未跟踪文件也能进 git diff：对 create_files 做 intent-to-add（-N，不暂存内容，
            # 仅登记路径，使 git diff 能显示其全部新增行）。幂等、无副作用（不真正 commit）。
            untracked = []
            for f in targets:
                p = _P(root) / f
                if p.is_file():
                    # 是否已跟踪
                    r = _sp.run(["git", "-C", root, "ls-files", "--error-unmatch", f],
                                capture_output=True, text=True)
                    if r.returncode != 0:
                        untracked.append(f)
            # TD2606-B5/C5/M5：add -N（改共享 index）+ diff 必须在同一 per-project 锁内原子完成，
            # 否则并发 worker 的 intent-to-add 互相泄漏进对方 diff、与他人 reset/diff 互踩。
            # 锁内只放这两条短命 git 命令；ls-files 探测（只读）与 diff 结果处理在锁外。
            with _ProjectGitFlock(root):
                # ── 主干A 治本（并行子任务共享聚合态）──
                # 根因：pull-back 把产物写回【共享】project_path 工作区（一任务一份，N 个并行
                # worker 共用），而本路径取"工作区当前内容"作 diff 新值。多写者对同一聚合文件
                # （根 pom / settings.gradle / Cargo.toml…）last-write-wins 互相覆盖 → 谁后写、
                # 谁的内容进了别人的 diff，被覆盖者的 +<module>/+<dependency> 直接从 diff 丢失，
                # 下游 MERGE 并集（Lever A）也救不回【从未被任何 diff 捕获】的成员。
                # 不变量：worker 的 diff 必须是 (HEAD, 本 worker 自己 pull-back 的产出) 的纯函数，
                # 与其他 worker 无关。锁内先用本 worker 的 _post_sync_contents 把自己的 scope 文件
                # 重置回自己的产出，再 diff——把 diff 对【长生命周期共享工作区】的依赖切断，concurrent
                # 写者无法在"重置→diff"这段持锁原子区内插进来。仅重置本 subtask owns 的 targets，
                # 不碰他人文件；二进制(None)/删除(缺键)/未产出则保留工作区现状（退化为原行为）。
                _own = getattr(self, "_post_sync_contents", None) or {}
                for _f in targets:
                    _txt = _own.get(_f)
                    if not isinstance(_txt, str):
                        continue
                    try:
                        _lp = _P(root) / _f
                        _lp.parent.mkdir(parents=True, exist_ok=True)
                        # _txt 来自 _post_sync_contents：其字节在 pull-back 时已过 _preserve_line_endings
                        # （与本地/HEAD 同行尾），decode 成字符串后行尾已正确，直接 encode 写回即同源。
                        # 【不再】二次对磁盘采样判 CRLF——持锁前磁盘可能是别的 worker 的覆盖版，采它会
                        # 误判行尾、给本 worker 的 diff 引入伪 CRLF 噪声（评审 MEDIUM，治本：不依赖共享磁盘）。
                        _lp.write_bytes(_txt.encode("utf-8"))
                    except OSError as _wexc:
                        self._log(f"主干A 自产出重置落盘失败 {_f}（退化读工作区现状）: {_wexc}")
                if untracked:
                    _sp.run(["git", "-C", root, "add", "-N", *untracked],
                            capture_output=True, text=True, timeout=30)
                r = _sp.run(
                    ["git", "-C", root, "diff", "--no-color",
                     resolve_base_ref(getattr(self, 'base_ref', None)), "--", *targets],
                    capture_output=True, timeout=60,  # 注意：不传 text=True，拿原始 bytes
                )

            # 生成 diff：钉扎 base 基线 vs 工作区当前（含 pull-back 的改动 + -N 的新文件）。
            # 3rd#2：显式相对 base（None→HEAD），与 merge base_reader 同源对齐，消除运行期 HEAD 漂移。
            # --no-color 防 ANSI；-- <files> 限定 scope，不带入无关变更。
            # 行尾一致性由 pull-back 的 _preserve_line_endings 保证（CRLF 项目写回仍 CRLF），
            # 工作区与 git HEAD 同行尾 → git diff 不会全文 churn、产出的 context 行带正确行尾，
            # git apply 同源必成功。故【不再用 --ignore-cr-at-eol】(那会产 LF context 反而和
            # CRLF 的 HEAD 对不上，task f20ea68d 实测 git apply --ignore-whitespace 都救不了)。
            # 【关键(task f20ea68d 根因)】用 bytes 模式读 git diff，不能用 text=True！
            # text=True 触发 Python universal-newlines，会把 git diff 输出里 CRLF 文件的
            # context 行尾 \r\n 静默转成 \n → diff 丢失 \r → git apply 回 CRLF 的 HEAD 时
            # context 字节不匹配（实测 --ignore-whitespace/--3way 都救不了）。bytes 模式
            # 保留 \r，产出与 CRLF 源文件完全同源的 diff，git apply 直接成功（无需任何忽略参数）。
            # （diff 已在上方 _ProjectGitFlock 锁内执行，结果即 r。）
            if r.returncode != 0:
                _err = (r.stderr or b"").decode("utf-8", "replace")
                self._log(f"git diff 失败(rc={r.returncode})，回退 difflib: {_err[:120]}")
                return None
            # 解码保留行尾：用 decode 不做 newline 转换（bytes→str 不触发 universal newlines）
            diff = (r.stdout or b"").decode("utf-8", "replace")
            # 删除文件：git diff 已能体现（工作区文件被删 → diff 显示删除）。
            self._log(f"diff 来源: 本地 git diff（{len(targets)} 个 scope 文件，行尾同源，git apply 直通）")
            # 仅去掉【整个 diff 末尾】的多余空行，不碰行内 \r（rstrip 只删尾部 \n，\r 在行内不受影响）
            return diff.rstrip("\n")
        except Exception as e:  # noqa: BLE001
            self._log(f"git diff 异常({str(e)[:80]})，回退 difflib")
            return None
