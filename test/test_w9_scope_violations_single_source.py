"""#29-5 W-9：_scope_violations 必须与 FileScope.is_writable（单一事实源）同结论。

修复前两处反向（findings 29 号文第七节 W-9，均已实测）：
- 方向二（冤杀）：全函数不读 allow_any。allow_any=True + 非空 writable 的子任务
  新建任何文件都被判越权 → L1.1 硬判死。生产可达：plan 节点主链的
  shared._merge_horizontal_subtasks（brain/nodes/__init__.py:2921 调用）把
  allow_any 取 any(...)、writable 取并集，可产出 allow_any=True+非空 writable。
- 方向一（假过）：`if not allowed: return []` 把空写权集 fail-open 成全放行。
  而 scope_guard 对同一 FileScope 是 is_writable=False（fail-closed）；worker 在
  沙箱里跑的 shell 命令不过 scope_guard，这道闸是唯一防线，空写权集必须 fail-closed。

每条断言的判据：把 _scope_violations 改回旧实现（无视 allow_any / 空集 return []），
对应测试必须红。
"""
from swarm.types import FileScope, SubTask
from swarm.worker.l1_pipeline import _scope_violations, run_l1_pipeline

_NEW = "--- /dev/null\n+++ b/src/new.py\n@@ -0,0 +1 @@\n+print(1)\n"
_MOD = "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n+changed\n"


# ── 方向二：allow_any=True 不得冤杀 ──

def test_allow_any_with_nonempty_writable_lets_new_file_through():
    """主链水平合并可达的形态：allow_any=True + writable 非空 → 新建文件不判越权。

    旧实现（无视 allow_any，只按 allowed 集匹配）下 'src/new.py' 必进 violations → 本测试红。
    """
    scope = FileScope(allow_any=True, writable=["src/app.py"])
    v = _scope_violations(_NEW + _MOD, scope)
    assert v == [], f"allow_any=True 不应有任何越权: {v}"


def test_allow_any_greenfield_empty_lists_lets_everything_through():
    """greenfield 常态（shared.py:435：三列表全空时 allow_any=True）→ 全放行。"""
    scope = FileScope(allow_any=True)
    assert _scope_violations(_NEW + _MOD, scope) == []


# ── 方向一：空写权集 fail-closed ──

def test_empty_scope_any_output_is_violation():
    """FileScope() 全空且 allow_any=False：任何产出都是越权（与 scope_guard 同结论）。

    旧实现 `if not allowed: return []` 下本测试必红（返 [] 零违规）。
    """
    scope = FileScope()
    v = _scope_violations(_MOD, scope)
    assert "src/app.py" in v


def test_empty_scope_empty_diff_no_violation():
    """fail-closed 只抓【有产出】：空 diff 无文件可判，不得凭空造违规。"""
    scope = FileScope()
    assert _scope_violations("", scope) == []


# ── round18 P0-B 不退化：extra_allowed 仍放行确定性修复触达 ──

def test_extra_allowed_still_honored_with_empty_scope():
    """空写权集 + extra_allowed：修复触达的文件放行，worker 真越权的仍被抓。"""
    scope = FileScope()
    pom = "--- a/pom.xml\n+++ b/pom.xml\n@@ -1 +1 @@\n+    <module>x</module>\n"
    v = _scope_violations(pom + _MOD, scope, extra_allowed={"pom.xml"})
    assert "pom.xml" not in v
    assert "src/app.py" in v


def test_extra_allowed_still_honored_with_allow_any():
    """allow_any + extra_allowed 并存不冲突（extra 是冗余但绝不能判死）。"""
    scope = FileScope(allow_any=True, writable=["src/app.py"])
    assert _scope_violations(_MOD, scope, extra_allowed={"pom.xml"}) == []


# ── 端到端：经 run_l1_pipeline 的 L1.1 阶段 ──

def test_pipeline_allow_any_scope_passes_l1_1(tmp_path):
    """端到端接线锁：allow_any=True 的子任务带新建文件 diff → l1_1_scope_ok=True。"""
    st = SubTask(id="st-w9", description="greenfield", scope=FileScope(allow_any=True))
    ok, details = run_l1_pipeline(str(tmp_path), st, _NEW)
    assert details["l1_1_scope_ok"] is True, details.get("scope_violations")
    assert details["scope_violations"] == []


def test_pipeline_empty_scope_fails_l1_1(tmp_path):
    """端到端接线锁：空写权集子任务有产出 → L1.1 判死（fail-closed 到达裁决面）。"""
    st = SubTask(id="st-w9b", description="no write grant", scope=FileScope())
    ok, details = run_l1_pipeline(str(tmp_path), st, _MOD)
    assert ok is False
    assert details["l1_1_scope_ok"] is False
    assert "src/app.py" in details["scope_violations"]


def test_scope_gate_death_carries_reason_for_failure_signature(tmp_path):
    """判死三件套锁（hunter 复核）：缺 reason 时 _failure_signature 兜底键集取空 ⇒
    签名恒空 ⇒ no-progress 早停对重复 scope 违规永不触发（白烧 fix 轮）。"""
    st = SubTask(id="st-w9c", description="no write grant", scope=FileScope())
    ok, details = run_l1_pipeline(str(tmp_path), st, _MOD)
    assert ok is False
    assert details["reason"] == "scope_violation"
    assert details["note"]
    from swarm.worker.executor_l1gate import _L1GateMixin
    assert _L1GateMixin._failure_signature(details), "scope 判死必须产出非空失败签名"


# ── 匹配器同源锁：_scope_violations 主臂走 is_writable(_path_scope_match)，
#    extra_allowed 臂走 _scope_match——两份匹配器漂移即重新制造冤杀/假过（hunter 复核）──

_MATCH_MATRIX = [
    ("src/a.py", "src/a.py"),          # 完全相等
    ("src/a.py", "src/"),              # 目录 scope
    ("src/sub/a.py", "src"),           # 祖先目录段
    ("repo/src/a.py", "src/a.py"),     # 多段 scope 容忍根前缀
    ("2src/a.py", "src/a.py"),         # 边界非分隔符 → 双 False
    ("src/main.py", "main.py"),        # 单段 basename 不尾匹配（audit #31）
    ("./src/a.py", "src/a.py"),        # ./ 归一
    ("src\\a.py", "src/a.py"),         # 反斜杠归一
    ("", "src/a.py"),                  # 空 fp → 双 False
    ("src/a.py", ""),                  # 空 w → 双 False
]


def test_scope_matchers_stay_equivalent():
    """_path_scope_match(types.py) 与 _scope_match(l1_pipeline.py) 必须逐点同结论。

    判据：改掉任一匹配器的任一分支，本测试必须红。
    """
    from swarm.types import _path_scope_match
    from swarm.worker.l1_pipeline import _scope_match
    for fp, w in _MATCH_MATRIX:
        assert _path_scope_match(fp, w) == _scope_match(fp, w), (fp, w)
