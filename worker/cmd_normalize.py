"""worker/cmd_normalize.py — shell 命令里 python 调用的规范化（单一事实源）。

round24 A2：原 `_normalize_python_cmd` 有两份语义不一致的副本——
  - sandbox.py：裸 python→python3 + py_compile <dir>→compileall（沙箱镜像只有 python3）
  - l1_pipeline.py：裸 python→_python_bin()，但【漏了 compileall 归一】

两处的【算法】相同（token 归一 + py_compile→compileall），只有【目标解释器】按环境
合理不同（沙箱恒 python3；本地按 _python_bin() 探测 venv/sys.executable）。故抽为单一
`normalize_python_cmd(command, *, py_bin)`，把解释器作参数——沙箱传 "python3"，本地传
_python_bin()。本地路径由此也获得 compileall 归一（补上原缺口）。
"""

from __future__ import annotations

import re

# 裸 python token：前后均无 word/./-，故 python3 / python3.11 / /usr/bin/python /
# my-python / pythonista 均不误伤。比旧 l1_pipeline 的 `(^|[\s;&|])python(?=\s)` 更全
# （能覆盖命令末尾的 python，且不依赖尾随空格）。
_PYTHON_TOKEN_RE = re.compile(r"(?<![\w./-])python(?![\w.-])")

# 捕获 `-m py_compile <args...>`，args 直到命令分隔符（&& || ; | 换行）或行尾。
_PY_COMPILE_RE = re.compile(r"-m\s+py_compile\s+(?P<args>[^&|;\n]+)")


def normalize_py_compile_cmd(command: str) -> str:
    """把 `py_compile <含目录参数>` 规范化为 `compileall <同参数>`（幂等）。

    py_compile 仅接受文件；只要参数里出现任一非 .py 结尾的路径（典型是目录 `.`），
    命令必失败（[Errno 21] Is a directory，exit=1 → L1 构建闸门假阴性，task d4f9db79
    实证）。compileall 接受目录递归编译，是"编译整个项目"的正确工具。
      - "python3 -m py_compile ."              → "python3 -m compileall ."         ✅
      - "python -m py_compile src/"            → "python -m compileall src/"        ✅
      - 'python3 -m py_compile "a.py" "b.py"'  → 不变（全是 .py 文件，py_compile 正确）✅
      - "python3 -m compileall ."              → 不变（已是 compileall）             ✅
    """
    if not command or "py_compile" not in command:
        return command

    def _sub(m: "re.Match[str]") -> str:
        raw = m.group("args")
        # 拆 token 检查：剥掉成对引号后，任一非 .py 结尾的 token 视为目录 → 需 compileall。
        toks = [t.strip("'\"") for t in raw.split() if t.strip("'\"")]
        # 仅看位置参数（跳过 -q/-f 等选项），任一不以 .py 结尾即判定含目录。
        pos = [t for t in toks if not t.startswith("-")]
        has_dir = any(not t.endswith(".py") for t in pos) if pos else True
        if has_dir:
            return m.group(0).replace("py_compile", "compileall", 1)
        return m.group(0)

    return _PY_COMPILE_RE.sub(_sub, command)


def normalize_python_cmd(command: str, *, py_bin: str = "python3") -> str:
    """把命令里独立的 `python` token 规范化为 py_bin，并把 py_compile <dir> 改 compileall。

    幂等，单一事实源，覆盖 harness/worker/trivial 生成的所有命令，避免逐处改命令
    字符串"修一个漏一个"（task a4988789 实证：沙箱 python→127 command not found）。
    py_bin 用 lambda 替换（不走 re.sub 的转义解释），故绝对路径解释器（sys.executable）
    也安全。
      - normalize_python_cmd("python -m pytest")              → "python3 -m pytest"
      - normalize_python_cmd("python x", py_bin="/v/py")      → "/v/py x"
      - normalize_python_cmd("python3 -m pytest")             → 不变（python3 不匹配）
      - normalize_python_cmd("PYTHONPATH=. python")           → "PYTHONPATH=. python3"
      - normalize_python_cmd("/usr/bin/python")               → 不变（前有 /，不匹配）
    """
    if not command or "python" not in command:
        return command
    command = _PYTHON_TOKEN_RE.sub(lambda _m: py_bin, command)
    return normalize_py_compile_cmd(command)
