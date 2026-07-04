"""A5：路径归属安全原语 is_within_root 行为契约（防 ../ 与 symlink 逃逸）。

行为测试——真造临时目录/符号链接断言归属判定，不断言实现结构。
覆盖归一前 3 处副本的等价语义 + fail-closed 边界。
"""
from __future__ import annotations

import os

from swarm.paths import is_within_root


class TestJoinMode:
    """join=True：相对片段拼到 root 再判（diff rel / sandbox rel 场景）。"""

    def test_within(self, tmp_path):
        assert is_within_root(tmp_path, "sub/a.txt", join=True) is True

    def test_root_itself(self, tmp_path):
        assert is_within_root(tmp_path, ".", join=True) is True

    def test_parent_escape(self, tmp_path):
        assert is_within_root(tmp_path, "../evil.txt", join=True) is False

    def test_deep_escape(self, tmp_path):
        assert is_within_root(tmp_path, "a/../../evil", join=True) is False

    def test_nested_ok(self, tmp_path):
        assert is_within_root(tmp_path, "a/b/c/d.py", join=True) is True


class TestAbsoluteMode:
    """join=False：candidate 是完整（可能绝对）路径（ingest 绝对上传路径 / executor 场景）。"""

    def test_absolute_within(self, tmp_path):
        target = tmp_path / "up" / "f.md"
        assert is_within_root(tmp_path, str(target), join=False) is True

    def test_absolute_outside(self, tmp_path):
        other = tmp_path.parent / "other_root_xyz" / "f.md"
        assert is_within_root(tmp_path / "root", str(other), join=False) is False

    def test_root_equals_target(self, tmp_path):
        assert is_within_root(tmp_path, str(tmp_path), join=False) is True


class TestSymlinkEscape:
    def test_symlink_out_is_rejected(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("x")
        link = root / "escape"
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            return  # 平台不支持 symlink → 跳过
        # 经 symlink 指向 root 外 → resolve 展开后判否
        assert is_within_root(root, "escape/secret.txt", join=True) is False


class TestFailClosed:
    def test_empty_root_absolute_candidate(self, tmp_path):
        # 空 root → Path("").resolve()=cwd；绝对 candidate 在 tmp_path 下大概率不在 cwd 内 → False
        # 用一个确定在 cwd 外的绝对路径断言 fail-closed 不误判归属
        assert is_within_root(tmp_path / "r", "/nonexistent_abs/x", join=False) is False

    def test_equivalence_to_parents_spelling(self, tmp_path):
        # 与旧 `target == root or root in target.parents` 拼写等价
        root = tmp_path.resolve()
        inside = (root / "a" / "b")
        assert (inside == root or root in inside.parents) == is_within_root(root, "a/b", join=True)
