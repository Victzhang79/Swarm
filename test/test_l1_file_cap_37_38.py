"""audit #37/#38 修复回归测试：编译/lint 文件上限可配 + 超限告警。

原硬编码 [:20]，大变更集遗漏后续文件检查。改 _cap_files（SWARM_WORKER_L1_MAX_FILES
可配，默认 20，截断时 warning）。
"""

from __future__ import annotations

import os

from swarm.worker.l1_pipeline import _cap_files, _max_files_per_check


def test_default_cap_is_20():
    os.environ.pop("SWARM_WORKER_L1_MAX_FILES", None)
    assert _max_files_per_check() == 20


def test_under_cap_unchanged():
    os.environ.pop("SWARM_WORKER_L1_MAX_FILES", None)
    files = [f"f{i}.py" for i in range(5)]
    assert _cap_files(files, "test") == files


def test_over_cap_truncates():
    os.environ.pop("SWARM_WORKER_L1_MAX_FILES", None)
    files = [f"f{i}.py" for i in range(25)]
    capped = _cap_files(files, "test")
    assert len(capped) == 20
    assert capped == files[:20]


def test_env_configurable():
    os.environ["SWARM_WORKER_L1_MAX_FILES"] = "5"
    try:
        assert _max_files_per_check() == 5
        files = [f"f{i}.py" for i in range(10)]
        assert len(_cap_files(files, "test")) == 5
    finally:
        os.environ.pop("SWARM_WORKER_L1_MAX_FILES", None)


def test_invalid_env_falls_back_to_20():
    os.environ["SWARM_WORKER_L1_MAX_FILES"] = "not-a-number"
    try:
        assert _max_files_per_check() == 20
    finally:
        os.environ.pop("SWARM_WORKER_L1_MAX_FILES", None)


if __name__ == "__main__":
    import sys
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✅ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ❌ {fn.__name__}: {e}")
    print(f"\n=== #37/#38 file cap configurable: {len(fns) - failed}/{len(fns)} passed ===")
    sys.exit(1 if failed else 0)
