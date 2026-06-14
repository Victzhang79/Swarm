"""安全两项回归测试（audit #31 scope 精确匹配 + #40 git token 脱敏）。

#31: _scope_violations 旧用任意字符后缀匹配，scope 'main.py' 误放行 'src/main.py'。
     修复后按路径段对齐。
#40: L3 git push token 从 URL 移到 http.extraHeader，且所有返回 stderr 经 _redact_secrets。

纯函数测试，无 DB/网络。
"""

from __future__ import annotations

import os

from swarm.types import FileScope
from swarm.worker.l1_pipeline import _scope_match, _scope_violations


# ── #31 scope 精确匹配 ──────────────────────────────

def test_scope_exact_match():
    assert _scope_match("src/a.py", "src/a.py") is True


def test_scope_rejects_basename_substring():
    """旧 bug：scope 'main.py' 不应放行 'src/main.py'（不同文件）。"""
    assert _scope_match("src/main.py", "main.py") is False


def test_scope_rejects_prefix_pollution():
    """scope 'src/main.py' 不应放行 '2src/main.py'（非路径段边界）。"""
    assert _scope_match("2src/main.py", "src/main.py") is False
    assert _scope_match("xsrc/main2.py", "src/main.py") is False


def test_scope_dir_scope_matches_children():
    """目录 scope 'src/' 应匹配其下文件。"""
    assert _scope_match("src/sub/a.py", "src/") is True


def test_scope_tolerates_repo_root_prefix():
    """diff 路径带仓库根前缀时，按完整路径段尾部对齐应匹配。"""
    assert _scope_match("repo/src/a.py", "src/a.py") is True


def test_scope_violations_end_to_end():
    diff = (
        "--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1 @@\n-x\n+y\n"
        "--- a/src/other.py\n+++ b/src/other.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    scope = FileScope(writable=["src/main.py"])
    violations = _scope_violations(diff, scope)
    # other.py 不在 scope → 违规；main.py 在 scope → 不违规
    assert "src/other.py" in violations
    assert "src/main.py" not in violations


# ── #40 git token 脱敏 ──────────────────────────────

def test_redact_secrets_masks_token():
    from swarm.brain.l3_gitlab import _redact_secrets
    os.environ["SWARM_GITLAB_TOKEN"] = "_test_secret_tok_abc123"
    try:
        msg = "fatal: unable to access https://oauth2:_test_secret_tok_abc123@gitlab/x.git/"
        red = _redact_secrets(msg)
        assert "_test_secret_tok_abc123" not in red
        assert "***" in red
    finally:
        os.environ.pop("SWARM_GITLAB_TOKEN", None)


def test_redact_oauth2_pattern_even_without_env():
    """兜底正则：即便环境无 token，也抹除 oauth2:xxx@ 形态。"""
    from swarm.brain.l3_gitlab import _redact_secrets
    os.environ.pop("SWARM_GITLAB_TOKEN", None)
    msg = "remote: https://oauth2:leakedtoken999@host/p.git rejected"
    red = _redact_secrets(msg)
    assert "leakedtoken999" not in red


def test_push_url_has_no_token():
    """push URL 不再含凭证（token 走 extraHeader）。"""
    from swarm.brain.l3_gitlab import _git_push_remote_url
    os.environ["SWARM_GITLAB_URL"] = "https://gitlab.example.com"
    os.environ["SWARM_GITLAB_TOKEN"] = "_test_tok_xyz"
    os.environ["SWARM_GITLAB_PROJECT_ID"] = "group/proj"
    try:
        url = _git_push_remote_url()
        assert url is not None
        assert "_test_tok_xyz" not in url
        assert "oauth2:" not in url
    finally:
        for k in ("SWARM_GITLAB_URL", "SWARM_GITLAB_TOKEN", "SWARM_GITLAB_PROJECT_ID"):
            os.environ.pop(k, None)


def test_auth_header_args_carries_token():
    """auth header 参数携带 token（用于 push 时注入）。"""
    from swarm.brain.l3_gitlab import _gitlab_auth_header_args
    os.environ["SWARM_GITLAB_TOKEN"] = "_test_tok_hdr"
    try:
        args = _gitlab_auth_header_args()
        assert args and args[0] == "-c"
        assert "_test_tok_hdr" in args[1]
        assert "extraHeader" in args[1]
    finally:
        os.environ.pop("SWARM_GITLAB_TOKEN", None)


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
    print(f"\n=== security #31+#40: {len(fns) - failed}/{len(fns)} passed ===")
    sys.exit(1 if failed else 0)
