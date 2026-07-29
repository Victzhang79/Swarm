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


def test_module_level_prompts_no_longer_use_bare_slices():
    """★决定"这个模块建哪些文件"的节点此前只看到需求前 2000 字符（丢约 85%）★
    断言的是接线事实（这些点走 clip 而非裸切片），不是实现细节。"""
    import inspect

    from swarm.brain import planning_nodes
    src = inspect.getsource(planning_nodes)
    assert "task_description=task_desc[:2000]" not in src
    assert "task_description=task_desc[:2500]" not in src
    assert "task_description=task_desc[:1500]" not in src
    assert src.count('_clip(task_desc') >= 3
