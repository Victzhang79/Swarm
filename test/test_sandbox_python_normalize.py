"""回归：沙箱命令规范化（单一事实源）。

两道幂等规范化，均落在 SandboxManager.run_command 执行统一入口，覆盖所有
harness/worker/trivial 命令，避免逐处改命令字符串"修一个漏一个"：

1) python→python3（task a4988789 实证）：沙箱 python 镜像 PATH 只有 python3，
   无 python 别名。"python -m py_compile" 等在沙箱 exit=127 command not found。

2) py_compile <dir>→compileall <dir>（task d4f9db79 实证）：py_compile 只接受
   【文件】参数，传目录（如 `.`）报 [Errno 21] Is a directory，exit=1 → L1
   构建闸门假阴性（trivial 成功却被判失败）。compileall 才接受目录递归编译。
"""

from __future__ import annotations

from swarm.worker.sandbox import _normalize_python_cmd as n
from swarm.worker.sandbox import _normalize_py_compile_cmd as pc


def test_bare_python_to_python3():
    # 注意：py_compile . 现在会被第二道规范化进一步改成 compileall .
    assert n("python -m py_compile .") == "python3 -m compileall ."
    assert n("python -m pytest -q") == "python3 -m pytest -q"
    assert n("python script.py") == "python3 script.py"


def test_python3_unchanged():
    assert n("python3 -m pytest") == "python3 -m pytest"
    assert n("python3.11 -m pytest") == "python3.11 -m pytest"


def test_compound_commands():
    assert n("cd /workspace && python -m py_compile .") == "cd /workspace && python3 -m compileall ."
    assert n("python -m a && python -m b") == "python3 -m a && python3 -m b"


def test_no_false_positives():
    # 环境变量 PYTHONPATH 不应被改
    assert n("PYTHONPATH=. python -c x") == "PYTHONPATH=. python3 -c x"
    # 绝对路径不动
    assert n("/usr/bin/python foo") == "/usr/bin/python foo"
    # 子串不误伤
    assert n("echo pythonista") == "echo pythonista"
    assert n("cat mypython.txt") == "cat mypython.txt"


def test_no_python_unchanged():
    assert n("mvn compile") == "mvn compile"
    assert n("ls -la") == "ls -la"
    assert n("") == ""


# ── py_compile <dir> → compileall <dir> 专项 ──

def test_py_compile_dir_to_compileall():
    # 目录参数（py_compile 跑不了）→ 改 compileall
    assert pc("python3 -m py_compile .") == "python3 -m compileall ."
    assert pc("python3 -m py_compile src/") == "python3 -m compileall src/"
    assert pc("python3 -m py_compile -q .") == "python3 -m compileall -q ."


def test_py_compile_files_unchanged():
    # 全是 .py 文件参数 → py_compile 本就正确，不改
    assert pc('python3 -m py_compile "a.py" "b.py"') == 'python3 -m py_compile "a.py" "b.py"'
    assert pc("python3 -m py_compile src/dotenv/version.py") == "python3 -m py_compile src/dotenv/version.py"


def test_compileall_idempotent():
    # 已是 compileall → 不动（幂等）
    assert pc("python3 -m compileall .") == "python3 -m compileall ."


def test_py_compile_compound():
    # 复合命令里只改 py_compile 段，&& 后另一段不受影响
    assert pc("cd /workspace && python3 -m py_compile .") == "cd /workspace && python3 -m compileall ."


def test_py_compile_no_match_unchanged():
    assert pc("python3 -m pytest -q") == "python3 -m pytest -q"
    assert pc("ls -la") == "ls -la"
    assert pc("") == ""


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ✅ {fn.__name__}")
    print(f"\n=== 沙箱命令规范化: {len(fns)}/{len(fns)} passed ===")
