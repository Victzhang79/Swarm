"""30 号文批22 锁：knowledge/experience 面 LEAD 三件。

- F-3：_prune_absent_files 候选集改读源头表 kb_file_index 且三表同清——原实现读
  符号表+只清符号/依赖表，幽灵行在 kb_file_index 永不清理，list_inventory（A7
  baseline 候选通道）把磁盘已删文件当「确定性现状」喂 Brain。
- F-6：入库闸 hidden_dir 放行 CI 定义目录（.github/workflows/.circleci）+ 根级 CI
  文件（.gitlab-ci.yml/.drone.yml）——「隐藏=噪声」对 CI 知识是误杀；短白名单封闭集。
- F-7：技能库缓存带文件指纹（mtime_ns+size），drop-in 对运行中进程即时生效。
"""
from __future__ import annotations

from pathlib import Path

from swarm.knowledge import ingest_guard as ig
from swarm.experience import service as svc


# ── F-3：幽灵行三表同清 ─────────────────────────────────────

class _FakeCur:
    def __init__(self, file_index_rows):
        self.file_index_rows = file_index_rows
        self.sql: list[tuple[str, tuple]] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchall(self):
        # 候选集 SELECT：必须读 kb_file_index（F-3 前读 kb_symbol_index=漏零符号文件）
        return [(r,) for r in self.file_index_rows]


class _FakeConn:
    def __init__(self, cur):
        self.cur = cur
        self.txn_used = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.cur

    def transaction(self):
        """R1 折（reviewer HIGH-1 + hunter F1）：三条 DELETE 必须包在显式事务里
        （autocommit 池上裸跑=中途失败造「永不复访的幽灵行」）。fake 计数供锁断言。"""
        self.txn_used += 1
        return self


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    def connection(self):
        return self._ConnCtx(self.conn)

    class _ConnCtx:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            return self.conn

        def __exit__(self, *a):
            return False


def _run_prune(monkeypatch, tmp_path: Path, indexed_rows: list[str], project: str = "p1"):
    """真跑 _prune_absent_files，DB 走 fake（mock 落盘 sink 纪律：绝不写真 PG）。"""
    from swarm.infra import db as _db
    from swarm.project import preprocess as pp

    cur = _FakeCur(indexed_rows)
    conn = _FakeConn(cur)
    monkeypatch.setattr(_db, "sync_pool", lambda: _FakePool(conn))
    n = pp._prune_absent_files(project, str(tmp_path))
    return n, cur.sql, conn


def test_prune_absent_reads_file_index_and_clears_three_tables(monkeypatch, tmp_path):
    """F-3 主锁：幽灵行（磁盘已删）必须从【源头表 kb_file_index UNION 符号表】出候选并三表同清。
    候选SELECT 摘掉任一表支、或摘 kb_file_index 的 DELETE，本锁红（双向都有牙）。"""
    (tmp_path / "alive.py").write_text("x = 1\n", encoding="utf-8")
    n, sql, conn = _run_prune(monkeypatch, tmp_path, ["alive.py", "ghost.py", "zero_symbol.md"])
    assert n == 2, f"幽灵行应=2（ghost.py + 零符号 zero_symbol.md），实 {n}"
    assert conn.txn_used == 1, "三条 DELETE 必须包在一次显式事务里（autocommit 非原子=R1 HIGH-1）"
    select_sql = sql[0][0]
    assert "FROM kb_file_index" in select_sql, \
        f"候选集必须含源头表 kb_file_index（只读符号表会漏零符号文件）: {select_sql}"
    # R2 折（hunter LOW-1 双保）：符号表 UNION 支也必须在——只读 file_index 会漏
    # 「codegraph 外部 CLI 与扫描层不对齐」产的孤儿符号行（旧覆盖被收窄=回归）。
    assert "FROM kb_symbol_index" in select_sql and "UNION" in select_sql, \
        f"候选集必须 UNION 符号表保住旧覆盖: {select_sql}"
    deletes = [s for s, _ in sql[1:]]
    assert any("DELETE FROM kb_file_index" in s for s in deletes), \
        f"kb_file_index 幽灵行未被清理（F-3 原病灶）: {deletes}"
    assert any("DELETE FROM kb_symbol_index" in s for s in deletes)
    assert any("DELETE FROM kb_dependency_graph" in s for s in deletes)
    for s, params in sql[1:]:
        if "kb_file_index" in s or "kb_symbol_index" in s:
            assert params[1] == ["ghost.py", "zero_symbol.md"], \
                f"删除集必须恰为幽灵集（不误伤存活文件）: {params}"
        if "kb_dependency_graph" in s:
            # R1 折（hunter F5③）：dep 表 DELETE 的参数逐字断——(pid, absent, absent)，
            # 参数顺序/内容漂移（如换成存活集）必须红。
            assert params == ("p1", ["ghost.py", "zero_symbol.md"], ["ghost.py", "zero_symbol.md"]), \
                f"dep 表删除参数必须逐字为 (project, absent, absent): {params}"


