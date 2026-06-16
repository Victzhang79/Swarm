"""task 1a49aa66 回归：worker diff 生成（git diff 优先 + difflib fallback）格式必须被 git apply 接受。

根因：旧 difflib 用法（keepends=True + lineterm="" + "\n".join）行尾翻倍 / javadoc 边界
前导符错乱 → git apply "补丁损坏"。修复：① 本地 git 仓库优先用 git diff（同源必接受）；
② difflib fallback 改用 normalize 方案（内容行自带\n、头部行补\n、"".join）。
"""
import difflib
import os
import subprocess
import tempfile


def _difflib_block(old: str, new: str, rel: str) -> str:
    """复刻 executor._get_git_diff 修正后的 difflib 路径。"""
    old_norm = old.replace("\r\n", "\n").replace("\r", "\n")
    new_norm = new.replace("\r\n", "\n").replace("\r", "\n")
    old_lines = old_norm.splitlines(keepends=True)
    new_lines = new_norm.splitlines(keepends=True)
    ud = difflib.unified_diff(
        old_lines, new_lines, fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""
    )
    block = "".join(x if x.endswith("\n") else x + "\n" for x in ud)
    return block.rstrip("\n")


def _git_apply_ok(diff: str, fname: str, original: str) -> tuple[int, str]:
    d = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q"], cwd=d, check=False)
    with open(os.path.join(d, fname), "w") as f:
        f.write(original)
    subprocess.run(["git", "-C", d, "add", "-A"], capture_output=True, check=False)
    subprocess.run(
        ["git", "-C", d, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
        capture_output=True, check=False,
    )
    pf = os.path.join(d, "p.diff")
    with open(pf, "w") as f:
        f.write(diff + "\n")
    res = subprocess.run(["git", "apply", "--check", pf], cwd=d, capture_output=True, text=True, check=False)
    return res.returncode, res.stderr


# javadoc 边界场景（之前坏在这）
_OLD = (
    "public class S {\n    /**\n     * judge empty\n     */\n"
    "    public static boolean isEmpty(Object o) {\n        return o == null;\n    }\n}\n"
)
_NEW = (
    "public class S {\n    /**\n     * new method\n     */\n"
    "    public static boolean isBlankAll(String... s) {\n        return true;\n    }\n\n"
    "    /**\n     * judge empty\n     */\n"
    "    public static boolean isEmpty(Object o) {\n        return o == null;\n    }\n}\n"
)


def test_difflib_diff_accepted_by_git_apply():
    """修正后的 difflib 路径产出的 diff 必须被 git apply 接受（javadoc 边界）。"""
    block = _difflib_block(_OLD, _NEW, "S.java")
    assert "\n\n" not in block.replace("\n\n+", "X").replace("\n\n ", "Y"), "不应有行尾翻倍"
    rc, err = _git_apply_ok(block, "S.java", _OLD)
    assert rc == 0, f"git apply 拒绝了 difflib diff: {err}"


def test_difflib_no_doubled_newlines():
    """diff 内容行不再出现 \\n\\n 翻倍。"""
    block = _difflib_block(_OLD, _NEW, "S.java")
    # 每个 hunk 体行应恰好一个换行；检查没有连续两个空内容
    lines = block.split("\n")
    # 不应有"裸空行夹在 + 行之间"（翻倍的症状）
    for i, ln in enumerate(lines[:-1]):
        if ln.startswith("+") and ln != "+":
            # 下一行不应是空字符串（翻倍会插空行）
            assert lines[i + 1] != "" or i + 1 == len(lines) - 1, f"第{i}行后疑似翻倍空行"


def test_simple_append_accepted():
    """简单追加方法的 diff 被 git apply 接受。"""
    old = "class A {\n    void f() {}\n}\n"
    new = "class A {\n    void f() {}\n    void g() {}\n}\n"
    block = _difflib_block(old, new, "A.java")
    rc, err = _git_apply_ok(block, "A.java", old)
    assert rc == 0, f"git apply 失败: {err}"
