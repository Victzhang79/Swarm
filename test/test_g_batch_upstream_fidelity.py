"""26 号文 G 路·上游保真批：需求→设计这一跳的三条信息损毁。

共性：闸门下游判得再准也救不回来——决定"要建什么"的节点看到的就是残缺/噪声/幻觉输入。
"""
from __future__ import annotations

import tempfile

import pytest

from swarm.brain.baseline_candidates import (
    _PATH_LIKE_RE,
    baseline_candidates_prompt_block,
    build_baseline_candidates,
)
from swarm.brain.prompt_clip import clip_for_prompt
from swarm.brain.stack_detect import detect_stack_deterministic


# ══════════════════════════════════════════════
# C-12：路径正则交替支顺序 + 尾边界
# ══════════════════════════════════════════════

@pytest.mark.parametrize("path", [
    "a/b/Foo.tsx", "w/x.cpp", "w/x.kts", "w/x.jspx", "m/A.java", "p/B.py", "v/C.vue",
])
def test_path_regex_not_truncated_by_shorter_alternative(path):
    """★交替是最左优先，没有尾边界就会被短支截断（26 号文 C-12 实测）★
    `ts|tsx` 里 `ts` 先匹配 → `a/b/Foo.tsx` 被截成 `a/b/Foo.ts`；同族 cpp→c、kts→kt、jsp→js。
    后果不是少提一条：抽不出路径 → 合法 baseline 申报被判"凭空捏造" → 并进
    `baseline_ineligible_reqs`（monotonic 单调累积）→ 误判此后每轮都在，不可撤销。"""
    assert _PATH_LIKE_RE.findall(path) == [path]


@pytest.mark.parametrize("path", ["tpl/x.ftl", "tpl/x.vm", "w/x.html", "w/x.jsp"])
def test_path_regex_covers_server_template_family(path):
    """E2E 基线 RuoYi 是 Thymeleaf/Velocity 单体，前端就在 templates 里——
    原表只有后端源码族，模板文件一个都抽不出来。"""
    assert _PATH_LIKE_RE.findall(path) == [path]


@pytest.mark.parametrize("noise", ["a/b.tsxx", "中文/说明", "只是/一句话"])
def test_path_regex_stays_conservative(noise):
    """放宽扩展名不能把普通中文/词组当路径——尾边界同时挡住 `.tsxx` 这类越界匹配。"""
    assert not _PATH_LIKE_RE.findall(noise)


# ══════════════════════════════════════════════
# C-10：A7 存量对账——噪声候选 + 把漏检升级成禁令
# ══════════════════════════════════════════════

_SYMS = [
    {"file_path": "common/PermissionService.java",
     "symbol_name": "isLacksPermitted", "class_name": "PermissionService"},
    {"file_path": "ruoyi-generator/GenTableController.java",
     "symbol_name": "genCode", "class_name": "GenTableController"},
]
_FILES = [
    {"file_path": "common/PermissionService.java", "module_name": "ruoyi-common"},
    {"file_path": "ruoyi-generator/GenTableController.java", "module_name": "ruoyi-generator"},
]


def test_substring_match_must_align_identifier_segments():
    """★`'slack' ⊂ 'isLacksPermitted'`（26 号文 C-10 实测）★
    裸子串匹配让本轮 6 条需求的"存量疑似实现"全指向同一个 isLacksPermitted。
    噪声候选不是"多几条无用提示"——清单尾部带着"清单外不要申报"的禁令，
    等于把大模型往错误的既有实现上引。"""
    got = build_baseline_candidates(
        [{"id": "req-1", "text": "支持 Slack 通知渠道"}], _FILES, _SYMS)
    hits = [c for c in got if c.get("candidates")]
    assert not hits, f"'slack' 不该匹配 isLacksPermitted：{hits}"


def test_real_baseline_still_recalled_after_boundary_tightening():
    """收紧不能把真存量也收掉：本轮真正该被认出的 ruoyi-generator 必须仍在。
    （段界匹配允许"连续段拼接"：gentable ↔ Gen|Table|Controller）"""
    got = build_baseline_candidates(
        [{"id": "req-2", "text": "GenTable 代码生成器"}], _FILES, _SYMS)
    assert got and got[0]["candidates"][0]["file"].startswith("ruoyi-generator/")


