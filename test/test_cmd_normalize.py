"""A2: python 命令规范化单一事实源 cmd_normalize.normalize_python_cmd 行为契约。

行为测试（禁 inspect.getsource）。锁两处环境的现有语义 + 收敛后本地路径新获得的
compileall 归一（原 l1_pipeline 副本缺此步 = round24 A2 修复的缺口）。
"""
from swarm.worker.cmd_normalize import normalize_py_compile_cmd, normalize_python_cmd


class TestSandboxPath:
    """py_bin 默认 python3（沙箱镜像只有 python3）——须与旧 sandbox._normalize_python_cmd 等价。"""

    def test_bare_python_to_python3(self):
        assert normalize_python_cmd("python -m pytest -q") == "python3 -m pytest -q"
        assert normalize_python_cmd("python script.py") == "python3 script.py"

    def test_py_compile_dir_becomes_compileall(self):
        assert normalize_python_cmd("python -m py_compile .") == "python3 -m compileall ."

    def test_python3_unchanged(self):
        assert normalize_python_cmd("python3 -m pytest") == "python3 -m pytest"
        assert normalize_python_cmd("python3.11 -m pytest") == "python3.11 -m pytest"

    def test_no_false_positives(self):
        assert normalize_python_cmd("PYTHONPATH=. python -c x") == "PYTHONPATH=. python3 -c x"
        assert normalize_python_cmd("/usr/bin/python foo") == "/usr/bin/python foo"
        assert normalize_python_cmd("echo pythonista") == "echo pythonista"

    def test_compound(self):
        assert normalize_python_cmd("cd /w && python -m py_compile .") == "cd /w && python3 -m compileall ."

    def test_no_python_unchanged(self):
        assert normalize_python_cmd("mvn compile") == "mvn compile"
        assert normalize_python_cmd("") == ""


class TestLocalPath:
    """py_bin=本机解释器（l1_pipeline 本地确定性闸门）——收敛后同样获得 compileall 归一。"""

    def test_custom_py_bin_substituted(self):
        assert normalize_python_cmd("python -m pytest", py_bin="/venv/bin/python") == "/venv/bin/python -m pytest"

    def test_absolute_py_bin_path_safe(self):
        # 绝对路径解释器含 / 与数字，不被 re.sub 转义误解释（lambda 替换）
        out = normalize_python_cmd("python x.py", py_bin="/Users/a/.venv/bin/python3.14")
        assert out == "/Users/a/.venv/bin/python3.14 x.py"

    def test_local_now_normalizes_py_compile(self):
        # round24 A2 修复缺口：本地路径原不改 py_compile <dir>，收敛后也改 compileall
        assert normalize_python_cmd("python -m py_compile .", py_bin="python") == "python -m compileall ."

    def test_py_bin_python_still_applies_compileall(self):
        # 即便 py_bin=="python"（python→python 无操作），compileall 归一仍生效
        assert normalize_python_cmd("python3 -m py_compile src/", py_bin="python") == "python3 -m compileall src/"

    def test_trailing_python_token_normalized(self):
        # 比旧 l1 正则更全：命令末尾（无尾随空格）的 python 也归一
        assert normalize_python_cmd("exec python", py_bin="python3") == "exec python3"


class TestPyCompileHelper:
    def test_files_unchanged(self):
        assert normalize_py_compile_cmd('python3 -m py_compile "a.py" "b.py"') == 'python3 -m py_compile "a.py" "b.py"'

    def test_dir_to_compileall(self):
        assert normalize_py_compile_cmd("python3 -m py_compile -q .") == "python3 -m compileall -q ."

    def test_idempotent(self):
        assert normalize_py_compile_cmd("python3 -m compileall .") == "python3 -m compileall ."
