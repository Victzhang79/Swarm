"""根因(task 9bd1d5b5): SIMPLE 快速路径 _build_simple_plan 定位文件时，
retrieved(知识库检索)滞后导致用 LLM 猜的错路径，应回退查 git/磁盘 ground truth。"""
import os
import subprocess
import tempfile

from swarm.brain.nodes.shared import _build_simple_plan


def _mk_repo_with_committed_file():
    """模拟任务1：HealthController 建在 monitor/ 并 commit，但知识库(retrieved)还没索引。"""
    d = tempfile.mkdtemp()
    sub = "ruoyi-admin/src/main/java/com/ruoyi/web/controller/monitor"
    os.makedirs(os.path.join(d, sub), exist_ok=True)
    with open(os.path.join(d, sub, "HealthController.java"), "w") as f:
        f.write("@RestController\npublic class HealthController {}\n")
    subprocess.run(["git", "-C", d, "init", "-q"], check=True)
    subprocess.run(["git", "-C", d, "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", d, "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", d, "add", "."], check=True)
    subprocess.run(["git", "-C", d, "commit", "-qm", "task1"], check=True)
    return d


def test_simple_plan_uses_ground_truth_when_retrieved_stale():
    """retrieved 为空(知识库滞后)，但 git 有 monitor/HealthController → scope 应用真实路径。"""
    d = _mk_repo_with_committed_file()
    # 需求点名裸文件名 HealthController.java；retrieved(affected_files) 空 = 知识库还没索引
    plan = _build_simple_plan(
        "给 HealthController.java 增加 version 字段",
        affected_files=[],          # 知识库滞后，检索不到
        project_path=d,
    )
    st = plan.subtasks[0]
    all_scope = (st.scope.writable or []) + (st.scope.create_files or [])
    # 必须解析到真实路径 monitor/，而非裸名或猜的 common/
    assert any("monitor/HealthController.java" in p for p in all_scope), \
        f"应从 ground truth 解析到真实路径 monitor/: {all_scope}"
    # 已存在文件 → 应归 writable(modify) 而非 create
    assert any("HealthController" in p for p in (st.scope.writable or [])), \
        f"已存在文件应归 writable: writable={st.scope.writable} create={st.scope.create_files}"


def test_simple_plan_wrong_guessed_path_corrected():
    """LLM 猜错目录 common/HealthController.java → 应被 ground truth 真实路径 monitor/ 覆盖。"""
    d = _mk_repo_with_committed_file()
    plan = _build_simple_plan(
        "修改 ruoyi-admin/src/main/java/com/ruoyi/web/controller/common/HealthController.java 加字段",
        affected_files=[],
        project_path=d,
    )
    st = plan.subtasks[0]
    all_scope = (st.scope.writable or []) + (st.scope.create_files or [])
    assert any("monitor/HealthController.java" in p for p in all_scope), \
        f"猜错的 common/ 路径应被真实 monitor/ 覆盖: {all_scope}"
    assert not any("common/HealthController" in p for p in all_scope), \
        f"不应保留猜错的 common/ 路径: {all_scope}"


def test_simple_plan_genuinely_new_file_stays_create():
    """真新建文件(ground truth 也没有) → 保持 create，不误判。"""
    d = _mk_repo_with_committed_file()
    plan = _build_simple_plan(
        "新建 NewWidget.java 实现某功能",
        affected_files=[],
        project_path=d,
    )
    st = plan.subtasks[0]
    # NewWidget 不存在 → 不应出现在 writable(modify)；要么 create 要么 allow_any
    assert not any("NewWidget" in p for p in (st.scope.writable or [])), \
        "不存在的文件不应判为 modify"