def test_prune_absent_failclosed_on_bad_project_path(monkeypatch, tmp_path):
    """F-3 反向钉（P1-25 原 fail-closed 保持 + R1 契约改）：project_path 不是现存目录 ⇒
    零 DB 访问返回 None（机读可辨「未对账」，绝不按「全不存在」清整表）。"""
    from swarm.infra import db as _db
    from swarm.project import preprocess as pp

    touched = []
    monkeypatch.setattr(_db, "sync_pool", lambda: touched.append(1) or None)
    n = pp._prune_absent_files("p1", str(tmp_path / "no_such_dir"))
    assert n is None and not touched, "坏路径必须 fail-closed 拒绝对账（None=未对账，机读可辨）"


def test_prune_absent_noop_when_all_present(monkeypatch, tmp_path):
    """反向钉 2：全部文件存活 ⇒ 零 DELETE（误杀方向锁），返回 0=真没有（与 None 可分）。"""
    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
    n, sql, _c = _run_prune(monkeypatch, tmp_path, ["a.py"])
    assert n == 0
    assert not any("DELETE" in s for s, _ in sql), f"存活文件被误删: {sql}"


# ── F-6：CI 定义白名单 ─────────────────────────────────────

def test_ci_definition_paths_allowed():
    """F-6 主锁：CI 定义不再是 hidden 噪声。摘 _CI_DIR_ALLOW 放行支或 _HIDDEN_FILE_ALLOW
    两个 CI 文件名，本锁红。"""
    assert ig.reject_reason_by_name(".github/workflows/ci.yml") is None
    assert ig.reject_reason_by_name(".github/workflows/deploy/prod.yaml") is None
    assert ig.reject_reason_by_name(".circleci/config.yml") is None
    assert ig.reject_reason_by_name(".gitlab-ci.yml") is None
    assert ig.reject_reason_by_name(".drone.yml") is None


def test_hidden_gate_scope_unchanged_outside_ci():
    """F-6 反向钉：白名单范围刻意只到 CI 定义——.github 其余面与普通隐藏路径照拒。"""
    assert ig.reject_reason_by_name(".github/ISSUE_TEMPLATE/bug.md") == "hidden_dir"
    assert ig.reject_reason_by_name(".github/dependabot.yml") == "hidden_dir"
    assert ig.reject_reason_by_name(".claude/plans/x.md") == "hidden_dir"
    # R1 折（hunter F3）：白名单只豁免前缀段本身——前缀下再嵌隐藏目录不是 CI 知识
    assert ig.reject_reason_by_name(".github/workflows/.cache/token.txt") == "hidden_dir"
    assert ig.reject_reason_by_name(".circleci/.hidden/x.txt") == "hidden_dir"
    assert ig.reject_reason_by_name(".env") == "sensitive_filename"
    assert ig.reject_reason_by_name(".env.production") is not None
    # 凭据目录闸（另一消费契约）不受 CI 白名单影响
    assert ig.credential_reject_reason(".ssh/config") == "credential_dir"


