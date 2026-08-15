"""30 号文批26 锁：B-2c 全局未捕获异常 500 的 traceback 节流 + B-2d [PENDING] 同型点
+ get_must_change_password 裸 except 收窄（批21 hunter 建议②既有债）。

- B-2c：PG 全宕期 /progress 轮询端点的 `store.get_task` 在 try 外先抛（批24 hunter M-1
  定位）→ 未捕获 → uvicorn 每请求一条完整 traceback 洗版。治法=api/app.py 全局
  exception_handler：500 语义保持诚实（绝不装 404/空 200），每请求一行 ERROR，
  完整 traceback 经 log_throttle 60s 心跳一条带抑制计数（首个现场完整留证）。
- B-2d：runner.get_pending_interrupt 的 [PENDING] 快照读失败 WARNING 接同一原语
  （选择触发非周期，但审核期多客户端叠加仍可成簇）。
- auth：get_must_change_password 原裸 except Exception 把 PG 宕机也吞成 False
  =强制改密被静默绕过（fail-open）。收窄：仅 UndefinedColumn（旧库未迁移列）降级
  False+节流 WARNING，其余异常上抛 fail-closed。
"""
from __future__ import annotations

import asyncio
import logging

import pytest

import swarm.brain.runner as runner
import swarm.infra.log_throttle as lt


# ── B-2c：全局未捕获异常 500 的 traceback 节流 ──────────────────


def test_unhandled_exception_500_json_and_traceback_throttled(monkeypatch, caplog):
    """真驱动 /api/tasks/{id}/progress 端点（patch store.get_task 抛=模拟 PG 全宕）：
    ①两次请求都得 500 JSON（诚实语义不变，handler 真接上了=接线证明）；
    ②第一次有完整 traceback（exc_info）记录，第二次窗口内只有一行 ERROR 无新 traceback。
    删 handler 注册即红（默认 Starlette 返纯文本非本 JSON 形状，且 traceback 不节流）。
    ★边界（批26 R1 双复核 HIGH-1）★：本锁只证 app logger 侧——ServerErrorMiddleware
    恒 re-raise 后 uvicorn 层每请求另打一条全 traceback 到 uvicorn.error，TestClient
    不过 uvicorn 结构性看不见；该通道由 logging_config._ASGIExceptionThrottleFilter
    收口（本文件下方两条锁直驱 filter + 接线）。"""
    from fastapi.testclient import TestClient

    from swarm.api.app import app
    from swarm.project import store

    def _boom(tid):
        raise RuntimeError("pg down (simulated)")

    monkeypatch.setattr(store, "get_task", _boom)
    lt._reset()
    try:
        # raise_server_exceptions=False：ServerErrorMiddleware 调完 handler 恒 raise，
        # 默认 True 会把异常抛进测试（handler 已接管响应，断言对象是响应+日志）
        client = TestClient(app, raise_server_exceptions=False)
        with caplog.at_level(logging.ERROR):
            r1 = client.get("/api/tasks/t-b26/progress")
            assert r1.status_code == 500 and r1.json() == {"detail": "Internal Server Error"}, \
                f"500 语义必须保持诚实: {r1.status_code} {r1.text[:100]}"
            tb1 = [r for r in caplog.records if r.exc_info]
            assert len(tb1) == 1, f"第一次必须留完整 traceback 现场: {len(tb1)}"
            one_liners = [r for r in caplog.records if "未捕获异常 GET" in r.getMessage()]
            assert len(one_liners) == 1, "每请求一行 ERROR（路径+异常repr）"
            r2 = client.get("/api/tasks/t-b26/progress")
            assert r2.status_code == 500
            tb2 = [r for r in caplog.records if r.exc_info]
            assert len(tb2) == 1, "60s 窗口内第二次不得再打完整 traceback（节流生效）"
            one_liners2 = [r for r in caplog.records if "未捕获异常 GET" in r.getMessage()]
            assert len(one_liners2) == 2, "单行 ERROR 不节流（每请求一条，机读账不丢）"
    finally:
        lt._reset()  # 不留残余节流状态给后续用例


# ── B-2d：[PENDING] 快照读失败 WARNING 节流 ──────────────────


