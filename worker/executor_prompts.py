"""Worker prompt/grounding 构建混入 —— 从 worker/executor.py 抽出（round26 god-file 治理 Step1）。

PROMPT 连通分量：11 个只读 self 状态、产出提示串的方法（各 build_*_prompt + 上下文/操作
清单/失败摘要/符号接地）。这些方法是 god-class 的叶簇——被 phase 方法【调用】，自身不驱动编排、
不写共享可变态，簇内互调（_context_snippets_block/_scope_ops_hint/_l1_failure_digest/
_javap_method_grounding）全在本 mixin 内。

以【混入类】而非独立协作者外置：WorkerExecutor 多继承本 mixin，self.<字段>/WorkerExecutor.<方法>/
patch.object(ex, "_build_*") 经 MRO 保持可寻址，既有测试零改动。本模块【禁】eager import
worker.executor（防 A6 循环）——只依赖 get_config 与函数内 lazy import。
"""

from __future__ import annotations

from swarm.config.settings import get_config


class _PromptBuildingMixin:
    """WorkerExecutor 的 prompt/grounding 构建方法簇（见模块 docstring）。

    仅读 self.effective_scope / self.subtask / self.project_id / self._sandbox /
    self._sandbox_manager（均由 WorkerExecutor.__init__ 初始化）。不持有自身状态。
    """

    def _scope_ops_hint(self) -> str:
        """生成给 LLM 的文件操作清单：明确哪些是修改/新建/删除。"""
        s = self.effective_scope
        if getattr(s, "allow_any", False) and not (
            getattr(s, "writable", []) or getattr(s, "create_files", []) or getattr(s, "delete_files", [])
        ):
            return (
                "【自由创建模式】这是一个从零开始/开放式任务，没有预设文件清单。"
                "你可以根据需求自由用 write_file 创建任意需要的文件（如源码、README、配置等），"
                "用 run_command 建目录/跑命令。请规划合理的项目结构并实现完整功能。"
            )
        modify = list(getattr(s, "writable", []) or [])
        create = list(getattr(s, "create_files", []) or [])
        delete = list(getattr(s, "delete_files", []) or [])
        readable = [f for f in (getattr(s, "readable", []) or []) if f not in modify + create + delete]
        lines = []
        if modify:
            lines.append(f"【修改现有文件】{', '.join(modify)} — 先 read_file 读取，再 patch_file/write_file 改动")
        if create:
            lines.append(f"【新建文件】{', '.join(create)} — 不要 read_file（文件还不存在），直接 write_file 写入完整内容")
        if delete:
            lines.append(f"【删除文件】{', '.join(delete)} — 用 run_command 执行 rm 删除")
        if readable:
            lines.append(f"【只读参考】{', '.join(readable)} — 仅供理解上下文，不要修改")
        return "\n".join(lines) if lines else "见 scope（无显式文件清单，请先用工具探查项目结构）"

    def _context_snippets_block(self) -> str:
        """方案A(task 34fab09e)：ELABORATE 预注入的 scope 文件代码片段。
        worker 直接据此编写，无需在沙箱里 cat 探索耗尽迭代步数。无则返回空串。"""
        snip = getattr(self.subtask, "context_snippets", "") or ""
        if not snip.strip():
            return ""
        return f"\n\n📎 预读代码上下文（已为你读好，直接据此实现，无需再 cat 探索）：\n{snip}\n"

    def _build_locate_prompt(self) -> str:
        return (
            "请开始 Phase 1（定位）：\n"
            "1. 阅读你权限范围内的相关文件\n"
            "2. 定位需要修改或实现的代码位置\n"
            "3. 确认接口契约和依赖关系\n"
            "⚠️ 上下文有限：大文件务必用 read_file(path, start_line=N, end_line=M) 只读需要的"
            "行范围，或先 search_files 定位行号再局部读。禁止对大文件无参数读全文（会撑爆上下文）。\n"
            "✅ 若下方已提供【预读代码上下文】，优先据此定位，能不 cat 就不 cat（省步数预算）。\n"
            "请简要汇报你的定位结果。"
            + self._context_snippets_block()
        )

    def _build_code_prompt(self, locate_result: str) -> str:
        return (
            "请开始 Phase 2（编码）：\n"
            f"定位结果: {locate_result}\n\n"
            "文件操作清单（务必按操作类型处理）：\n"
            f"{self._scope_ops_hint()}\n\n"
            "根据定位结果和子任务要求进行实现：\n"
            "⚠️ 上下文有限：改文件前用 read_file(path, start_line=N, end_line=M) 只读目标行范围，"
            "不要无参数读全文；用 patch_file 做最小必要改动，不要全文重写输出（大文件全文重写会撑爆上下文）。\n"
            "1. 【修改】文件：用 patch_file 在可写范围内改动\n"
            "2. 【新建】文件：用 write_file 直接写入完整内容，不要先 read_file\n"
            "3. 【删除】文件：用 run_command 执行 rm\n"
            "4. 确保修改符合接口契约，保持代码风格一致\n\n"
            "⚠️ 本阶段【只管把目标文件改对】，禁止运行 mvn/gradle/npm 等重型构建或测试命令"
            "（编译和测试由后续 Phase 3 / 系统确定性 L1 闸门统一负责）。反复跑构建会耗光步数"
            "预算导致任务失败。改完目标文件即【立即停止】并确认改动，不要反复读取/编译/自我怀疑。\n"
        )

    def _build_batch_code_prompt(
        self, locate_result: str, batch: list[str], done: list[str], idx: int, total: int
    ) -> str:
        """B2 分批编码 prompt：只聚焦本批文件，已完成的不重做。"""
        done_hint = f"\n已完成文件（勿重做）：{done}\n" if done else "\n"
        return (
            f"请开始 Phase 2 编码（分批 {idx}/{total}）：\n"
            f"定位结果: {locate_result[:400]}\n"
            f"{done_hint}"
            f"\n🎯 本批【只】负责这些文件，其它文件本批不要碰：\n{batch}\n\n"
            "处理规则：\n"
            "1. 【修改】用 read_file(局部行范围) 后 patch_file 最小改动\n"
            "2. 【新建】直接 write_file 写完整内容（不要先 read_file）\n"
            "3. 确保符合接口契约、与已完成文件协调一致、保持代码风格\n"
            "⚠️ 只写本批文件即【立即停止】，禁止跑 mvn/gradle/npm 构建测试（L1 闸门统一负责），"
            "不要反复读取/自我怀疑（省步数预算）。\n"
            + self._context_snippets_block()
        )

    def _build_verify_prompt(self) -> str:
        # ── 根因修复(task 51c8e1f8)：medium/complex 路径的 worker 自验证绕圈 ──
        # 旧 prompt 让 worker 自己 run_compile + run_tests，但系统的确定性 L1 闸门
        # worker 再自己反复跑 mvn compile/test 是【纯多余的绕圈】：在复杂项目(RuoYi junit
        # 环境)测试跑不起来时，worker 会反复 mvn test + 查 junit 依赖，耗尽迭代上限(50)，
        # 即使实现代码本身 mvn compile=exit0(对的)也被拖死。
        # 修复：worker 只【自查改动是否完整】(读回改的文件确认)，编译/测试由系统确定性闸门负责。
        # 与 trivial 路径"禁止自跑 mvn"一致。worker 是开发，不是测试工程师。
        return (
            "请开始 Phase 3（自查）：\n"
            "1. 简要 review 你本轮的改动是否【完整覆盖】子任务要求（可 read_file 看几眼改过的文件）。\n"
            "2. 确认没有明显语法错误（凭阅读判断，不要运行构建）。\n\n"
            "⚠️【禁止运行重型构建/测试命令】：不要跑 mvn compile / mvn test / gradle / npm 等。\n"
            "编译和测试由系统的确定性 L1 闸门统一负责（系统会真跑一次编译+harness 测试），\n"
            "你自己反复跑会耗光步数预算导致任务失败。改动完整即【立即停止】。\n"
            "报告格式：L1_RESULT: PASS（你认为改动完整）或 L1_RESULT: FAIL（发现改漏/改错），然后简述。"
        )

    def _l1_failure_digest(self, l1_details: dict) -> str:
        """从确定性 L1 结果提取【真实失败证据】摘要（已是压缩值，不膨胀 context）。

        I4（Anthropic code-execution/context-engineering 启发）：fix prompt 过去只带 LLM
        自己上轮的 verify_result（自报，可能没说清真正的 compile 错误）。这里改为优先注入
        确定性 pipeline 抓到的真实失败信号（compile_message / lint / build_output，均已被
        compress_tool_output 压到 ≤1500 字符），让修复有的放矢，且因用压缩摘要不灌全量输出。
        """
        if not l1_details:
            return ""
        parts: list[str] = []
        # scope 越权（最高优先，确定性硬失败）
        sv = l1_details.get("scope_violations")
        if sv:
            parts.append(f"[scope 越权] 改了 scope 外的文件: {sv}")
        cm = (l1_details.get("compile_message") or "").strip()
        if cm and not l1_details.get("l1_2_compile_ok", True):
            parts.append(f"[编译失败]\n{cm}")
        lint = l1_details.get("lint") or {}
        if isinstance(lint, dict) and lint.get("message") and lint.get("status") == "error":
            parts.append(f"[lint 失败]\n{str(lint.get('message')).strip()}")
        bo = (l1_details.get("build_output") or "").strip()
        if bo and l1_details.get("l1_2_1_build_ok") is False:
            parts.append(f"[构建失败]\n{bo}")
        reason = l1_details.get("reason")
        if reason and not parts:
            parts.append(f"[确定性闸门] {reason}: {l1_details.get('note', '')}")
        return "\n\n".join(parts).strip()

    async def _symbol_grounding_hint(self, verify_result: str, l1_details: dict | None) -> str:
        """路径1 治本：编译报 cannot find symbol → codegraph 解析真实 FQN 提示。

        只在出现 cannot find symbol 时触发；offload 到线程不阻塞执行 loop；全程 try 包裹 +
        service 层自身吞异常——接地是【增益】，绝不能因它让修复回路崩或卡。
        """
        try:
            digest = self._l1_failure_digest(l1_details or {})
            evidence = digest or verify_result or ""
            if "cannot find symbol" not in evidence:
                return ""
            import asyncio as _asyncio

            from swarm.knowledge.service import resolve_symbols_sync
            sc = getattr(self.subtask, "scope", None)
            create_files = list(getattr(sc, "create_files", []) or []) if sc else []
            class_hint = await _asyncio.to_thread(
                resolve_symbols_sync, evidence, self.project_id or "", create_files
            )
            # P5：臆造【方法】接地——class-FQN 解析接不住"真实类上调不存在的方法"
            # （Base64.encodeToByte），沙箱 javap 取真实方法集喂模型。
            method_hint = await _asyncio.to_thread(self._javap_method_grounding, evidence)
            return "\n\n".join(p for p in (class_hint, method_hint) if p)
        except Exception:  # noqa: BLE001
            return ""

    def _javap_method_grounding(self, evidence: str) -> str:
        """P5（治本，996db614 实测 18×900s 主因之一）：编译报 `cannot find symbol: method X /
        location: class C`（在真实存在的类上调臆造方法）→ 沙箱内 `javap C` 取 C 真实方法集，
        生成"C 真实方法有 [...]，X 不存在，从中选"提示，杜绝模型反复臆造方法烧满 900s。

        JDK 类（java.*/javax.*）javap 无需 classpath 直接解析；非 JDK/javap 失败优雅跳过（增益层，
        绝不阻断）。symbol-repair 的近邻纠错接不住此类（无项目近邻），codegraph 也跳过 method。"""
        try:
            if not self._sandbox or not self._sandbox_manager:
                return ""
            from swarm.worker.symbol_resolver import (
                build_method_grounding,
                parse_javap_methods,
                parse_missing_methods,
                to_javap_class_name,
            )
            pairs = parse_missing_methods(evidence)
            if not pairs:
                return ""
            rc = getattr(self._sandbox_manager, "run_command", None)
            if rc is None:
                return ""
            remote = get_config().sandbox.sandbox_remote_workdir
            # R2（治本，996db614 实证 CipherUtils 类幻觉）：javap 无 -cp 解析不了【项目类】(CipherUtils
            # 在 ruoyi-common)/【第三方库类】(RedisTemplate/Jwts/StrUtil 在依赖 jar)→ 空输出→无接地→
            # 模型打地鼠猜方法名。组【完整 classpath】让任意 classpath 上的类都可 javap：
            #   ① 项目类 = 各模块 target/classes（-am compile 后已存在）；
            #   ② 第三方类 = mvn dependency:build-classpath 导出依赖 jar 全集（deps 已在 ~/.m2，本地解析快）。
            # 合并去重写入沙箱临时文件一次，各 javap 复用。mvn 不可用/失败→优雅降级到仅 target/classes。
            cp_build = (
                f"cd {remote} 2>/dev/null && rm -f /tmp/swarm_dep_cp.txt 2>/dev/null; "
                f"mvn -q dependency:build-classpath -Dmdep.outputFile=/tmp/swarm_dep_cp.txt "
                f"-Dmdep.appendOutput=true >/dev/null 2>&1; "
                f"{{ find . -path '*/target/classes' -type d 2>/dev/null; "
                f"tr ':' '\\n' < /tmp/swarm_dep_cp.txt 2>/dev/null; }} "
                f"| sort -u | tr '\\n' ':' > /tmp/swarm_javap_cp.txt"
            )
            rc(self._sandbox, cp_build, timeout=150)
            probed: list[tuple[str, str, list[str]]] = []
            seen_classes: set[str] = set()
            for method, klass in pairs[:5]:
                if klass in seen_classes:
                    continue
                seen_classes.add(klass)
                bin_name = to_javap_class_name(klass)
                import shlex
                cmd = (
                    f"cd {shlex.quote(remote)} 2>/dev/null && "
                    f"javap -cp \"$(cat /tmp/swarm_javap_cp.txt 2>/dev/null).\" "
                    f"-public {shlex.quote(bin_name)} 2>/dev/null | head -80"
                )
                result = rc(self._sandbox, cmd, timeout=30)
                methods = parse_javap_methods(getattr(result, "stdout", "") or "")
                if methods:
                    probed.append((method, klass, methods))
            return build_method_grounding(probed)
        except Exception:  # noqa: BLE001
            return ""

    def _build_fix_prompt(
        self, verify_result: str, l1_details: dict | None = None, symbol_hint: str = ""
    ) -> str:
        # I4：优先用确定性失败证据（真实 compile/lint/scope，已压缩），回退 LLM 自报
        digest = self._l1_failure_digest(l1_details or {})
        evidence = digest if digest else verify_result
        # 路径1 治本：编译报 cannot find symbol 时，附 codegraph 解析的真实 FQN，
        # 让 worker 照真实位置改 import，而非再猜包名（RUN20 主导缺陷类）。
        grounding = f"\n\n{symbol_hint}" if symbol_hint else ""
        # C7（阶段4，登记册 §四）：fix 轮带修改记忆——每轮 _run_agent 是全新单条 human
        # 消息（无对话累积），模型看不到自己已改过什么 → 重复勘察烧步数/把同一 typo
        # 反复写回（996db614 实证）。确定性拼进已改文件清单+关键新增行（数据源=当前
        # git diff，含确定性修复触达的 scope 外文件）；失败绝不拖垮 fix 轮（空块降级）。
        changed_block = ""
        try:
            _diff = self._get_git_diff()
            from swarm.project.diff_apply import files_from_unified_diff
            _files = files_from_unified_diff(_diff or "")
            if _files:
                from swarm.memory.pattern_extractor import extract_key_lines
                _key = extract_key_lines(_diff or "", max_lines=15)
                changed_block = (
                    "\n\n【你在本子任务已修改的文件（勿重复勘察，改动已生效）】\n- "
                    + "\n- ".join(_files[:20])
                    + (f"\n最近关键新增行（节选）：\n{_key}" if _key else ""))
        except Exception:  # noqa: BLE001 — 记忆块是增益，绝不阻断修复
            changed_block = ""
        return (
            f"L1 验证未通过，确定性失败证据：\n{evidence}{grounding}{changed_block}\n\n"
            "请分析失败原因并修复代码：\n"
            "1. 仔细阅读上面的错误信息（这是真实的编译/lint/scope 检查结果）\n"
            "2. 定位问题根因（若有【符号接地提示】，照其给出的真实 FQN 修正引用，勿臆造包名）\n"
            "3. 使用 patch_file 修复（已修改文件清单里的改动无需重做）\n"
            "完成后请再次运行验证。"
        )

    def _build_produce_prompt(self) -> str:
        return (
            "请开始 Phase 4（产出）：\n"
            "1. 回顾你刚才用 write_file/patch_file 做的所有改动（系统会自动采集文件 diff，"
            "无需依赖 git；若你想复核可用 read_file 查看最终内容）\n"
            "2. 撰写变更摘要\n"
            "3. 评估你的置信度\n\n"
            "请按以下格式输出：\n"
            "```\n"
            "SUMMARY: (变更摘要)\n"
            "CONFIDENCE: (high/medium/low)\n"
            "NOTES: (需要人工审查的部分，如无则写 无)\n"
            "```"
        )