def test_full_scan_walk_enumerates_ci_dirs(tmp_path):
    """F-6 接线锁（R1 折 reviewer M-1 + hunter F2 双透镜同洞）：全量 preprocess 的
    walk 剪枝必须放行根层 CI 前缀目录——否则名字闸的 CI 放行在主发现通道是死代码。
    摘 _scan_sync 的 _CI_WALK_KEEP 支，本锁红（ci.yml 消失）。"""
    from swarm.project.preprocess import _scan_sync

    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")
    (tmp_path / ".github" / "ISSUE_TEMPLATE").mkdir()
    (tmp_path / ".github" / "ISSUE_TEMPLATE" / "bug.md").write_text("x\n", encoding="utf-8")
    (tmp_path / ".circleci").mkdir()
    (tmp_path / ".circleci" / "config.yml").write_text("version: 2\n", encoding="utf-8")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "x.md").write_text("x\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")

    result = _scan_sync(str(tmp_path))
    paths = {f["rel_path"] for f in result["files"]}
    assert any(p.replace("\\", "/") == ".github/workflows/ci.yml" for p in paths), \
        f"CI 定义在全量通道没进索引（walk 剪枝死机制复发）: {sorted(paths)}"
    assert any(p.replace("\\", "/") == ".circleci/config.yml" for p in paths)
    assert "main.py" in paths
    # 反向钉：非白名单子树照拒（名字闸 hidden_dir），其余隐藏目录仍被 walk 剪掉
    assert not any("ISSUE_TEMPLATE" in p for p in paths), ".github 其余面必须照拒"
    assert not any(p.startswith(".claude") for p in paths), ".claude 必须仍被剪"
    assert result["ingest_rejected_by_name"] >= 1  # ISSUE_TEMPLATE/bug.md 被拒留痕


# ── F-7：技能库指纹热加载 ───────────────────────────────────

def _write_skill(path: Path, sid: str, title: str) -> None:
    path.write_text(
        f"---\nid: {sid}\ntitle: {title}\ndescription: \"测试技能 {title} 的判别依据\"\n"
        f"target: [worker]\n---\n- x\n",
        encoding="utf-8",
    )


def test_skills_dropin_effective_without_restart(tmp_path, monkeypatch):
    """F-7 主锁：运行中 drop-in/修改/删除即生效（不重启、不 invalidate_cache）。
    摘 _dirs_fingerprint 比对（恢复无指纹缓存），本锁红。"""
    svc.invalidate_cache()
    d = tmp_path / "skills"
    d.mkdir()
    _write_skill(d / "a.md", "a", "A")
    dirs = [str(d)]
    assert [s.id for s in svc._load_cached(dirs)] == ["a"]
    _write_skill(d / "b.md", "b", "B")          # drop-in 新增
    assert [s.id for s in svc._load_cached(dirs)] == ["a", "b"], "新增技能未热生效"
    _write_skill(d / "a.md", "a", "A2")          # 修改
    docs = svc._load_cached(dirs)
    assert next(s for s in docs if s.id == "a").title == "A2", "修改未热生效"
    (d / "b.md").unlink()                        # 删除
    assert [s.id for s in svc._load_cached(dirs)] == ["a"], "删除未热生效"
    svc.invalidate_cache()  # 卫生：不留 tmp 路径 key 在进程级缓存


def test_skills_cache_no_reload_when_unchanged(tmp_path, monkeypatch):
    """F-7 反向钉（性能面）：指纹不变 ⇒ 不重读盘（load_skills_from 只调一次）。"""
    svc.invalidate_cache()
    d = tmp_path / "skills"
    d.mkdir()
    _write_skill(d / "a.md", "a", "A")
    calls = []
    real = svc.load_skills_from

    def _spy(key):
        calls.append(1)
        return real(key)

    monkeypatch.setattr(svc, "load_skills_from", _spy)
    svc._load_cached([str(d)])
    svc._load_cached([str(d)])
    svc._load_cached([str(d)])
    assert len(calls) == 1, f"无变化重复加载 {len(calls)} 次（指纹缓存失效）"
    svc.invalidate_cache()