def test_pending_snapshot_warning_throttled(monkeypatch, caplog):
    """真驱动 get_pending_interrupt 快照失败路径两次：首条 [PENDING] WARNING 放行、
    窗口内第二次被抑制。删节流接线（退回直打 logger.warning）本锁红。"""
    lt._reset()

    class _G:
        async def aget_state(self, config):
            raise RuntimeError("pg down")

    monkeypatch.setattr(runner, "get_compiled_brain_graph", lambda: _G())
    monkeypatch.setattr(runner.store, "get_task",
                        lambda tid: {"project_id": "p", "thread_id": "th-1"})
    monkeypatch.setattr("swarm.tracing.brain_graph_config", lambda **kw: {})
    try:
        with caplog.at_level(logging.WARNING):
            assert asyncio.run(runner.get_pending_interrupt("t-b26")) is None
            first = [r for r in caplog.records if "[PENDING] 读取快照失败" in r.getMessage()]
            assert len(first) == 1, f"首次失败必须 WARNING 放行: {len(first)}"
            assert asyncio.run(runner.get_pending_interrupt("t-b26")) is None
            second = [r for r in caplog.records if "[PENDING] 读取快照失败" in r.getMessage()]
            assert len(second) == 1, "60s 窗口内第二次同站失败必须被节流抑制"
    finally:
        lt._reset()


# ── auth：get_must_change_password 裸 except 收窄 ──────────────────


class _Cur:
    """按指定异常抛出的假游标（execute 抛、fetchone 不到）。"""

    def __init__(self, exc):
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        raise self._exc


class _Conn:
    def __init__(self, exc):
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _Cur(self._exc)


def test_must_change_password_only_undefined_column_degrades(monkeypatch, caplog):
    """仅 UndefinedColumn（旧库未迁移列）降级 False+节流 WARNING；
    其余异常（PG 宕=OperationalError 代表）必须原样上抛 fail-closed——
    裸 except Exception 回退即红（=强制改密被静默绕过的 P0 方向）。"""
    import psycopg

    from swarm.auth import store as auth_store

    lt._reset()
    try:
        # ① 列缺失（旧库）：降级 False + WARNING 留痕
        monkeypatch.setattr(auth_store, "_pooled_conn", lambda conn_str=None: _Conn(
            psycopg.errors.UndefinedColumn("column must_change_password does not exist")))
        with caplog.at_level(logging.WARNING):
            assert auth_store.get_must_change_password("u1") is False
        assert any("must_change_password 列缺失" in r.getMessage() for r in caplog.records), \
            "降级路径必须留痕（铁律#3）"
        # 窗口内第二次：仍 False 但 WARNING 被抑制（节流接线证据）
        caplog.clear()
        assert auth_store.get_must_change_password("u1") is False
        assert not any("must_change_password 列缺失" in r.getMessage() for r in caplog.records), \
            "同站第二次必须被节流抑制"

        # ② PG 宕（OperationalError）：必须上抛，绝不静默 False
        monkeypatch.setattr(auth_store, "_pooled_conn", lambda conn_str=None: _Conn(
            psycopg.OperationalError("connection refused")))
        with pytest.raises(psycopg.OperationalError):
            auth_store.get_must_change_password("u1")
    finally:
        lt._reset()


# ── 批23 尾巴收口①：gradle rootDir 裸标记收紧为迭代基座邻近式 ──


def test_gradle_rootdir_static_interpolation_no_longer_dynamic():
    """批23 登记债收口：`$rootDir/legacy/...` 字符串插值是合法静态写法，不得冤判
    动态（冤判 ⇒ reconcile 不补 include 且 merge 保守直通=成员蒸发方向）。
    突变=把裸 `\brootDir\b` 加回标记表 ⇒ 本锁红。"""
    from swarm.worker import workspace_manifest as wm

    text = (
        "rootProject.name = 'demo'\n"
        "include ':core'\n"
        "project(':core').projectDir = file(\"$rootDir/legacy/core\")\n"
        # R1 复核增补静态形态：纯构造（无迭代方法）也不得冤判
        "project(':legacy').projectDir = new File(rootDir, 'legacy-x')\n"
    )
    assert wm._gradle_dynamic_hit(text) is None, "静态 rootDir 插值/纯构造冤判动态"
    assert wm.manifest_member_probes("settings.gradle", text) == [("core", "core")]
    merged = wm.merge_shared_manifest(text + "include ':api'\n", text, "settings.gradle")
    assert ":api" in merged and ":core" in merged, "静态文件必须真并成员（非直通）"


