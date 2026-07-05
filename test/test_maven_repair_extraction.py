"""god-file 簇B-1：Maven 依赖补全簇从 nodes/__init__ 拆出后的可寻址契约（行为）。

守约束：①经 __init__ re-export 保 swarm.brain.nodes.X 可寻址；②maven_repair 不反向依赖
nodes/__init__（自包含，无环）。
"""
from __future__ import annotations

import subprocess
import sys


def test_reexport_identity():
    import swarm.brain.nodes as n
    import swarm.brain.nodes.maven_repair as m

    for name in ("_pkg_match_tokens", "_extract_missing_pkgs", "_iter_project_poms",
                 "_find_maven_dep_for_pkg", "_inject_dep_into_pom", "_inject_missing_maven_deps",
                 "_ARTIFACT_RE", "_DEP_BLOCK_RE", "_GROUP_RE", "_MAVEN_GENERIC_SEG",
                 "_MISSING_PKG_BRAIN_RE"):
        assert getattr(n, name) is getattr(m, name), f"{name} 未经 __init__ re-export"


def test_maven_repair_importable_standalone():
    r = subprocess.run(
        [sys.executable, "-c",
         "import importlib; mod = importlib.import_module('swarm.brain.nodes.maven_repair'); "
         "assert hasattr(mod, '_inject_missing_maven_deps'); print('ok')"],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_pkg_match_tokens_behavior():
    from swarm.brain.nodes.maven_repair import _pkg_match_tokens

    assert _pkg_match_tokens("org.quartz") == ["quartz"]
    # 通用段 org/com 去掉，数字后缀变体保留
    assert _pkg_match_tokens("okhttp3.client") == ["okhttp3", "okhttp", "client"]


def test_extract_missing_pkgs_behavior():
    from swarm.brain.nodes.maven_repair import _extract_missing_pkgs

    blob = "error: 程序包 org.quartz 不存在\npackage com.foo.bar does not exist"
    got = _extract_missing_pkgs(blob)
    assert "org.quartz" in got and "com.foo.bar" in got
