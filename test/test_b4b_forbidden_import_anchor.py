"""B4b-1：#102(DR-10-F1) forbidden-import 负断言 import 锚定 sanitizer 行为级测试。

round66 st-29/st-32 实证：负断言 `! grep -rE 'import lombok|javax\\.'` 的裸包前缀分支
`javax\\.` 是行内子串匹配，命中 worker 写的"本类无 javax.*"表功注释 → 假阴性判死好产出。
锚定后只对真实 import 语句生效。
"""
from __future__ import annotations

from types import SimpleNamespace

from swarm.brain.contract_utils import (
    _anchor_forbidden_import_in_cmd,
    _anchor_forbidden_import_pattern,
    anchor_forbidden_import_asserts,
)


def test_bare_package_prefix_branch_anchored():
    assert _anchor_forbidden_import_pattern("javax\\.") == \
        "^[[:space:]]*import[[:space:]].*javax\\."
    # 多个裸包前缀
    assert _anchor_forbidden_import_pattern("javax\\.|jakarta\\.") == \
        "^[[:space:]]*import[[:space:]].*javax\\.|^[[:space:]]*import[[:space:]].*jakarta\\."


def test_javalang_class_api_ban_not_anchored():
    """复核 CONFIRMED HIGH：java.lang.* 类名 API 禁令（Runtime\\./System\\./Math\\.，首段大写）
    绝不锚定——这些类自动导入、从不写 import，锚定会令禁令永久失效(假 DONE)。首段小写才是包前缀。"""
    assert _anchor_forbidden_import_pattern("Runtime\\.exec\\(|Runtime\\.") == \
        "Runtime\\.exec\\(|Runtime\\."
    assert _anchor_forbidden_import_pattern("System\\.") == "System\\."
    assert _anchor_forbidden_import_pattern("Math\\.") == "Math\\."


def test_import_lombok_sibling_branch_line_anchored():
    """复核 C（修一类捞 sibling）：`import lombok` 分支也补行首锚，杜绝命中"无需 import lombok"注释。"""
    out = _anchor_forbidden_import_pattern("import lombok|javax\\.")
    assert "^[[:space:]]*import lombok" in out
    assert "^[[:space:]]*import[[:space:]].*javax\\." in out


def test_already_anchored_and_nonpkg_branches_untouched():
    # 已行首锚定的分支原样（幂等）
    assert _anchor_forbidden_import_pattern("^import (lombok|javax)") == "^import (lombok|javax)"
    # 方法名/字符串（不以 \\. 结尾）原样
    assert _anchor_forbidden_import_pattern("getGroups|setName") == "getGroups|setName"
    # 裸词 lombok（无 \\.）原样
    assert _anchor_forbidden_import_pattern("lombok") == "lombok"


def test_idempotent():
    once = _anchor_forbidden_import_pattern("import lombok|javax\\.")
    assert _anchor_forbidden_import_pattern(once) == once


def test_cmd_negative_assertion_anchored():
    c = "! grep -rE 'import lombok|javax\\.' ruoyi-alarm/x/TemplateRenderUtils.java"
    out = _anchor_forbidden_import_in_cmd(c)
    assert "^[[:space:]]*import[[:space:]].*javax\\." in out
    assert out != c


def test_cmd_test_z_grep_negative_anchored():
    c = 'test -z "$(grep -rE \'javax\\.\' src/A.java)"'
    out = _anchor_forbidden_import_in_cmd(c)
    assert "^[[:space:]]*import[[:space:]].*javax\\." in out


def test_cmd_test_n_positive_assertion_untouched():
    """复核 D：`test -n "$(grep …)"` 是【正面存在断言】(模式必须出现)，语义与 forbidden-import
    相反，绝不锚定（否则把"子串出现"要求收紧成"必须 import 语句行"→冤杀合法满足）。"""
    c = 'test -n "$(grep -rE \'javax\\.\' src/A.java)"'
    assert _anchor_forbidden_import_in_cmd(c) == c


def test_positive_grep_untouched():
    c = "grep -q 'javax\\.' src/A.java"
    assert _anchor_forbidden_import_in_cmd(c) == c


def test_bare_word_and_method_negations_untouched():
    assert _anchor_forbidden_import_in_cmd("! grep -rq 'lombok' pom.xml") == \
        "! grep -rq 'lombok' pom.xml"
    assert _anchor_forbidden_import_in_cmd("! grep -rn 'getGroups' src/A.java") == \
        "! grep -rn 'getGroups' src/A.java"


def test_already_anchored_cmd_untouched():
    c = "! grep -rnE '^import (lombok|javax\\.)' src/A.java"
    assert _anchor_forbidden_import_in_cmd(c) == c


def test_plan_level_rewrites_and_reports():
    st = SimpleNamespace(
        id="st-32",
        harness=SimpleNamespace(
            verify_commands=["! grep -rE 'import lombok|javax\\.' a/B.java"],
        ),
    )
    plan = SimpleNamespace(subtasks=[st])
    summary = anchor_forbidden_import_asserts(plan)
    assert "st-32" in summary
    # 已就地重写
    assert "^[[:space:]]*import[[:space:]].*javax\\." in st.harness.verify_commands[0]


def test_plan_level_no_change_no_report():
    st = SimpleNamespace(
        id="st-1",
        harness=SimpleNamespace(verify_commands=["mvn -q compile", "! grep -rq 'lombok' pom.xml"]),
    )
    plan = SimpleNamespace(subtasks=[st])
    summary = anchor_forbidden_import_asserts(plan)
    assert summary == {}
    assert st.harness.verify_commands == ["mvn -q compile", "! grep -rq 'lombok' pom.xml"]


def _grep_matches(pattern: str, text: str, extended: bool = True) -> bool:
    """用【真实 grep】（非 Python re——POSIX 字符类 [[:space:]] 仅 grep 支持）判匹配。"""
    import subprocess
    args = ["grep", "-E", pattern] if extended else ["grep", pattern]
    p = subprocess.run(args, input=text, capture_output=True, text=True)
    return p.returncode == 0


def test_comment_no_longer_false_kills_but_real_import_still_caught():
    """核心不变量（用真实 grep 验证）：注释里的 javax. 锚定后不再命中；真实 import javax. 仍被抓。"""
    anchored = _anchor_forbidden_import_pattern("import lombok|javax\\.")
    # worker 表功注释（散文）——锚定后不命中 → 负断言恒过（好产出不再被杀）
    assert _grep_matches(anchored, " * 静态工具类，无 Lombok、无 javax.*。") is False
    # 真实违规 import——仍命中 → 负断言判死（真禁令不放松）
    assert _grep_matches(anchored, "import javax.servlet.http.HttpServletRequest;") is True
    # 多空格 import 也抓（1+ 空白）
    assert _grep_matches(anchored, "import  javax.persistence.Entity;") is True
    # 复核 A：import static 也抓（不再逃逸）
    assert _grep_matches(anchored, "import static javax.servlet.http.HttpServletResponse.SC_OK;") is True
    # 注释里含 import 关键字的边缘（行首锚定杜绝）
    assert _grep_matches(anchored, "// import javax.foo 是被禁止的写法") is False


def test_anchor_prefix_uses_posix_class_not_gnu_backslash_s():
    """可移植性铁律：锚定前缀必须用 POSIX [[:space:]] 而非 GNU 扩展 \\s
    （负断言在沙箱跑，grep 可能是 busybox/BSD，\\s 会令模式永不匹配→放松禁令假 DONE）。"""
    anchored = _anchor_forbidden_import_pattern("javax\\.")
    assert "[[:space:]]" in anchored
    assert "\\s" not in anchored
