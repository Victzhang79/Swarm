"""Worker L1 确定性闸门 / 结果解析混入 —— 从 worker/executor.py 抽出（round26 god-file 治理 Step3）。

L1GATE 连通分量（6 方法）：确定性 L1 闸门（_deterministic_l1_gate：跑 run_l1_pipeline 得
compile/lint/test/scope 真结果）、LLM 自报解析（_parse_l1_result）、失败签名归一
（_failure_signature，去 ANSI/行列号/路径做 no-progress 早停判重）、修复产物 provenance
记录（_record_repaired_paths）、失败测试闸门（_run_failing_test_gate）、Phase4 产出解析
（_parse_produce_result → WorkerOutput）。

以【混入类】外置（同 _SandboxSyncMixin/_PromptBuildingMixin）：方法读写 self 的
_pre_sync_contents/_repaired_extra_paths/_sync_*/execution_log/effective_scope 等（均由
WorkerExecutor.__init__ 初始化），跨簇调用 self._get_git_diff/_git_baseline_text（SYNC mixin）、
self._resolve_project_stack/_check_timeout/_log（核心类）全靠 composed 实例 MRO 解析，本 mixin
不持有它们。禁 eager import worker.executor（防 A6 环）——deps 直接从源模块导；
run_l1_pipeline/_run_l1_command/compress_tool_output/hashlib 保持方法内 lazy import。
"""

from __future__ import annotations

import logging
import os
import re

from pathlib import Path

from swarm.types import Confidence, NotRunKind, WorkerOutput
from swarm.worker.l1_verdict import _is_refusal_or_truncated

logger = logging.getLogger(__name__)