def test_gradle_rootdir_iteration_base_still_dynamic():
    """收紧不得丢召回：rootDir 作【迭代基座】的形态（bare 时代独有覆盖、eachDir/
    listFiles 裸标记抓不到的 eachFile*/eachDirRecurse/traverse/walk 系）必须仍判
    动态。突变=删 `rootDir\\s*\\.\\s*(?:each|…)` 或 `File\\s*\\(\\s*rootDir…` 邻近式 ⇒ 本锁红。"""
    from swarm.worker import workspace_manifest as wm

    for dyn in (
        "rootDir.eachFile { include it.name }\n",
        "rootDir.eachDirRecurse { d -> include d.name }\n",
        "rootDir.traverse { if (it.directory) include it.name }\n",
        "rootDir.walkTopDown().forEach { println(it) }\n",
        "rootDir.eachDir { include it.name }\n",       # eachDir 裸标记兜底系
        "rootDir.listFiles().each { include it.name }\n",  # listFiles 裸标记兜底系
        "new File(rootDir, 'libs').eachFile { include it.name }\n",  # R1：构造器包裹迭代
    ):
        assert wm._gradle_dynamic_hit(dyn) is not None, f"迭代基座漏判动态: {dyn.strip()}"


def test_gradle_rootdir_alias_two_line_shape_deliberately_ceded():
    """刻意让渡的行为钉（批26 R1 双复核 MEDIUM 证伪「召回零损」声称后如实登记）：
    别名两行形态（`def d = rootDir` 后另起行 `d.eachFile{}`）regex 级不可判（需别名
    流分析），旧裸标记只靠赋值行碰巧含 rootDir 偶然覆盖。本锁把【当前不命中】钉为
    刻意取舍——误杀方向有 reconcile WARNING 机读可辨；谁把它改到命中，必须回来
    同步本锁与 _GRADLE_DYNAMIC 注释里的让渡登记。"""
    from swarm.worker import workspace_manifest as wm

    for ceded in (
        "def d = rootDir\nd.eachFile { include it.name }\n",
        "def d = new File(rootDir, 'modules')\nd.traverse { include it.name }\n",
    ):
        assert wm._gradle_dynamic_hit(ceded) is None, \
            f"别名两行形态=登记的刻意让渡（改判须同步注释权衡）: {ceded!r}"


def test_gradle_filetree_bare_marker_deliberately_kept():
    """登记保留项的行为钉：fileTree 维持裸标记（「先赋值后遍历」形态召回优先于
    静态冤判成本，权衡见 _GRADLE_DYNAMIC 注释）——有人顺手把 fileTree 也收紧成
    邻近式时本锁红，逼其回登记册重估而非静默改判据。"""
    from swarm.worker import workspace_manifest as wm

    assert wm._gradle_dynamic_hit("fileTree(dir: 'libs', include: '*.jar')\n") is not None


# ── 批23 尾巴收口②：prune_manifest_members go.work 摘除臂引号/尾斜杠容忍 ──


def test_prune_go_work_quoted_block_line_removed():
    """批26 同药平移：go.work 词法允许引号字符串，读径 _norm_use 本就剥引号归一
    ⇒ 摘除臂必须认 `\"./svc\"` 引号行，否则残留成永久幽灵成员。
    突变=摘除臂退回无 `[\"']?`/`/?` 容忍 ⇒ 本锁红。"""
    from swarm.worker import workspace_manifest as wm

    text = 'go 1.22\n\nuse (\n\t./core\n\t"./svc"\n)\n'
    new_text, removed = wm.prune_manifest_members(
        "go.work", text, lambda probe: False if probe == "svc" else True)
    assert removed == ["svc"], removed
    assert '"./svc"' not in new_text, new_text
    assert "./core" in new_text and "use (" in new_text, "既有成员与块结构不伤"


def test_prune_go_work_single_line_quoted_crlf_comment_removed():
    """单行臂同药：`use \"./svc/\" // 注释`（引号+尾斜杠+行注释+CRLF 四复合形态）
    必须整行摘除。治前单行臂 `[ \\t]*$` 对四形态全不匹配=残留。"""
    from swarm.worker import workspace_manifest as wm

    text = 'go 1.22\r\n\r\nuse ./core\r\nuse "./svc/" // 新增\r\n'
    new_text, removed = wm.prune_manifest_members(
        "go.work", text, lambda probe: False if probe == "svc" else True)
    assert removed == ["svc"], removed
    assert "svc" not in new_text, new_text
    assert "use ./core" in new_text, "既有成员不伤"