def test_unsearchable_requirement_is_marked_not_dropped():
    """★"检索不了" ≠ "检索过、没有"（26 号文 C-10）★
    本通道只认 ASCII token，纯中文需求恒 0 token → 原先直接 continue、条目不出现在清单里，
    而清单尾部写着"清单外的条目不要凭空申报 baseline_covered"——**一次检索能力的缺席被
    渲染成了对模型的禁令**。本轮 16 条 base 已有能力的需求正是这样被逼着重新实现。"""
    got = build_baseline_candidates(
        [{"id": "req-3", "text": "菜单管理：支持动态路由、按钮权限"}], _FILES, _SYMS)
    assert got and got[0].get("unsearchable") is True


def test_prompt_block_lifts_prohibition_for_unsearchable_items():
    """禁令的前提是"我们确实替你查过了"——查不了的条目必须显式解除禁令。"""
    blk = baseline_candidates_prompt_block(build_baseline_candidates(
        [{"id": "req-2", "text": "GenTable 代码生成器"},
         {"id": "req-3", "text": "菜单管理：支持动态路由、按钮权限"}],
        _FILES, _SYMS), truncated=False)
    assert "检索能力不覆盖" in blk
    assert "req-3" in blk
    assert "禁令对它们不适用" in blk
    assert "本通道没有查过，不代表存量里没有" in blk
    # 有候选的条目仍然带禁令（不能因为解除一处就整体放开）
    assert "清单外的条目不要凭空申报" in blk


# ══════════════════════════════════════════════
# C-11：stack_detect fail-open 产幻觉画像并永久缓存
# ══════════════════════════════════════════════

def test_unreadable_path_is_scan_failed_not_no_stack():
    """★实测 detect_stack_deterministic("/nonexistent") → backend='未判明' confidence=0.2，
    零异常零日志（26 号文 C-11）★ 三条链必须一起断：
    低置信 → needs_model_adjudication → LLM 拿空证据凭训练先验裁决（正是 task 8537fa5e
    "RuoYi=Vue" 死代码的产地）→ 写 projects.config 并按空指纹缓存 → 下次同样扫不到时
    指纹相同、缓存命中 → **幻觉画像永久复用**。"""
    p = detect_stack_deterministic("/nonexistent/definitely/not/here")
    assert p.get("scan_failed") is True
    assert p["confidence"] == 0.0
    assert p["needs_model_adjudication"] is False, "空证据绝不能交给 LLM 裁决"


def test_empty_dir_is_also_scan_failed():
    """目录可读但零文件（空目录/权限拒绝/挂载未就绪）与路径不可读同性质。"""
    with tempfile.TemporaryDirectory() as d:
        assert detect_stack_deterministic(d).get("scan_failed") is True


def test_real_project_is_not_scan_failed():
    """闸不能矫枉过正——真项目必须照常出画像。"""
    p = detect_stack_deterministic(str(__import__("pathlib").Path(__file__).parent.parent))
    assert not p.get("scan_failed")
    assert p["confidence"] > 0.5


def test_detect_stack_node_drops_scan_failed_profile():
    """★对称反证在同一文件里就有：baseline_lombok_present 早有 isdir 守卫★
    节点侧必须同时断掉"下发/缓存/裁决"三条链，只断一条另两条会重新接上。"""
    import inspect

    from swarm.brain import planning_nodes
    src = inspect.getsource(planning_nodes.detect_stack)
    i_detect = src.index("detect_stack_deterministic(proj_path)")
    i_guard = src.index('profile.get("scan_failed")')
    i_cache = src.index("update_project(pid, config=cfg)")
    assert i_detect < i_guard < i_cache, "scan_failed 守卫必须在写缓存之前"


# ══════════════════════════════════════════════
# C-9：prompt 裸切片——切在结构中间 + 截断对模型不可见
# ══════════════════════════════════════════════

def test_clip_never_cuts_inside_a_markdown_table_row():
    """★实测切点落在 `| 渠道类型 | Slack / 企`（26 号文 C-9）★
    模型收到的不是"被截短的需求"，而是一份看起来完整、实则中途断掉的畸形文档。"""
    doc = ("总述段落。\n\n"
           "| 渠道类型 | 说明 |\n|---|---|\n| Slack | 企业协作 |\n| 邮件 | 系统通知 |\n\n"
           "第二章内容。\n\n") * 20
    out = clip_for_prompt(doc, 400, what="需求原文")
    body = out.split("（⚠️")[0].rstrip()
    for line in body.splitlines():
        if line.startswith("|"):
            assert line.rstrip().endswith("|"), f"表格行被腰斩: {line!r}"