class _L1GateMixin:
    """WorkerExecutor 的 L1 确定性闸门 / 结果解析方法簇（见模块 docstring）。不持有自身状态。"""

    def _parse_l1_result(self, verify_result: str) -> tuple[bool, dict]:
        """解析 LLM 自报的 L1 验证结果（弱信号，仅作辅助）。

        注意：LLM 自报易误判（幻觉 PASS / 中文措辞歧义），真正的权威是
        Phase 3 循环内的确定性 pipeline（见 _deterministic_l1_gate）。
        此处仅用更鲁棒的方式提取 LLM 的自报信号。
        """
        import re

        text = verify_result or ""

        # P1-2：识别模型拒答/截断响应（复用模块级 _is_refusal_or_truncated，
        # 与 trivial / Phase3 硬否决同一事实源）。这类不是真正的验证结论，
        # 标记为 unavailable，明确区分"模型没给出有效自报"与"模型报告失败"。
        llm_unavailable = _is_refusal_or_truncated(text)
        if llm_unavailable:
            details: dict = {
                "raw_result": "(模型拒答/截断，非有效验证自报)",
                "raw_refusal": text[:200],
                "llm_self_report": "unavailable",
                "compile_passed": False,
                "tests_passed": False,
            }
            # 自报不可用 → 保守判 fail（但最终以 deterministic gate 为准）
            return False, details

        # 显式标记优先：L1_RESULT: PASS / FAIL（容忍大小写与空格）
        m = re.search(r"L1_RESULT\s*:?\s*(PASS|FAIL)", text, re.IGNORECASE)
        if m:
            passed = m.group(1).upper() == "PASS"
        else:
            # 无显式标记时保守判定：出现明确失败信号即视为未通过
            low = text.lower()
            has_fail = any(
                kw in low
                for kw in ("fail", "失败", "未通过", "error", "错误", "❌")
            )
            has_pass = any(
                kw in low for kw in ("pass", "通过", "成功", "✅")
            )
            passed = has_pass and not has_fail

        details = {
            "raw_result": text[:500],
            "llm_self_report": "pass" if passed else "fail",
            "compile_passed": bool(re.search(r"编译.*通过|compile.*ok|compiled", text, re.IGNORECASE)),
            "tests_passed": bool(re.search(r"测试.*通过|tests?.*pass", text, re.IGNORECASE)),
        }
        return passed, details

    @staticmethod
    def _failure_signature(l1_details: dict) -> str:
        """P1-D：把确定性闸门的失败归一化成稳定签名，用于跨轮 no-progress 比对。

        取 build/test/compile 输出里的错误行，剥掉行列号/绝对路径/ANSI（这些每轮可能抖动但
        不代表进展），对【去重排序后的错误行集合】求 hash。整组错误一字不变 → 同签名 → 无进展。
        """
        if not isinstance(l1_details, dict):
            return ""
        import hashlib
        blob = "\n".join(
            str(l1_details.get(k) or "")
            for k in ("build_output", "test_output", "compile_message", "reason", "build_failed")
        )
        if not blob.strip():
            return ""
        t = re.sub(r"\x1b\[[0-9;]*m", "", blob)                       # 去 ANSI
        t = re.sub(r":\[\d+,\d+\]", ":[L,C]", t)                      # 去行列号
        t = re.sub(r"(/[^\s:]+)+/", "<path>/", t)                     # 去绝对路径
        # 去掉每轮必抖动但与进展无关的噪声（时长/时间戳/maven 下载进度）
        t = re.sub(r"(?m)^.*(Total time|Finished at|Progress \(\d+\)|"
                   r"Download(ing|ed) from).*$", "", t)
        t = re.sub(r"\s+", " ", t).strip()
        if not t:
            return ""
        return hashlib.md5(t.encode("utf-8")).hexdigest()[:12]

    def _record_repaired_paths(self, details: dict) -> None:
        """TD2606-C9：登记 L1 闸门在沙箱里确定性修复的文件相对路径。

        - 归一化（去 ./ 前缀），累积到 self._repaired_extra_paths；后续每次 _sync_from_sandbox
          都会把它们一并 pull-back，_get_git_diff 也会把它们纳入 diff——即便文件在子任务写权
          scope 之外（典型：父 pom 的版本号被 version-repair 改对）。
        - 同时为【无 .git 回退 difflib】路径补 pre 基线：此刻本地文件尚未被沙箱修复触及
          （修复发生在沙箱），其本地内容即 HEAD 基线，捕获后 difflib 才能算出正确增量。
        """
        paths = details.get("repaired_file_paths") if isinstance(details, dict) else None
        if not paths:
            return
        local_root = Path(self.project_path) if self.project_path else None
        for raw in paths:
            rel = str(raw or "").strip()
            if rel.startswith("./"):
                rel = rel[2:]
            if not rel:
                continue
            self._repaired_extra_paths.add(rel)
            # difflib 基线（仅在尚未捕获时，且仅用于无 .git 回退路径）：优先 git HEAD 提交版
            # （两种执行模式都正确）；无 git 时回退本地工作副本（沙箱模式下此刻本地仍是 HEAD）。
            if local_root is not None and rel not in self._pre_sync_contents:
                git_text = self._git_baseline_text(local_root, rel)
                if git_text is not None:
                    self._pre_sync_contents[rel] = git_text
                else:
                    try:
                        lp = local_root / rel
                        self._pre_sync_contents[rel] = (
                            lp.read_text("utf-8") if lp.is_file() else ""
                        )
                    except (OSError, UnicodeDecodeError):
                        self._pre_sync_contents[rel] = ""

    def _deterministic_l1_gate(self) -> tuple[bool | None, dict]:
        """循环内确定性 L1 闸门：用真实 compile/lint/scope 结果驱动修复轮次。

        借鉴 ECC 的"确定性断言驱动控制循环"经验 —— 不依赖 LLM 自报 PASS，
        而是对当前 git diff 跑确定性 pipeline。返回:
            (None, {...}) 表示无 diff 可检（跳过，交给 LLM 信号）
            (bool, details) 表示确定性结论。
        """
        # A5 治本：worker 总预算闸。确定性 L1 同步路径(含 run_l1_pipeline 的 build-repair
        # 循环，自带 900s 墙钟、与 worker 总预算解耦)过去无任何 _check_timeout——verify 撞
        # max_execution_time 后 Phase4 仍能再起一整轮 repair runaway。已超时 → 不进 pipeline，
        # 降 BLOCKED(交裁决器走退避，run() 随即因超时收尾)。
        if self._check_timeout():
            return None, {"deterministic_gate": "skipped: worker budget exhausted",
                          "not_run_kind": NotRunKind.BLOCKED.value,
                          "error": "timeout_in_verifying"}
        if not self.project_path:
            return None, {"deterministic_gate": "skipped: no project_path",
                          "not_run_kind": NotRunKind.BLOCKED.value}
        try:
            diff = self._get_git_diff()
        except Exception as exc:  # noqa: BLE001
            return None, {"deterministic_gate": f"skipped: diff error {exc}",
                          "not_run_kind": NotRunKind.BLOCKED.value}
        # C2（阶段4）：diff 内容签名——与上次【确定性 PASS】一致则复用结果（pipeline 对
        # 同一 diff 是确定性的；Phase-4 无条件整遍重跑=happy-path 白烧两遍全量构建）。
        import hashlib as _hashlib
        _gate_diff_sig = _hashlib.sha1(
            (diff or "").encode("utf-8", "replace")).hexdigest()
        # 4.9 复核 T3（CONFIRMED·HIGH）：命中还须【本次同步计数干净】——D30/A3 的
        # fail-closed 闸读的是 live 计数器（oversize/skip/err 反映最近一次 pull-back），
        # 缓存只证明缓存那一刻干净；Phase-4 同步新出错时命中=绕闸，「沙箱绿本地 diff
        # 缺产物」的静默假绿复活。
        _sync_clean = (getattr(self, "_sync_skipped_count", 0) == 0
                       and not getattr(self, "_sync_error_rels", None)
                       and not getattr(self, "_sync_oversize_rels", None))
        if (_sync_clean
                and _gate_diff_sig == getattr(self, "_last_gate_diff_sig", None)
                and getattr(self, "_last_gate_details", None) is not None):
            import copy as _copy
            _cached = _copy.deepcopy(self._last_gate_details)  # R-F2：防嵌套共享变异
            _cached["reused_deterministic_gate"] = True
            self._log("L1 确定性闸门：diff 未变+上次 PASS+同步干净 → 复用结果（省一遍全量 pipeline）")
            return True, _cached
        # empty_diff 判定：strip 后判空，杜绝 whitespace-only / 占位变体绕过。
        # 过去仅匹配固定字面串("(无变更)"等)，导致纯空格 diff(如 "   ")被当"有变更"
        # 送进 pipeline → 解析出 0 文件 → "no diff changes → True" → 空 diff 漏判通过。
        _diff_stripped = (diff or "").strip()
        empty_diff = (
            not _diff_stripped
            or _diff_stripped in ("(无变更)", "(无法获取 git diff)")
        )
        harness = getattr(self.subtask, "harness", None)
        has_harness_checks = bool(
            harness and (harness.build_command or harness.test_command or harness.verify_commands)
        )
        # 空 diff = worker 没产生任何改动。若任务【本应改/建文件】(scope 有 writable/
        # create_files)，这是"没干活"，绝不能因 mvn 编译未改动代码恰好通过就误判 PASS。
        # 实测：模型 "need more steps"/stall 后没改 StringUtils，diff 空但 L1 却通过 →
        # 任务假 DONE。空 diff + 期望有产出 → 确定性判失败，触发重试/换模型。
        scope = self.effective_scope
        # A1 治本：delete_files 也是"期望产生变更"——删除必须体现为 diff（本地文件被
        # unlink → git diff 显示删除）。漏算 delete_files 时，纯删除 scope 恒空 diff →
        # 走下方 BENIGN → 回退 LLM 弱信号假绿（删除从未发生/未传播）。纳入后：删除已
        # 传播成功 → diff 非空正常裁决；未传播/未执行 → 空 diff + expects → 判 False。
        expects_changes = bool(
            (getattr(scope, "writable", []) or [])
            or (getattr(scope, "create_files", []) or [])
            or (getattr(scope, "delete_files", []) or [])
        )
        if empty_diff and expects_changes:
            return False, {
                "deterministic_gate": "fail",
                "reason": "empty_diff_but_changes_expected",
                "note": "worker 未产生任何改动（期望修改/新建文件），判定未完成",
            }
        if empty_diff and not has_harness_checks:
            # 既无 diff 又无 harness 可执行检查——且上方已排除 expects_changes（那是 BLOCKED fail）。
            # 此即真 no-op：合法地没东西可验证 → BENIGN，可回退 LLM 弱信号。
            return None, {"deterministic_gate": "skipped: empty diff",
                          "not_run_kind": NotRunKind.BENIGN.value}
        try:
            from swarm.worker.l1_pipeline import run_l1_pipeline

            # 空 diff 但有 harness（如 greenfield 新建文件 diff 未被捕获）：
            # 仍用 harness 命令做确定性验证，杜绝 LLM 口头自报合格。
            ok, details = run_l1_pipeline(
                self.project_path, self.subtask, diff or "", llm=None,
                project_stack=self._resolve_project_stack(),
                # round18 P0-B：确定性修复触达的 scope 外文件(如 module-reg 自愈的父 pom)
                # 不计入 scope 违规——见 _get_git_diff 把 _repaired_extra_paths 纳入 diff。
                extra_writable_paths=set(self._repaired_extra_paths),
                # C1（阶段4）：worker 总预算贯穿 pipeline——A5 的入口布尔快照只拦"进门时
                # 已超时"，进门后 build+repair 900s 墙钟与预算解耦（最坏 ~35min runaway）。
                deadline=(self.start_time + self.max_execution_time
                          if getattr(self, "start_time", 0) else None),
            )
            # TD2606-C9：登记本轮在沙箱里被确定性修复的文件，使其回传本地 + 计入 diff。
            self._record_repaired_paths(details)
            # audit #5/#29：标记此为 Phase 3 循环内确定性闸门(llm=None，无 LLM 开销)。
            details["l1_phase"] = "phase3_loop_deterministic"
            # fail-closed：pipeline 可能「跑通了能跑的、但有该验证的环节被阻塞」（构建工具/工程
            # 清单缺失、构建命中 infra 瞬时故障、非空 diff 却解析到 0 文件）。这种 passed-but-blocked
            # 绝不能当真 PASS → 降为 None(BLOCKED)，交裁决器走 transient 退避重试。
            if ok and details.get("pipeline_blocked"):
                details["deterministic_gate"] = "skipped: pipeline blocked"
                details["not_run_kind"] = NotRunKind.BLOCKED.value
                return None, details
            # D30 治本：pull-back 因超过 MAX_SYNC_FILE_SIZE 的【确定性】skip 单独归类——
            # 每次重试同样超限，当 transient BLOCKED 退避就是活锁（旧行为：package-lock.json
            # >1MiB 永久 BLOCKED 至配额耗尽）；判 PASS 又是静默丢产物（本地 diff 缺该文件）。
            # → 确定性 FAIL 走既有失败阶梯（重试换法/拆分/放弃），可观测且不空烧。
            if ok and self._sync_oversize_rels:
                logger.warning(
                    "[L1] pull-back 有超限文件确定性 skip(%d 个: %s) → 判确定性 FAIL"
                    "(非 transient，重试不可恢复；上限 SWARM_SANDBOX_MAX_SYNC_FILE_SIZE 可调)",
                    len(self._sync_oversize_rels), self._sync_oversize_rels[:5],
                )
                details["deterministic_gate"] = "fail"
                details["reason"] = "pullback_oversize_deterministic_skip"
                details["oversize_files"] = list(self._sync_oversize_rels)
                details["note"] = (
                    "产物超过单文件同步上限被确定性跳过，本地交付 diff 不完整；"
                    "可调大 SWARM_SANDBOX_MAX_SYNC_FILE_SIZE 或缩小产物"
                )
                return False, details
            # A3 治本(fail-closed)：沙箱内 pipeline 判 True，但本轮 pull-back 若有 skip(>1MiB)
            # 或 err(读失败) → 本地工作区/diff 不完整，"沙箱绿"不代表"本地交付完整"。此时禁止
            # 判 True → 降 None(BLOCKED) 走 transient 退避重试拉全，杜绝沙箱绿本地缺的静默假绿。
            if ok and (self._sync_skipped_count > 0 or self._sync_error_rels):
                logger.warning(
                    "[L1] pull-back 不完整(skipped=%d, errors=%d)但沙箱 pipeline 判过 → "
                    "拒绝判 PASS(降 BLOCKED 重试)，防沙箱绿本地 diff 缺改",
                    self._sync_skipped_count, len(self._sync_error_rels),
                )
                details["deterministic_gate"] = "skipped: pull-back incomplete"
                details["not_run_kind"] = NotRunKind.BLOCKED.value
                details["pullback_skipped"] = self._sync_skipped_count
                details["pullback_errors"] = len(self._sync_error_rels)
                return None, details
            details["deterministic_gate"] = "pass" if ok else "fail"
            if empty_diff:
                details["note"] = "empty diff，仅靠 harness 命令验证"
            # C2（阶段4）：只缓存【确定性 PASS】——diff 内容签名未变时 Phase-4 复用，
            # 不再整遍重跑（happy-path 每子任务 ≥3 次全量 L1 的主推手）。FAIL/BLOCKED
            # 绝不缓存：修复动作会改沙箱态，同 diff 重验是修复回路既有语义。
            if ok:
                self._last_gate_diff_sig = _gate_diff_sig
                self._last_gate_details = dict(details)
            return ok, details
        except Exception as exc:  # noqa: BLE001
            return None, {"deterministic_gate": f"skipped: pipeline error {exc}",
                          "not_run_kind": NotRunKind.BLOCKED.value}

    def _maybe_capture_tdd_red_baseline(self) -> None:
        """T3·TDD 红绿闸（ECC §C 移植）：DEBUG 意图在【编码前 HEAD 基线】跑一次 failing_test_command
        取 RED 证据——此刻沙箱/本地树=HEAD=bug 未修，failing_test 应【失败】(exit≠0)=RED 成立。

        三态存 self._tdd_red_exit_code（对齐 l3/runtime_smoke 三态语义，None≠False）：
          - exit≠0 → RED 成立（failing_test 在未修代码上确实失败，bug 复现）。
          - exit==0 → failing_test 在未修代码上就通过=【不复现 bug】（测试恒绿/平凡通过）→ 后续
            "修复后通过"是假信号，Phase4 DEBUG 闸据此 fail-closed（ECC 红绿铁律：无红不算绿）。
          - None → 跳过（非 DEBUG/无 failing_cmd/env 关）或基线跑不动（异常）→ 不阻断、仅可观测，
            绝不把"基线跑不动"误判成红证证伪而误伤合法修复。

        默认开，泄压阀 SWARM_WORKER_TDD_RED_GATE=0 关（改动的是既有 DEBUG 绿闸判据，留逃生口）。
        栈无关：只读 harness.failing_test_command，复用 _run_l1_command（沙箱优先+本地兜底）。
        """
        self._tdd_red_exit_code: int | None = None
        self._tdd_red_detail: str = ""
        # 泄压阀（与本仓 SWARM_WORKER_* 惯例一致：默认 true，关闭集 false/0/no）。
        if os.environ.get("SWARM_WORKER_TDD_RED_GATE", "true").lower() in ("false", "0", "no"):
            return
        if getattr(self.subtask, "intent", None) != "debug" or not self.project_path:
            return
        harness = getattr(self.subtask, "harness", None)
        failing_cmd = getattr(harness, "failing_test_command", "") if harness else ""
        if not failing_cmd:
            return
        from swarm.worker.l1_pipeline import _run_l1_command
        from swarm.worker.output_compress import compress_tool_output
        try:
            # F5(对抗复核)：基线只需证明"失败"，用较短超时(45s)，避免 hang-bug 的基线跑满 120s
            # 吞掉子任务【共享】墙钟预算（LOCATING+CODING+VERIFY+PRODUCE 同一时钟）。
            ec, out = _run_l1_command(failing_cmd, self.project_path, timeout=45)
        except Exception as exc:  # noqa: BLE001
            # 理论兜底：_run_l1_command 内部已吞异常统一返回 (int,str)，此分支实际少走；仍保守置 None。
            self._tdd_red_exit_code = None
            self._tdd_red_detail = f"baseline capture error (skip, non-blocking): {exc}"
            self._log(f"TDD 红绿闸: 基线红证采集异常，三态置 None 不阻断: {exc}")
            return
        # F3(对抗复核)：_run_l1_command 把基础设施失败塌成非零退出码——命令被黑名单拒=126、
        # 超时=124（沙箱/网络抖动或 hang）。这些【非真测试断言失败】不能冒充红证 → 归三态 None
        # (未知，不计红证)：诚实审计 + 对齐"扫不动≠证成立"；observe-only 默认下 false-RED 本就低危，此处再去噪。
        if ec in (124, 126):
            self._tdd_red_exit_code = None
            self._tdd_red_detail = f"baseline infra/timeout (non-blocking, not counted as red), raw exit={ec}"
            self._log(f"TDD 红绿闸: 基线疑似基础设施失败/超时(exit={ec})，三态置 None 不计红证")
            return
        self._tdd_red_exit_code = ec
        self._tdd_red_detail = (
            f"baseline exit_code={ec}, output={compress_tool_output(out or '', max_chars=600)}"
        )
        if ec == 0:
            self._log(
                "TDD 红绿闸: ⚠️ failing_test 在 HEAD 基线(未修)就通过=红证不成立"
                f"（默认仅观测不阻断；strict 模式才 fail-closed）| {failing_cmd}"
            )
        else:
            self._log(f"TDD 红绿闸: 基线 RED 成立(exit={ec})，failing_test 复现 bug ✅")

    def _tdd_red_green_verdict(
        self, debug_green_ok: bool, red_exit_code: int | None, *, strict: bool = False
    ) -> tuple[bool, str]:
        """T3·红绿闸裁决（纯函数，易测）：综合 GREEN(修复后 failing_test 通过) + RED(基线未修时失败)。

          - GREEN 未过 → (False, "green_failed")：修复后测试仍失败=未修复（既有行为）。
          - GREEN 过 + RED 证伪(red_exit_code==0=基线未修就绿=不复现 bug)：
              · strict(SWARM_WORKER_TDD_RED_STRICT=1) → (False, "red_not_proven_failclosed")。
              · 默认非 strict → (True, "red_not_proven_observed")：只观测留痕【不阻断】。
                对抗复核 F1/F2：硬 fail-closed 会误伤 ①间歇/竞态 bug(基线偶然通过) ②修复中【新写】
                复现测试、而 runner(go test -run / maven failIfNoTests)对无匹配测试 exit 0 的合法修复。
                默认观测优先（北极星"绝不误杀"+"通用多栈"）；strict 留给"复现测试窄且确定"的运维显式开。
          - GREEN 过 + RED 成立(red≠0) → (True, "green_after_red")：真红转绿。
          - GREEN 过 + RED 未知(None=基线跳过/跑不动/基础设施噪声) → (True, "green_red_unknown")：None≠False。
        """
        if not debug_green_ok:
            return False, "green_failed"
        if red_exit_code == 0:
            return (False, "red_not_proven_failclosed") if strict else (True, "red_not_proven_observed")
        return True, ("green_after_red" if red_exit_code is not None else "green_red_unknown")

    def _run_failing_test_gate(self, failing_cmd: str) -> tuple[bool, str]:
        """DEBUG 意图专属 L1 闸门：确定性执行 failing_test_command，验证修复后该命令通过。

        复用 l1_pipeline 的 _normalize_python_cmd + subprocess 机制，
        与现有 L1 确定性验证共享同样的执行模型（local / sandbox 均可）。
        返回 (bool, detail_str)：True=命令通过(bug 已修复)，False=命令仍失败。

        优雅降级：异常时返回 False（保守失败，M1 修复）——执行环境失败
        不能误判为 bug 已修复，宁可判未通过让其重试/人工复核。
        """
        # TD2606-C2：走 sandbox-first 的 _run_l1_command（与 L1 确定性闸门同执行模型）。
        # 原实现裸 local subprocess，在非 Python 栈(本地无 mvn/go/cargo 工具链)必 except →
        # DEBUG 意图任务的闸门【永远】保守失败、无法验证修复。沙箱可用即在沙箱跑、否则回退本地。
        from swarm.worker.l1_pipeline import _run_l1_command
        from swarm.worker.output_compress import compress_tool_output

        try:
            ec, out = _run_l1_command(failing_cmd, self.project_path, timeout=120)
            ok = ec == 0
            detail = f"exit_code={ec}, output={compress_tool_output(out or '', max_chars=800)}"
            return ok, detail
        except Exception as exc:  # noqa: BLE001
            # M1：执行环境异常 → 保守判失败（不能把"验证不了"当"已修复"放过未修坏代码）。
            self._log(f"DEBUG L1: failing_test_command 执行异常，保守判未通过: {exc}")
            return False, f"execution error (conservative fail): {exc}"

    def _parse_produce_result(
        self,
        produce_result: str,
        l1_passed: bool,
        l1_details: dict,
    ) -> WorkerOutput:
        """解析产出结果，构造 WorkerOutput"""
        summary = ""
        confidence = Confidence.MEDIUM
        notes = ""

        # 尝试从输出中提取结构化字段
        lines = produce_result.split("\n")
        for line in lines:
            if line.startswith("SUMMARY:"):
                summary = line[len("SUMMARY:"):].strip()
            elif line.startswith("CONFIDENCE:"):
                conf_str = line[len("CONFIDENCE:"):].strip().lower()
                confidence = Confidence(conf_str) if conf_str in ("high", "medium", "low") else Confidence.MEDIUM
            elif line.startswith("NOTES:"):
                notes = line[len("NOTES:"):].strip()

        if not summary:
            summary = produce_result[:500]

        diff = self._get_git_diff()

        return WorkerOutput(
            subtask_id=self.subtask.id,
            diff=diff,
            summary=summary,
            confidence=confidence,
            l1_passed=l1_passed,
            l1_details=l1_details,
            execution_log="\n".join(self.execution_log),
            notes=notes,
        )