def test_prune_go_work_prefix_sibling_still_not_eaten():
    """F1 行尾锚防回退：容忍扩展不得松绑前缀防护——摘 `svc` 时 `svc2`/`sub/svc`
    兄弟行不得被吃前缀（批23 R1 hunter F1 的死法换方向复发）。"""
    from swarm.worker import workspace_manifest as wm

    text = 'go 1.22\n\nuse (\n\t./svc\n\t./svc2\n\t"./sub/svc"\n)\n'
    new_text, removed = wm.prune_manifest_members(
        "go.work", text, lambda probe: False if probe == "svc" else True)
    assert removed == ["svc"], removed
    assert "./svc2" in new_text, f"svc2 被吃前缀:\n{new_text}"
    assert '"./sub/svc"' in new_text, f"sub/svc 引号行被误伤:\n{new_text}"
    assert "\t./svc\n" not in new_text


# ── 批26 R1 HIGH-1：uvicorn 层 ASGI 异常 traceback 节流 Filter ──


def _asgi_record(with_exc: bool = True) -> logging.LogRecord:
    """构造 uvicorn run_asgi 打出的那条 record（五个协议实现消息文本一致，
    已在 venv 源码逐字核对：h11_impl/httptools_impl/websockets×3）。"""
    import sys

    exc_info = None
    if with_exc:
        try:
            raise RuntimeError("pg down (simulated)")
        except RuntimeError:
            exc_info = sys.exc_info()
    return logging.LogRecord("uvicorn.error", logging.ERROR, "", 0,
                             "Exception in ASGI application\n", (), exc_info)


def test_asgi_exception_throttle_filter_behavior():
    """批26 R1 HIGH-1 治本：uvicorn run_asgi 每请求一条全 traceback（ServerError-
    Middleware 恒 re-raise，应用层 handler 节流够不着这条通道）。filter 判据=
    消息+exc_info 双条件：窗口内首条放行+抑制计数机读尾巴，后续 drop；非该消息/
    无 exc_info 一律放行。突变=摘挂 filter 或改坏判据 ⇒ 本锁红。"""
    from swarm.logging_config import _ASGIExceptionThrottleFilter

    lt._reset()
    try:
        f = _ASGIExceptionThrottleFilter()
        first = _asgi_record()
        assert f.filter(first) is True, "窗口内首条放行"
        assert f.filter(_asgi_record()) is False, "窗口内第二条 drop（节流生效）"
        # 非 ASGI 异常消息（启动/配置错误等）不拦
        rec = logging.LogRecord("uvicorn.error", logging.ERROR, "", 0,
                                "startup failure", (), None)
        assert f.filter(rec) is True, "非目标消息一律放行"
        # 同消息但无 exc_info 不拦（双条件判据）
        assert f.filter(_asgi_record(with_exc=False)) is True, "无 exc_info 放行"
        # ★R2 hunter LOW-c★：机读账尾巴分支（sup>0 时抑制计数并进 msg、args 置空
        # 防格式化炸）单测内跨窗口不可达——monkeypatch throttled 直驱该分支
        # （throttled 本体接线由上方首放/次 drop 两臂已证；本臂钉尾巴语义本体，
        # 漏 `record.args = ()` 或漏尾巴即红）。
        import swarm.infra.log_throttle as _lt

        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(_lt, "throttled", lambda key, **kw: 3)
            rec = _asgi_record()
            assert f.filter(rec) is True
            assert "已抑制 3 条" in rec.msg, f"抑制计数必须进机读尾巴: {rec.msg!r}"
            assert rec.args == (), "msg 改写后 args 必须置空（否则格式化炸）"
        finally:
            monkeypatch.undo()
    finally:
        lt._reset()


def test_asgi_exception_throttle_filter_wired_to_uvicorn_error():
    """接线锁：setup_logging 跑完后 uvicorn.error 必须挂着本 filter——只测单元
    不测接线正是「接线覆盖≠机制存在」的坑形（_AccessPollFilter 先例只有单元锁，
    本锁把「被接上了」钉死：删 setup_logging 里的挂接行即红）。
    ★R2 reviewer MEDIUM★：filter 经 log_throttle 全局 key 有状态，重复挂接叠出
    第二实例时首条放行被反转成全丢（实例1放行开窗口、实例2同key判窗口内=drop）
    ——本锁第二臂钉【恰 1 个实例】（删挂接处的 isinstance 查重即红）。"""
    from swarm import logging_config as lc

    lc.setup_logging(force=True, console=False)
    lc.setup_logging(force=True, console=False)  # 幂等路径：第二遍不得再叠实例
    flts = [f for f in logging.getLogger("uvicorn.error").filters
            if isinstance(f, lc._ASGIExceptionThrottleFilter)]
    assert len(flts) == 1, f"filter 必须恰挂 1 个实例（叠加=首条放行反转全丢）: {len(flts)}"
