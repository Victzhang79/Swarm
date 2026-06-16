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
