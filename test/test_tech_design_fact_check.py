"""tech_design 事实核验数据源函数测试（不起 LLM，测纯逻辑）。"""
import os
import tempfile

from swarm.brain.planning_nodes import _gather_project_facts, _verify_named_files_exist


def _mk_ruoyi_like():
    d = tempfile.mkdtemp()
    for sub, fn in [
        ("src/controller", "UserController.java"),
        ("src/service", "IUserService.java"),
        ("src/service/impl", "UserServiceImpl.java"),
        ("src/mapper", "UserMapper.java"),
        ("src/domain", "User.java"),
    ]:
        os.makedirs(os.path.join(d, sub), exist_ok=True)
        with open(os.path.join(d, sub, fn), "w") as f:
            f.write("// stub\n")
    return d


def test_gather_facts_finds_layered_samples():
    d = _mk_ruoyi_like()
    facts = _gather_project_facts(d)
    assert "controller" in facts.lower() or "UserController" in facts
    assert "样例文件" in facts


def test_gather_facts_no_path():
    out = _gather_project_facts(None)
    assert "无项目路径" in out or "沙箱实地确认" in out


def test_verify_existing_file():
    d = _mk_ruoyi_like()
    r = _verify_named_files_exist("给 UserController.java 加方法", d)
    hit = [x for x in r if x["file"] == "UserController.java"]
    assert hit and hit[0]["exists"] is True


def test_verify_nonexistent_file_false_premise():
    """虚假前提：bac.java 不存在 → exists False（应触发澄清）。"""
    d = _mk_ruoyi_like()
    r = _verify_named_files_exist("在 bac.java 顶部加注释", d)
    hit = [x for x in r if x["file"] == "bac.java"]
    assert hit and hit[0]["exists"] is False


def test_verify_no_named_file():
    """需求未点名文件（产品式）→ 返回空，不误判。"""
    d = _mk_ruoyi_like()
    r = _verify_named_files_exist("做个功能管一下设备", d)
    assert r == []


def test_verify_no_path_noop():
    assert _verify_named_files_exist("改 X.java", None) == []


# ── 第二批-2 多源仲裁 + 可信度 ──
def test_verify_returns_confidence():
    """核验结果带 confidence 字段。"""
    d = _mk_ruoyi_like()
    r = _verify_named_files_exist("给 UserController.java 加方法", d)
    hit = [x for x in r if x["file"] == "UserController.java"][0]
    assert "confidence" in hit and "sources" in hit