def test_clip_tells_the_model_it_was_truncated():
    """截断必须显式告知——模型知道自己看到的是节选，才可能说"信息不足"而不是自信补全。"""
    out = clip_for_prompt("甲" * 5000, 500)
    assert "5000 字符" in out and "未展示" in out


def test_clip_is_noop_when_within_limit():
    """未超长绝不挂"节选"帽子（否则每个 prompt 都多一句噪声且暗示模型信息不全）。"""
    assert clip_for_prompt("短文本", 500) == "短文本"
    assert clip_for_prompt("", 500) == ""
    assert clip_for_prompt(None, 500) == ""


def test_clip_hard_cuts_when_no_boundary_available():
    """整段无边界的长文（如一行 JSON）→ 硬切，但尾注仍如实说明，不静默。"""
    out = clip_for_prompt("x" * 3000, 500)
    assert len(out.split("（⚠️")[0].rstrip()) <= 500
    assert "未展示" in out


_MARK_EVERY = 500


def _marked_requirement(total_chars: int) -> str:
    """造一段带位置标记的长需求：每 500 字符埋一个 `MARK_04500` 形式的锚。

    有了锚就能机读判断「prompt 里到底进了原文的前多少字符」——这是区分
    「走 clip（9000）」与「裸切片（2000）」的唯一确定性判据。
    """
    parts: list[str] = []
    pos = 0
    while pos < total_chars:
        head = f"MARK_{pos:05d} 需求条目：系统应支持第 {pos // _MARK_EVERY} 项能力。"
        # ★必须补齐到恰好 _MARK_EVERY 字符★：否则锚的编号与真实字符偏移不符
        # （每块仅 ~31 字符时 MARK_08000 其实躺在第 500 字符处），锚就失去判据意义。
        pad = _MARK_EVERY - len(head) - 1
        assert pad >= 0, "标记行本身超过一个区块长度"
        parts.append(head + "填" * pad + "\n")
        pos += _MARK_EVERY
    out = "".join(parts)
    assert len(out) == (total_chars // _MARK_EVERY) * _MARK_EVERY, \
        f"锚偏移自检失败: {len(out)}"
    return out


@pytest.mark.asyncio
async def test_module_level_prompts_carry_far_more_than_bare_slice(monkeypatch):
    """★决定"这个模块建哪些文件"的节点此前只看到需求前 2000 字符（丢约 85%）★

    ★行为级★（29 号文 T-A10）：原实现断 `src.count('_clip(task_desc') >= 3`，实测恰为 3
    ⇒ **恰在界上**：任何人新增第 4 个调用点后，守卫立刻失去一颗牙（可删一处仍绿）且
    无任何信号；三条 `not in` 负向断言也能用 `task_desc[:2000 ]`（多一空格）或中间
    变量绕过。这里改为断**进 prompt 的实际内容**：埋位置锚，验原文前 8000+ 字符确实
    到了 prompt，且截断处有如实尾注。
    """
    import swarm.brain.planning_nodes as pn

    req = _marked_requirement(12000)
    seen: list[str] = []

    class _Resp:
        def __init__(self, content):
            self.content = content

    class _CapturingLLM:
        async def ainvoke(self, msgs):
            seen.append("\n".join(m["content"] for m in msgs))
            if "consumer_map" in msgs[0]["content"]:      # Stage A 骨架
                return _Resp('{"skeleton": {"conventions": [], "constants": [], "consumer_map": []}}')
            return _Resp('{"interfaces": [], "dtos": [], "apis": [], "dependencies": []}')

    monkeypatch.setattr(pn, "_get_brain_llm", lambda: _CapturingLLM())
    await pn.contract_design({
        "complexity": "ultra",
        "tech_design": {"modules": [{"name": "channel", "responsibility": "渠道"},
                                    {"name": "engine", "responsibility": "引擎"}],
                        "data_model": "x"},
        "task_description": req,
    })

    assert seen, "夹具前提失效：一次 LLM 调用都没发生，下面的断言会 vacuous 通过"
    # 前提锁：需求原文真的超过了任何一个 clip 上限（否则「未截断」是因为它本来就短）
    assert len(req) > 9000, f"夹具需求仅 {len(req)} 字符，不足以触发截断路径"
    # ★逐条相等锁，不是 `any` 下界★（复核 M-1① + 我自己的突变实验修正）：
    # 初版写的是 `if "MARK_00000" not in prompt: continue` + 循环外一条 `any(...)`。
    # 实测仍不够——`contract_design` 有**两个**需求原文注入点（骨架段 `:2594`、逐模块段
    # `:2689`），只把其中一个置空时：那条 prompt 被 `continue` 跳过、`any` 由**另一个站点**
    # 满足 ⇒ 依旧全绿（两站点互相背书）。故改为断【带需求原文的 prompt 条数 == 全部条数】：
    # 任一站点被置空/退回裸切片都会让计数掉下来。
    with_req = [p for p in seen if "MARK_00000" in p]
    assert len(with_req) == len(seen), (
        f"{len(seen)} 条 prompt 里只有 {len(with_req)} 条带需求原文 ⇒ 有注入点被置空/绕过"
        "（两个站点会互相背书，所以这里必须按条数相等断，不能用 any 下界）"
    )

    for prompt in with_req:
        # 裸 [:2000] 只能带到 MARK_01500 左右；走 clip(6000/9000) 必然带到 MARK_05500 以后
        assert "MARK_05500" in prompt, (
            "prompt 只带了需求前 ~2000 字符 ⇒ 裸切片复发（决定建哪些文件的节点看不到 85% 需求）"
        )
        # 截断必须如实标注（绝不静默丢内容）
        assert "未展示" in prompt, "超长需求被截断却无尾注 ⇒ LLM 会以为看到了全文并凭猜补全"
        # 用 len(req) 而非写死 "12000"（hunter F1 附带建议）：把 clip 上限抬到 >len(req) 后
        # 需求不再被截断，此时该红成"尾注缺失"而不是"数字对不上"——写死字面量会把
        # 失败原因指向错误方向。
        assert f"共 {len(req)} 字符" in prompt, "尾注应如实报出原文总字符数"


@pytest.mark.asyncio
async def test_tech_design_stage2_prompt_carries_full_requirement(monkeypatch):
    """站点③ `_tech_design_staged._gen_one_module`（`planning_nodes.py:1496`，上限 9000）。

    ★这是原 finding 与 `brain/prompt_clip.py` docstring 指名的**那个**节点★
    （"决定这个模块要建哪些文件的 STAGE2 此前只拿到需求前 2000 字符，丢约 85%"），
    而它的最外层是 `_tech_design_staged`，**不在 `contract_design` 的驱动路径上**
    ⇒ 上面那条行为测试覆盖不到它（复核 M-1②，已用 AST 归属核实）。
    此前它唯一的守卫是 AST 字面量锁——锁的是 `9000` 这个数，不是"文本真的进了 prompt"。
    """
    import json

    import swarm.brain.planning_nodes as pn

    req = _marked_requirement(12000)
    seen: list[str] = []

    class _Resp:
        def __init__(self, content):
            self.content = content

    class _StagedLLM:
        """第 1 次调用回 STAGE1 模块清单，之后回 STAGE2 单模块 file_plan。"""

        def __init__(self):
            self.n = 0

        async def ainvoke(self, msgs):
            self.n += 1
            seen.append("\n".join(m["content"] if isinstance(m, dict) else str(m)
                                  for m in msgs))
            if self.n == 1:
                return _Resp(json.dumps({
                    "architecture": "分层", "data_model": "DM",
                    "modules": [{"name": "modA", "responsibility": "A"}],
                }, ensure_ascii=False))
            return _Resp(json.dumps({
                "file_plan": [{"path": "modA/A.java", "action": "create",
                               "module": "modA", "purpose": "p"}],
            }, ensure_ascii=False))

    monkeypatch.setattr(pn, "_format_knowledge", lambda state: "")
    await pn._tech_design_staged(_StagedLLM(), req, "ultra", False, {}, "facts", "", "")

    assert len(seen) >= 2, f"应至少发生 STAGE1+STAGE2 两次调用，实得 {len(seen)}"
    stage2 = [p for p in seen[1:] if "MARK_00000" in p]
    assert stage2, (
        "STAGE2 的 prompt 里一条都没有需求原文 ⇒ 决定「这个模块建哪些文件」的节点看不到需求"
    )
    for prompt in stage2:
        assert "MARK_05500" in prompt, (
            "STAGE2 只拿到需求前 ~2000 字符 ⇒ 裸切片复发（丢约 85% 需求，原病本体）"
        )
        assert "未展示" in prompt, "超长需求被截断却无尾注 ⇒ LLM 会以为看到了全文并凭猜补全"


def test_prompt_clip_limits_never_regress_below_6000():
    """三个需求原文注入点的上限不得被静默调小（下界断言族的正向锁）。

    用 AST 取**实参字面量**而非子串计数：新增第 4 个调用点会让集合变化 ⇒ 红，
    调小任一上限 ⇒ 红。断的是接线事实（谁以什么上限走 clip），非实现细节。
    """
    import ast
    import inspect
    import pathlib

    from swarm.brain import planning_nodes

    src = pathlib.Path(inspect.getsourcefile(planning_nodes)).read_text(encoding="utf-8")
    limits: list[int] = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "_clip"):
            continue
        # 只看第一个实参是 task_desc 的调用（需求原文注入点）
        if not (node.args and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "task_desc"):
            continue
        assert len(node.args) >= 2 and isinstance(node.args[1], ast.Constant), \
            f"第 {node.lineno} 行的 clip 上限不是字面量，本锁无法核验"
        limits.append(int(node.args[1].value))

    # 当前三处：_gen_one_module(9000) / contract_design(9000) / _gen_one_module_contract(6000)
    assert sorted(limits) == [6000, 9000, 9000], (
        f"需求原文注入点的上限集合变了（实得 {sorted(limits)}）。"
        "新增注入点或调小上限都会走到这里——请确认新点也走 clip 且上限 >= 6000，再更新本锁。"
    )


