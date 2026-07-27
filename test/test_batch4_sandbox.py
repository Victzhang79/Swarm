#!/usr/bin/env python3
"""批次4（19号文 B 簇 + 21 号文 W-3/W-5）沙箱生命周期簇治本测试。

覆盖：B4 共享清单降级盲覆盖 WARNING / B5 空文件三态 / B6 ls 兜底列错位 /
B7 package.json workspaces 共享清单保护 / B9 模板查询认证头收敛 /
B10 walk run_command 兜底 / B11 嵌套目录对称 / B12 LOW 五件套 /
W-3 tarball 静默 skip WARNING / W-5 kill_sandbox 异常路径兜底。
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest


# ── 公共假件 ──────────────────────────────────────────────────────────────


class _FakeFiles:
    def __init__(self):
        self.writes: list[tuple[str, bytes]] = []
        self.dirs: list[str] = []

    def write(self, path, data):
        self.writes.append((path, data))

    def make_dir(self, path):
        self.dirs.append(path)


class _FakeSandbox:
    def __init__(self, sid="sb-1"):
        self.sandbox_id = sid
        self.files = _FakeFiles()


def _mgr_stub():
    """裸 SandboxManager（绕过 __init__ 的 env/sidecar），只挂测试需要的属性。"""
    from swarm.worker.sandbox import SandboxManager

    mgr = object.__new__(SandboxManager)
    mgr.config = SimpleNamespace(
        sandbox_remote_workdir="/workspace", verify_ssl=True, api_url="", api_key="",
    )
    mgr._fail_counts = {}
    mgr._sandbox_meta = {}
    mgr._sandbox_activity = {}
    mgr._resolved_templates = {}
    mgr._sandbox_deadlines = {}
    return mgr


# ── B4：共享清单 flock+merge 降级盲覆盖必须 WARNING ───────────────────────


def test_b4_manifest_blind_overwrite_emits_warning(tmp_path, monkeypatch, caplog):
    """flock/merge 抛异常 → 降级盲覆盖照旧落盘（fail-open 不阻断），但必须 WARNING
    可观测（治前零日志，并发修复蒸发复发不可见）。"""
    import swarm.worker.sandbox as sb_mod
    import swarm.worker.executor as ex_mod

    mgr = _mgr_stub()
    monkeypatch.setattr(
        sb_mod, "read_file_from_sandbox",
        lambda sandbox, path, manager=None: b"<project><dependencies/></project>",
    )

    class _BoomFlock:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            raise RuntimeError("lock backend down")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ex_mod, "_ProjectGitFlock", _BoomFlock)
    with caplog.at_level(logging.WARNING):
        stats = mgr.sync_files_from_sandbox(_FakeSandbox(), tmp_path, ["pom.xml"])
    assert stats["downloaded"] == 1, f"降级盲覆盖仍应落盘: {stats}"
    assert (tmp_path / "pom.xml").is_file()
    assert any("降级为盲覆盖" in r.message for r in caplog.records), \
        "降级路径必须 WARNING（B4）"


# ── B5：read_file shell 兜底空文件三态 ────────────────────────────────────


def _run_cmd_returns(stdout):
    def _rc(sandbox, cmd, timeout=30):
        return SimpleNamespace(stdout=stdout, stderr="", exit_code=0)
    return _rc


def test_b5_empty_file_returns_empty_bytes():
    """合法空文件（__init__.py/.gitkeep）→ 确定性返回 b""，不误判错误丢文件。"""
    mgr = _mgr_stub()
    mgr.run_command = _run_cmd_returns("__EMPTY_FILE__")
    from swarm.worker.sandbox import read_file_from_sandbox

    class _Bare:  # 无 download_url / files 属性 → 直达 shell 兜底
        pass

    assert read_file_from_sandbox(_Bare(), "/w/pkg/__init__.py", manager=mgr) == b""


def test_b5_missing_file_still_raises():
    """不存在/目录 → 照旧 RuntimeError（三态不混淆）。"""
    mgr = _mgr_stub()
    mgr.run_command = _run_cmd_returns("__NOT_A_FILE__")
    from swarm.worker.sandbox import read_file_from_sandbox

    class _Bare:
        pass

    with pytest.raises(RuntimeError, match="not a file"):
        read_file_from_sandbox(_Bare(), "/w/nope.py", manager=mgr)


def test_b5_not_found_error_feeds_stop_spin_signal():
    """★hunter R1 HIGH-1 契约★：shell 兜底 not-found 异常必须命中
    file_tools._is_not_found_err——否则 worker 丢"请勿反复重读"止转信号、
    复活跨子任务文件同步空转（文案漂移曾静默击穿分类，此处跨模块锁死）。"""
    mgr = _mgr_stub()
    mgr.run_command = _run_cmd_returns("__NOT_A_FILE__")
    from swarm.worker.sandbox import read_file_from_sandbox
    from swarm.tools.file_tools import _handle_read_miss, _is_not_found_err

    class _Bare:
        pass

    with pytest.raises(RuntimeError) as ei:
        read_file_from_sandbox(_Bare(), "/w/zzz_never_exists_qq.py", manager=mgr)
    assert _is_not_found_err(ei.value), \
        f"sandbox not-found 文案必须命中 _is_not_found_err 键表: {ei.value}"
    reply = _handle_read_miss("zzz_never_exists_qq.py", 1, 10, ei.value)
    assert "请勿反复重读" in reply, f"必须给止转信号而非通用瞬时错误: {reply}"


# ── B6：list_files ls 兜底 6 列解析 ──────────────────────────────────────


def test_b6_ls_fallback_parses_six_columns():
    """--time-style=+ 输出 6 列(perms links owner group size name)——按 5 列解包
    会把 group 当 size（恒 0）、"size name" 当文件名（假路径）。"""
    mgr = _mgr_stub()
    ls_out = (
        "total 8\n"
        "-rw-r--r-- 1 root root 4096 app.py\n"
        "drwxr-xr-x 2 root root 4096 pkg/\n"
        "-rw-r--r-- 1 root root 12 with space.txt\n"
    )
    mgr.run_command = _run_cmd_returns(ls_out)
    mgr._instances = {"sb-1": _FakeSandbox("sb-1")}

    class _NoListFiles:
        pass

    mgr._instances["sb-1"].files = _NoListFiles()  # files.list 不存在 → 走 ls 兜底
    files = mgr.list_files("sb-1", "/workspace")
    by_name = {f["name"]: f for f in files}
    assert "app.py" in by_name, f"文件名不得带 size 前缀: {sorted(by_name)}"
    assert by_name["app.py"]["size"] == 4096
    assert "pkg" in by_name and by_name["pkg"]["is_dir"] is True
    assert "with space.txt" in by_name, "带空格文件名应完整保留"
    assert by_name["with space.txt"]["size"] == 12


# ── B7：package.json workspaces 共享清单保护 ─────────────────────────────


def test_b7_is_shared_manifest_package_json_content_based(tmp_path):
    """按内容判定：含 workspaces 键的根 package.json → 共享清单；子包 package.json
    （无 workspaces）→ 不纳入（不扩大锁面）。"""
    from swarm.worker.sandbox import _is_shared_manifest, _is_shared_manifest_on_disk

    agg = b'{"name": "root", "workspaces": ["packages/*"]}'
    sub = b'{"name": "sub-pkg", "dependencies": {}}'
    assert _is_shared_manifest("package.json", agg) is True
    assert _is_shared_manifest("package.json", sub) is False
    assert _is_shared_manifest("packages/a/package.json", sub) is False
    # 纯 rel 旧调用面（无 content）→ 保守 False，行为不变
    assert _is_shared_manifest("package.json") is False
    # 非法 JSON → 保守 False
    assert _is_shared_manifest("package.json", b"{oops") is False
    # on_disk 版
    (tmp_path / "package.json").write_bytes(agg)
    subdir = tmp_path / "packages" / "a"
    subdir.mkdir(parents=True)
    (subdir / "package.json").write_bytes(sub)
    assert _is_shared_manifest_on_disk("package.json", tmp_path) is True
    assert _is_shared_manifest_on_disk("packages/a/package.json", tmp_path) is False
    # basename 命中（pom.xml 等）不需要 content
    assert _is_shared_manifest_on_disk("ruoyi-admin/pom.xml", tmp_path) is True


def test_b7_npm_workspaces_merge_union():
    """npm 合并驱动：local 独有的 workspaces 成员并回 incoming（防陈旧副本盲覆盖
    丢注册）；无缺失 → 原样返回（零 diff churn）。"""
    from swarm.worker.workspace_manifest import merge_shared_manifest

    local = '{\n  "name": "root",\n  "workspaces": ["packages/a", "packages/b"]\n}\n'
    incoming = '{\n  "name": "root",\n  "workspaces": ["packages/a"]\n}\n'
    merged = merge_shared_manifest(local, incoming, "package.json")
    import json as _json
    assert sorted(_json.loads(merged)["workspaces"]) == ["packages/a", "packages/b"], merged
    # 无缺失 → 原样
    assert merge_shared_manifest(incoming, incoming, "package.json") == incoming
    # 对象形 workspaces {"packages": [...]}
    local_obj = '{"workspaces": {"packages": ["x", "y"]}}'
    inc_obj = '{"workspaces": {"packages": ["x"]}}'
    merged_obj = merge_shared_manifest(local_obj, inc_obj, "package.json")
    assert sorted(_json.loads(merged_obj)["workspaces"]["packages"]) == ["x", "y"]
    # incoming 无 workspaces 键 → 保守不臆造结构
    assert merge_shared_manifest(local, '{"name": "r"}', "package.json") == '{"name": "r"}'


# ── B9：模板查询认证头收敛（单一封装，双头并发）───────────────────────────


def test_b9_query_templates_sends_both_auth_headers(monkeypatch):
    """同一 /templates 端点历史两套头（Bearer vs X-API-KEY）→ 收敛后一次请求双头
    并发，服务端认其一即可。"""
    import swarm.worker.sandbox as sb_mod

    sent: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return [{"templateID": "tpl-a", "status": "READY"}]

    def _fake_get(url, headers=None, timeout=None, verify=None):
        sent["url"] = url
        sent["headers"] = dict(headers or {})
        return _Resp()

    import httpx
    monkeypatch.setattr(httpx, "get", _fake_get)
    cfg = SimpleNamespace(api_url="http://cube:8080/", api_key="k-1", verify_ssl=False)
    items = sb_mod.query_cubemaster_templates(cfg)
    assert items == [{"id": "tpl-a", "status": "READY", "imageInfo": ""}]
    assert sent["url"] == "http://cube:8080/templates"
    assert sent["headers"].get("X-API-KEY") == "k-1"
    assert sent["headers"].get("Authorization") == "Bearer k-1"


def test_b9_template_exists_uses_shared_wrapper(monkeypatch):
    """image_builder 探活改走同一封装（行为一致：存在→True，查不到→False，失败→None）。"""
    import swarm.worker.sandbox as sb_mod
    from swarm.worker import image_builder as ib

    monkeypatch.setattr(
        sb_mod, "query_cubemaster_templates",
        lambda cfg, timeout=5.0: [{"id": "tpl-a", "status": "READY", "imageInfo": ""}],
    )
    cfg = SimpleNamespace(api_url="http://cube:8080", api_key="k", verify_ssl=True)
    monkeypatch.setattr(ib, "get_config", lambda: SimpleNamespace(sandbox=cfg), raising=False)
    from swarm.config import get_config as _real_gc  # noqa: F401 — 占位防误删
    # image_builder 内 from swarm.config import get_config → 需 patch 该模块属性
    import swarm.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "get_config", lambda: SimpleNamespace(sandbox=cfg))
    assert ib.template_exists_in_cubemaster("tpl-a") is True
    assert ib.template_exists_in_cubemaster("tpl-gone") is False


# ── B10：walk run_command 兜底 ────────────────────────────────────────────


def test_b10_walk_falls_back_to_shell_find(tmp_path, monkeypatch):
    """语言镜像无 Jupyter kernel → run_code walk 502；补 shell find 兜底后照常拉回，
    不再确定性 downloaded=0 静默丢全部产出。"""
    import swarm.worker.sandbox as sb_mod

    mgr = _mgr_stub()

    class _CodeResult:
        error = "502 kernel unavailable"
        stdout = ""
        stderr = "502"

    mgr.run_code = lambda sandbox, code, timeout=120: _CodeResult()
    mgr.run_command = _run_cmd_returns("/workspace/src/a.py\n/workspace/src/b.py\n__WALK_RC__0")
    monkeypatch.setattr(
        sb_mod, "read_file_from_sandbox",
        lambda sandbox, path, manager=None: b"print(1)\n",
    )
    stats = mgr.sync_sandbox_to_local(_FakeSandbox(), tmp_path, remote_root="/workspace")
    assert stats["downloaded"] == 2, f"shell 兜底应拉回两个文件: {stats}"
    assert (tmp_path / "src" / "a.py").read_bytes() == b"print(1)\n"


def test_b10_walk_shell_rc_nonzero_recorded_as_error(tmp_path, monkeypatch):
    """find 失败（RC 非 0）→ 如实记 errors（区分"find 失败"与"目录为空"）。"""
    import swarm.worker.sandbox as sb_mod

    mgr = _mgr_stub()

    class _CodeResult:
        error = "502"
        stdout = ""
        stderr = ""

    mgr.run_code = lambda sandbox, code, timeout=120: _CodeResult()
    mgr.run_command = _run_cmd_returns("__WALK_RC__1")
    stats = mgr.sync_sandbox_to_local(_FakeSandbox(), tmp_path, remote_root="/workspace")
    assert stats["downloaded"] == 0
    assert stats["errors"], "walk 双失败必须入账"


# ── B11：嵌套目录 _ensure_remote_dir 对称 ────────────────────────────────


def test_b11_write_remote_file_ensures_parent_dir():
    """envd files.write 不保证自动建父目录——嵌套路径必须先 make_dir（治前 tar 批量
    失败回退逐文件时嵌套文件全报错，worker 在半截项目树上编译）。"""
    mgr = _mgr_stub()
    sb = _FakeSandbox()
    mgr._write_remote_file(sb, "/workspace/src/deep/mod/a.py", b"x", True)
    assert sb.files.dirs == ["/workspace/src/deep/mod"], sb.files.dirs
    assert sb.files.writes == [("/workspace/src/deep/mod/a.py", b"x")]


def test_b11_write_remote_file_flat_path_skips_redundant_mkdir():
    """根下平铺文件：父目录=remote_root（调用方已 ensure），不重复建。"""
    mgr = _mgr_stub()
    sb = _FakeSandbox()
    mgr._write_remote_file(sb, "a.py", b"x", True)
    assert sb.files.dirs == [], sb.files.dirs
    assert sb.files.writes == [("a.py", b"x")]


# ── B12：LOW 五件套 ──────────────────────────────────────────────────────


def test_b12_fail_counts_cleared_on_unregister():
    """熔断计数随 unregister 清理（长活进程无界增长 + 池复用跨借用者续存双治）。"""
    mgr = _mgr_stub()
    mgr._fail_counts["sb-1"] = 3
    mgr.unregister_sandbox_meta("sb-1")
    assert "sb-1" not in mgr._fail_counts


def test_b12_bucket_key_config_error_warns(caplog, monkeypatch):
    """_bucket_key 读配置异常 → 退化无隔离桶键必须 WARNING（治前静默丢 project 隔离）。"""
    from swarm.worker.sandbox_pool import HotSandboxPool
    import swarm.config.settings as settings_mod

    pool = object.__new__(HotSandboxPool)
    monkeypatch.setattr(
        settings_mod, "get_config",
        lambda: (_ for _ in ()).throw(RuntimeError("config backend down")),
    )
    with caplog.at_level(logging.WARNING):
        key = pool._bucket_key("tpl-a", "proj-1")
    assert key == "tpl-a"  # 退化行为不变
    assert any("project 隔离" in r.message for r in caplog.records)


def test_b12_dependency_fingerprint_error_returns_random(tmp_path, monkeypatch, caplog):
    """指纹扫描异常 → 随机指纹（永不匹配=强制重建，不误复用陈旧模板）+ WARNING。"""
    import os

    from swarm.worker import image_builder as ib

    monkeypatch.setattr(
        os, "walk", lambda *a, **k: (_ for _ in ()).throw(OSError("disk gone")))
    with caplog.at_level(logging.WARNING):
        fp1 = ib._dependency_fingerprint(tmp_path)
        fp2 = ib._dependency_fingerprint(tmp_path)
    assert fp1.startswith("err-") and fp2.startswith("err-")
    assert fp1 != fp2, "异常指纹必须随机（恒定空指纹=误复用闩锁）"
    assert any("强制重建" in r.message for r in caplog.records)


def test_b12_activity_jsonl_rotation(tmp_path, monkeypatch):
    """activity JSONL 超上限截尾轮转（磁盘缓慢泄漏止血），文件保持有界。"""
    mgr = _mgr_stub()
    mgr._ACTIVITY_JSONL_MAX_BYTES = 200
    monkeypatch.setattr(
        type(mgr), "_activity_log_dir", staticmethod(lambda: tmp_path))
    big = "x" * 120
    for i in range(6):
        mgr._persist_activity("sb-1", {"i": i, "pad": big})
    fp = tmp_path / "sb-1.jsonl"
    assert fp.is_file()
    assert fp.stat().st_size <= 300, f"轮转后应有界: {fp.stat().st_size}"
    tail = fp.read_text()
    assert '"i": 5' in tail, "最新条目必须保留"


# ── W-3：tarball 静默 skip 补 WARNING ────────────────────────────────────


def test_w3_tarball_oversize_skip_warns(tmp_path, monkeypatch, caplog):
    """>阈值文件被 skip 必须 WARNING（治前零日志，镜像缺大文件→沙箱假编译错无迹可查）。
    非 git 路径（rglob）+ 阈值调小验证。"""
    import tarfile
    import io as _io

    from swarm.worker import image_builder as ib

    monkeypatch.setattr(ib, "_TARBALL_MAX_FILE_BYTES", 100)
    (tmp_path / "big.sql").write_bytes(b"x" * 200)
    (tmp_path / "small.py").write_bytes(b"print(1)")
    with caplog.at_level(logging.WARNING):
        blob = ib._make_source_tarball(tmp_path)
    assert any("跳过超限文件" in r.message for r in caplog.records), "skip 必须 WARNING"
    with tarfile.open(fileobj=_io.BytesIO(blob), mode="r:gz") as tar:
        names = tar.getnames()
    assert "small.py" in names
    assert "big.sql" not in names


# ── W-5：kill_sandbox 异常路径另一通道兜底 ────────────────────────────────


class _KillStub:
    """挂 _SandboxLifecycleMixin 最小假件。"""

    def __init__(self):
        from swarm.worker.executor_lifecycle import _SandboxLifecycleMixin

        class _W(_SandboxLifecycleMixin):
            def _log(self, msg, level="info"):
                pass

        self._w = _W()


def test_w5_pool_release_failure_falls_back_to_manager_kill():
    """pool.release 抛异常 → 置空引用前 manager.kill 兜底销毁（治前幽灵泄漏）。"""
    from swarm.worker.executor_lifecycle import _SandboxLifecycleMixin

    class _W(_SandboxLifecycleMixin):
        def _log(self, msg, level="info"):
            pass

    w = _W()
    sb = _FakeSandbox("sb-x")
    killed: list[str] = []

    class _Pool:
        def release(self, sandbox, reusable=False):
            raise RuntimeError("pool backend down")

    w._sandbox = sb
    w._from_pool = True
    w._sandbox_pool = _Pool()
    w._sandbox_manager = SimpleNamespace(kill=lambda sid: killed.append(sid))
    w.kill_sandbox()
    assert killed == ["sb-x"], "释放失败必须经 manager.kill 兜底"
    assert w._sandbox is None


def test_w5_non_pool_path_kill_failure_still_nulls_refs(caplog):
    """非池借用：manager.kill 双失败（主+兜底同一通道）→ WARNING 入账（幽灵泄漏待
    reap 兜底），引用照常置空不卡死。hunter R1-L4：pool.release 兜底分支已删
    （对外来沙箱错扣 borrowed 计数，且本不可达）。"""
    import logging as _lg

    from swarm.worker.executor_lifecycle import _SandboxLifecycleMixin

    class _W(_SandboxLifecycleMixin):
        def _log(self, msg, level="info"):
            pass

    w = _W()
    sb = _FakeSandbox("sb-y")
    calls: list[str] = []

    def _boom_kill(sid):
        calls.append(sid)
        raise RuntimeError("kill api down")

    w._sandbox = sb
    w._from_pool = False
    w._sandbox_pool = None
    w._sandbox_manager = SimpleNamespace(kill=_boom_kill)
    with caplog.at_level(_lg.WARNING):
        w.kill_sandbox()
    assert calls == ["sb-y", "sb-y"], "主路径+兜底各试一次 manager.kill"
    assert any("双通道均失败" in r.message for r in caplog.records)
    assert w._sandbox is None