def test_verify_git_tracked_file_recognized():
    """git 已跟踪但工作区被临时删的文件 → git 源仍命中（不误判不存在）。"""
    import subprocess
    d = _mk_ruoyi_like()
    subprocess.run(["git", "-C", d, "init", "-q"], check=True)
    subprocess.run(["git", "-C", d, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", d, "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", d, "add", "."], check=True)
    subprocess.run(["git", "-C", d, "commit", "-qm", "init"], check=True)
    # 模拟 VERIFY_L2 reset 临时删工作区文件，但 git 里还在
    import os
    os.remove(os.path.join(d, "src/controller/UserController.java"))
    r = _verify_named_files_exist("改 UserController.java", d)
    hit = [x for x in r if x["file"] == "UserController.java"][0]
    assert hit["exists"] is True, "git 跟踪的文件应被认存在（不靠工作区单源）"
    assert "git" in hit["sources"]


def test_verify_both_sources_high_confidence():
    """磁盘+git 双命中 → confidence high。"""
    import subprocess
    d = _mk_ruoyi_like()
    subprocess.run(["git", "-C", d, "init", "-q"], check=True)
    subprocess.run(["git", "-C", d, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", d, "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", d, "add", "."], check=True)
    subprocess.run(["git", "-C", d, "commit", "-qm", "init"], check=True)
    r = _verify_named_files_exist("改 User.java", d)
    hit = [x for x in r if x["file"] == "User.java"][0]
    assert hit["confidence"] == "high" and set(hit["sources"]) == {"disk", "git"}


def test_verify_existing_returns_real_path_for_correction():
    """核验已存在文件 → candidates[0] 是真实路径，供 file_plan 路径校正用（bug 9bd1d5b5）。

    任务1 把 HealthController 建在 monitor/，任务2 LLM 可能猜成 common/。
    核验必须返回真实路径 monitor/...，让确定性后处理覆盖 LLM 猜的路径。
    """
    import os
    d = _mk_ruoyi_like()
    # 模拟任务1产出：monitor 目录下的 HealthController
    os.makedirs(os.path.join(d, "src/controller/monitor"), exist_ok=True)
    with open(os.path.join(d, "src/controller/monitor/HealthController.java"), "w") as f:
        f.write("// health\n")
    r = _verify_named_files_exist("给 HealthController.java 加 version 字段", d)
    hit = [x for x in r if x["file"] == "HealthController.java"][0]
    assert hit["exists"] is True
    assert hit["candidates"], "已存在文件必须返回真实路径"
    assert "monitor" in hit["candidates"][0], f"真实路径应指向 monitor/: {hit['candidates']}"


def test_stack_mismatch_filter_doc_vs_disk_not_blocked():
    """治本（用户原则"不以文档为准"）：磁盘已权威定栈后，PRD 的框架假设与磁盘不一致 =
    栈差异，必须【适配不阻断】，哪怕描述里含"不存在/没有"。实测 RuoYi retry 卡死真因回归。"""
    from swarm.brain.planning_nodes import _is_stack_mismatch_issue as f
    # 卡死本体：PRD 说 Vue，磁盘是 Thymeleaf，detail 含"不存在" → 必须判为栈差异(剔除)
    vue = {"claim": "PRD 提到'前端：Vue 页面'",
           "detail": "磁盘事实：162 个 .html、0 个 .vue，无独立前端工程。PRD 假设的 Vue SPA 在本项目中不存在。"}
    assert f(vue) is True
    # React/SPA 同理
    assert f({"claim": "PRD 假设 React SPA", "detail": "项目无 .jsx，前端为服务端模板"}) is True
    # 真·缺文件/缺类（无框架关键词）→ 仍按虚假前提保留阻断，不被误剔除
    real = {"claim": "PRD 要求修改 com.ruoyi.alarm.AlarmController",
            "detail": "该类在项目中不存在，找不到对应文件"}
    assert f(real) is False
    # 空文本不剔除
    assert f({"claim": "", "detail": ""}) is False


def test_after_tech_design_blocks_only_grounded_false_premises():
    """治本：block 必须确定性坐实——只阻断 grounded=True（磁盘坐实点名文件缺失）；
    纯 LLM 框架/栈/语义 verdict=false(grounded=False) 降级 advisory 不阻断。"""
    from swarm.brain.graph import after_tech_design as rt
    vue = {"claim": "PRD 提到 Vue", "detail": "项目是 Thymeleaf，Vue 不存在",
           "verdict": "false", "grounded": False}
    miss = {"claim": "需求点名文件 X.java", "detail": "磁盘核验：不存在",
            "verdict": "false", "grounded": True}
    assert rt({"tech_design_fact_issues": [vue]}) == "review_design"        # 框架差异不阻断
    assert rt({"tech_design_fact_issues": [miss]}) == "clarify"             # 坐实缺文件阻断
    assert rt({"tech_design_fact_issues": [vue, miss]}) == "clarify"        # 有坐实即阻断
    assert rt({"tech_design_fact_issues": []}) == "review_design"
    # 缺 grounded 字段（旧 checkpoint 兼容）= 视为未坐实 → 不阻断（保守不误杀自动化）
    legacy = {"claim": "PRD 提到 Vue", "verdict": "false"}
    assert rt({"tech_design_fact_issues": [legacy]}) == "review_design"