# ══════════════════════════════════════════════
# G-H6：多构建清单冲突无裁决——"首个 walk 命中即定栈"
# ══════════════════════════════════════════════

def _mk_project(manifests, sources):
    import pathlib
    import tempfile
    t = tempfile.mkdtemp()
    for name, content in manifests:
        pathlib.Path(t, name).write_text(content)
    for rel in sources:
        p = pathlib.Path(t, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    return t


_POM = ("pom.xml", "<project/>")
_GOMOD = ("go.mod", "module x")
_PKG = ("package.json", "{}")


def test_multi_manifest_no_longer_picks_by_walk_order():
    """★实测 pom + go.mod + package.json 并存 → `backend='go' confidence=0.75
    needs_adj=False` 的【高置信错答案】（26 号文 G-H6）★
    后果链已 grep 实证：给 Java 工程下发 `go build ./...`。
    walk 序取决于文件系统枚举顺序——等于用随机数定栈。"""
    p = _mk_project([_POM, _GOMOD, _PKG],
                    [f"src/main/java/{n}.java" for n in "ABC"])
    r = detect_stack_deterministic(p)
    assert r["backend"].lower().startswith(("java", "spring")) or "java" in r["backend"].lower()
    assert r["build"] == "maven"


def test_real_go_project_still_detected():
    """闸不能矫枉过正：真 Go 工程（有 .go 源文件）照常判 go。"""
    r = detect_stack_deterministic(_mk_project([_GOMOD, _PKG], ["a.go", "b.go"]))
    assert r["backend"].lower().startswith("go") and r["build"] == "go"


def test_tie_yields_low_confidence_and_adjudication():
    """★分不出胜负时绝不硬选★——那正是"高置信错答案"的来源。
    降置信 + needs_model_adjudication，让模型据证据裁而不是让 walk 序裁。"""
    r = detect_stack_deterministic(_mk_project([_POM, _GOMOD], []))
    assert r["confidence"] <= 0.4
    assert r["needs_model_adjudication"] is True
    assert any("多个构建清单" in e for e in r["evidence"]), "冲突必须写进证据面"


def test_single_manifest_path_is_unchanged():
    """单一清单（绝大多数项目）逐字节零变化。"""
    r = detect_stack_deterministic(_mk_project([_POM], ["src/main/java/A.java"]))
    assert r["build"] == "maven" and r["needs_model_adjudication"] is False


def test_same_language_multiple_manifests_is_not_a_conflict():
    """pom + build.gradle 是【构建工具】之争不是【栈】之争——不该触发歧义裁决。"""
    r = detect_stack_deterministic(_mk_project(
        [_POM, ("build.gradle", "")], ["src/main/java/A.java"]))
    assert r["needs_model_adjudication"] is False


def test_lang_source_ext_table_covers_every_manifest_language():
    """★两张表必须同步（新增语言时一起加）★
    裁决依赖 `_LANG_SOURCE_EXTS`，漏一个语言 = 该语言在冲突里恒得 0 分、必然输掉。"""
    from swarm.brain.stack_detect import _LANG_SOURCE_EXTS, _MANIFEST_BACKEND
    missing = {lang for lang, _b in _MANIFEST_BACKEND.values()} - set(_LANG_SOURCE_EXTS)
    assert not missing, f"_LANG_SOURCE_EXTS 漏登记语言：{missing}（它们在多清单冲突里恒输）"


# ══════════════════════════════════════════════
# G-H11：考卷同源对账是 Maven 独有——★本轮只做"缺席可辨"，重设计待拍板★
# ══════════════════════════════════════════════
#
# `reconcile_template_exam` 用正则锚 `pom` 字面 + ```xml 围栏识别权威模板，于是对
# npm/go/cargo 是**永久 no-op**——而"没有可对账的模板"与"有模板但我认不出来"在日志上
# 逐字不可分（正是方法论硬检查④要防的形态）。
# 补齐需要每栈一套"权威模板 → 考卷断言"驱动（package.json deps → `grep '"x":'`、
# go.mod → module 断言、Cargo.toml → …），属**重设计而非逐点补**（26 号文原文建议），
# 需先拍板范围。本轮只做一件不可省的事：**让缺席变成机读可辨**。

def _plan_with(desc):
    class _S:
        def __init__(self, d):
            self.description = d
    class _P:
        def __init__(self, x):
            self.subtasks = x
    return _P([_S(desc)])


def _fence(path: str, body: str = "x") -> str:
    """与生产注入的【权威 X 模板】围栏块同形（散文提及≠模板证据）。"""
    return (f"【权威 {path.rsplit('/', 1)[-1]} 模板（确定性生成，原样写入 {path}）】"
            f"\n```\n{body}\n```")


def test_template_manifest_detected_from_real_fence_block():
    """权威模板落点必须能被认出（判据与 reconcile 同源=_extract_auth_templates，
    返回落点 basename——driver 按清单名注册，「认不得」按同一粒度报）。"""
    from swarm.brain.contract_utils import _authoritative_template_manifests as f
    assert f(_plan_with(_fence("web/package.json"))) == {"package.json"}
    assert f(_plan_with(_fence("svc/go.mod"))) == {"go.mod"}
    assert f(_plan_with(_fence("svc/requirements.txt"))) == {"requirements.txt"}


def test_maven_manifest_detected():
    """pom.xml 在驱动表内——判据本身也要能认出它（否则差集恒真、误报"不支持"）。"""
    from swarm.brain.contract_utils import _authoritative_template_manifests as f
    assert f(_plan_with(_fence("ruoyi-admin/pom.xml"))) == {"pom.xml"}


def test_prose_mention_is_not_template_evidence():
    """★P-H2 R2 reviewer F3★ 散文提到 package.json（甚至带「原样写入」字样）但没有
    真实围栏块 = 零模板证据——治前的第二口子串扫描会把它认成 npm 模板，把同一描述里
    真 unsupported 落点（stack.toml）的 G-H11 告警压掉。"""
    from swarm.brain.contract_utils import _authoritative_template_manifests as f
    assert f(_plan_with("参考 package.json 写法，原样写入下述权威模板")) == set()
    mixed = "参考 package.json 写法\n" + _fence("svc/stack.toml")
    assert f(_plan_with(mixed)) == {"stack.toml"}, \
        "散文里的 package.json 绝不许压掉 stack.toml 的真落点"


def test_no_template_no_noise():
    """无权威模板的普通子任务 → 空集，绝不产生噪声告警。"""
    from swarm.brain.contract_utils import _authoritative_template_manifests as f
    assert f(_plan_with("实现 AlarmService 的发送逻辑")) == set()


def test_unsupported_stack_absence_is_reported():
    """★接线事实：不支持的落点必须在咽喉处告警 + record_degrade★
    否则这个机制会对 driver 表外的栈永久静默失效——
    "空返回是正常返回"正是 norms 层死了 12 天没人知道的同一个形态。
    （P-H2 多栈化后：支持集=_EXAM_DRIVERS 键域单一事实源，键名随之改如实。）"""
    import inspect

    from swarm.brain import contract_utils
    src = inspect.getsource(contract_utils.inject_build_scaffold_subtasks)
    assert "_authoritative_template_manifests" in src
    assert "stack_unsupported" in src
