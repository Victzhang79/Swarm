"""主题I H-5（外部深审 HIGH）：项目根敏感目录黑名单必须覆盖 realpath 归一形态。

病根：_reject_sensitive 先 realpath 再比对，但旧黑名单只有字面 "/etc"。macOS 上
/etc→/private/etc（firmlink），norm("/private/etc") 既不 =="/etc" 也不 startswith
"/etc/" → 绕过 → 可把项目根指向宿主敏感目录（developer 越权读写服务账号可达目录）。
治：黑名单条目本身也过 realpath 并入集（Linux 上 realpath(/etc)==/etc 无变化，平台通用）。
"""
from __future__ import annotations

import os

from swarm.api.routers.project import _path_is_sensitive, _sensitive_dir_set


def test_h5_sensitive_set_includes_realpath_forms():
    """平台通用不变量：每个敏感目录的 realpath 形态都在集里（macOS 才有差异）。"""
    s = _sensitive_dir_set()
    for d in ("/etc", "/var/run", "/usr"):
        assert os.path.realpath(d) in s, f"{d} 的 realpath 形态必须在黑名单（否则 macOS 绕过）"


def test_h5_etc_rejected_even_via_realpath():
    """核心：无论 /etc 的 realpath 是 /etc(Linux) 还是 /private/etc(macOS) 都判敏感。"""
    assert _path_is_sensitive("/etc") is True
    assert _path_is_sensitive("/etc/passwd") is True
    # 直接给 realpath 后的形态也必须拦（macOS 上就是这个）
    assert _path_is_sensitive(os.path.realpath("/etc")) is True


def test_h5_subpath_of_sensitive_rejected():
    assert _path_is_sensitive("/usr/local/bin") is True
    assert _path_is_sensitive("/root/.ssh") is True


def test_h5_normal_project_path_allowed():
    # /tmp→/private/tmp 不在敏感列表；用户目录、空路径同样放行
    assert _path_is_sensitive("/tmp/some_project") is False
    assert _path_is_sensitive(os.path.expanduser("~/myproject")) is False
    assert _path_is_sensitive("") is False


if __name__ == "__main__":
    print("run via pytest")
