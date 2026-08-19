"""★32 号文批2a★ merge_engine M3/M4/M5 行为锁。

M3：_union_new_manifest 并集成功但恰等于 owner 版（非 owner 是其子集、零新增）时
    返回 None ⇒ 调用方当并集失败记 owner_drops 假账（一行没丢却冤杀 L6/冤触 M-6）。
M4：git core.quotePath=true（默认）把非 ASCII 路径输出成 C 风格引号串，全引擎零
    反转义 ⇒ 中文文件名毁形/a//b/ 前缀剥离失效（引号包前缀）/目录键口径分裂。
    治法=消费侧唯一漏斗 _strip_diff_path/_parse_git_header_paths 先反转义再剥前缀。
M5：_is_aggregate_manifest/_is_module_manifest 原是 STACK_SPEC 之外的第二份手写
    枚举（漏 npm package.json、python pyproject.toml）⇒ 收编到访问器派生（调用时
    读取不冻结，F-7），.NET 补集显式保留。本文件的漂移锁直接遍历访问器全集——
    STACK_SPEC 加栈/加别名，判据与锁自动跟随。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from swarm.brain.merge_engine import (
    _diff_target_files,
    _is_aggregate_manifest,
    _is_module_manifest,
    _parse_git_header_paths,
    _union_new_manifest,
    base_has_module_skeleton,
    filter_orphan_module_patches,
)
from swarm.project.diff_apply import files_from_unified_diff, strip_diff_path
from swarm.stacks import module_manifest_names, root_aggregate_manifests


# ───────────── M3：并集=owner 版（零新增）不得记丢件假账 ─────────────

def _pom_lines(*deps: str) -> list[str]:
    return ["+<project>", "+  <dependencies>",
            *[f"+    <dependency>{d}</dependency>" for d in deps],
            "+  </dependencies>", "+</project>"]


def test_union_new_manifest_subset_non_owner_returns_owner_lines_not_none():
    """M3 单元锁：非 owner 是 owner 的真子集 ⇒ 并集成功零新增，返回 owner 行（非 None）。
    返回 None 会被调用方记 owner_drops 假账（冤杀 L6 should_write_success + 冤触 M-6）。
    注：bodies 的行带 `+` 前缀（_new_side_lines 口径，见调用点注释）。"""
    owner = _pom_lines("alarm-core", "alarm-notify")
    subset = _pom_lines("alarm-core")
    got = _union_new_manifest("alarm-task/pom.xml", "st-1",
                              {"st-1": owner, "st-2": subset})
    assert got is not None, "子集包含=并集成功零新增，绝不允许当失败回退（M3 假账）"
    assert got == owner, "零新增并集的交付内容必须逐字等于 owner 版"


def test_union_new_manifest_real_additions_still_union():
    """回归护栏：各加不同条目 ⇒ 真并集（两条都在），不受 M3 分支影响。"""
    got = _union_new_manifest("alarm-task/pom.xml", "st-1",
                              {"st-1": _pom_lines("alarm-core"),
                               "st-2": _pom_lines("alarm-notify")})
    assert got is not None
    assert any("alarm-core" in ln for ln in got) and any("alarm-notify" in ln for ln in got)


# ───────────── M4：C-quoted 路径反转义（唯一漏斗）─────────────

def test_strip_diff_path_unquotes_c_quoted_utf8():
    """git 对中文路径的输出形态：`+++ "b/\\344\\270\\255\\346\\226\\207.txt"`——
    引号包住 b/ 前缀，不反转义则前缀剥离失效+文件名毁形。"""
    assert strip_diff_path('"b/\\344\\270\\255\\346\\226\\207.txt"') == "中文.txt"


def test_strip_diff_path_unquoted_and_devnull_unchanged():
    """回归护栏：非引号路径与 /dev/null 哨兵行为不变。"""
    assert strip_diff_path("b/src/Main.java") == "src/Main.java"
    assert strip_diff_path("/dev/null") == "/dev/null"


def test_parse_git_header_paths_c_quoted_with_space():
    """diff --git 行的 C-quoted 形态：引号内空格不得按空白切段。"""
    ap, bp = _parse_git_header_paths(
        'diff --git "a/\\344\\270\\255 \\346\\226\\207.txt" "b/\\344\\270\\255 \\346\\226\\207.txt"')
    assert ap == bp == "中 文.txt"


def test_parse_git_header_paths_unquoted_unchanged():
    ap, bp = _parse_git_header_paths("diff --git a/src/A.java b/src/A.java")
    assert (ap, bp) == ("src/A.java", "src/A.java")


def test_diff_target_files_unquotes_c_quoted():
    """_diff_target_files 已收编唯一漏斗：C-quoted +++ 行出干净相对路径。"""
    diff = ('diff --git "a/\\344\\270\\255\\346\\226\\207.txt" "b/\\344\\270\\255\\346\\226\\207.txt"\n'
            "--- /dev/null\n"
            '+++ "b/\\344\\270\\255\\346\\226\\207.txt"\n'
            "@@ -0,0 +1,1 @@\n+hi\n")
    assert _diff_target_files(diff) == ["中文.txt"]


@pytest.mark.skipif(shutil.which("git") is None, reason="沙箱无 git")
def test_real_git_produces_c_quoted_paths_and_funnel_handles_both():
    """端到端锁：真 git 默认 quotePath=true 产出 C-quoted 中文路径（前提事实），
    漏斗反转义；显式 -c core.quotePath=false 产出原始 UTF-8，漏斗原样透传。
    ★批2a-R1 F5★ 全部 git 子进程统一隔离 env（GIT_CONFIG_NOSYSTEM+HOME=d）——
    开发机/CI 若配 core.quotePath=false，下面的前提断言会假红（平台相关假红族）。"""
    with tempfile.TemporaryDirectory() as d:
        git_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": d,
                   "GIT_CONFIG_NOSYSTEM": "1",
                   "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                   "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "init", "-q"], cwd=d, check=True, env=git_env)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=d,
                       check=True, env=git_env)
        Path(d, "中文.txt").write_text("hello\n")
        # 未跟踪文件不进 diff；用 add -N 占位（与 executor_sync 同手法）
        subprocess.run(["git", "add", "-N", "中文.txt"], cwd=d, check=True, env=git_env)
        quoted = subprocess.run(["git", "diff", "--no-color", "HEAD"], cwd=d,
                                capture_output=True, text=True, env=git_env).stdout
        assert '\\344\\270\\255' in quoted, "前提事实：默认 quotePath=true 必须产出 C-quoted"
        assert _diff_target_files(quoted) == ["中文.txt"]
        raw = subprocess.run(["git", "-c", "core.quotePath=false",
                              "diff", "--no-color", "HEAD"], cwd=d,
                             capture_output=True, text=True, env=git_env).stdout
        assert "中文.txt" in raw
        assert _diff_target_files(raw) == ["中文.txt"]


# ───────────── M5：判据=STACK_SPEC 派生（漂移锁）+ .NET 补集保留 ─────────────

@pytest.mark.parametrize("name", sorted(root_aggregate_manifests()))
def test_every_stack_aggregate_manifest_recognized(name):
    """M5 漂移锁·聚合档：STACK_SPEC 全栈聚合清单名逐个必须被认（加栈/加别名自动进锁）。"""
    assert _is_aggregate_manifest(name), name
    assert _is_aggregate_manifest(f"sub/{name}"), f"sub/{name}"


@pytest.mark.parametrize("name", sorted(module_manifest_names()))
def test_every_stack_module_manifest_recognized(name):
    """M5 漂移锁·模块档：全栈模块清单名逐个必须被认（含 npm package.json、
    python pyproject.toml——原手写枚举漏掉的两个）。"""
    assert _is_module_manifest(f"mod/{name}"), name


def test_dotnet_supplement_kept():
    """未收录栈补集（.NET 不在 STACK_SPEC）：删补集=.NET 行为回退，显式锁死。"""
    assert _is_aggregate_manifest("App.sln")
    assert _is_module_manifest("ui/Foo.csproj")
    assert _is_module_manifest("ui/Foo.fsproj")
    assert _is_module_manifest("ui/Foo.vbproj")


def test_non_manifests_still_rejected():
    """反向护栏：普通源文件/文档绝不误判为清单（误杀侧）。"""
    for f in ("src/Main.java", "module.py", "x/pom.xml.bak", "README.md",
              "package.json5", "mypyproject.toml"):
        assert not _is_aggregate_manifest(f), f
        assert not _is_module_manifest(f), f


# ───────────── 批2a-R1 F1：豁免探针与激活面同源（npm/python 不冤判孤儿）─────────────

def test_base_has_module_skeleton_derived_set():
    """F1 单元锁：探针认 STACK_SPEC 派生全集（含 npm package.json/python pyproject.toml）
    + .NET 三后缀补集；空目录/不存在目录/路径 None 一律 False（fail-safe 让路由调用方
    以 probe=None 表达，与本探针的 False 分形）。"""
    with tempfile.TemporaryDirectory() as d:
        for name in ("package.json", "pyproject.toml", "pom.xml", "go.mod"):
            sub = Path(d, name.replace(".", "_"))
            sub.mkdir()
            (sub / name).write_text("{}")
            assert base_has_module_skeleton(d, sub.name), name
        net = Path(d, "netmod")
        net.mkdir()
        (net / "Foo.fsproj").write_text("<Project/>")
        assert base_has_module_skeleton(d, "netmod")
        empty = Path(d, "empty")
        empty.mkdir()
        assert not base_has_module_skeleton(d, "empty")
        assert not base_has_module_skeleton(d, "nonexistent")
    assert not base_has_module_skeleton(None, "whatever")


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0,
                    reason="root 下 chmod 000 不产生 EACCES")
def test_base_has_module_skeleton_eacces_raises_not_false():
    """★批2a-R2 hunter MED★ 权限异常目录必须【抛 OSError】（filter 的 #29-8 M-5
    「抛异常=证据不足保守保留」分路才收得到），绝不被 py≥3.13 的 is_file/glob
    静默吞成 False（=「确定不存在」冤剔既有模块，且与 py<3.13 极性翻转）。"""
    with tempfile.TemporaryDirectory() as d:
        mod = Path(d, "a")
        mod.mkdir()
        (mod / "package.json").write_text("{}")
        mod.chmod(0)
        try:
            with pytest.raises(OSError):
                base_has_module_skeleton(d, "a")
        finally:
            mod.chmod(0o755)


def test_orphan_filter_keeps_existing_npm_module():
    """★F1 反向锁（hunter HIGH）★：npm workspace 既有模块 packages/a（base 有
    package.json）在【本批未碰它的清单】时不得冤判孤儿——M5 把激活面拓宽到 npm
    （defined 非空即可激活），若豁免探针仍只认 pom/gradle/Cargo/go.mod，本锁红：
    st-touch 对 packages/a/src 的真产出补丁会被剔掉。"""
    with tempfile.TemporaryDirectory() as d:
        pkg_a = Path(d, "a")
        pkg_a.mkdir(parents=True)
        (pkg_a / "package.json").write_text('{"name": "a"}')
        diff_new_mod = (
            "--- /dev/null\n+++ b/b/package.json\n"
            "@@ -0,0 +1,1 @@\n+{}\n"
            "--- /dev/null\n+++ b/b/index.js\n"
            "@@ -0,0 +1,1 @@\n+x\n")
        diff_touch_a = (
            "--- a/a/src/index.js\n+++ b/a/src/index.js\n"
            "@@ -1,1 +1,1 @@\n-old\n+new\n")
        kept, dropped = filter_orphan_module_patches(
            [("st-new", diff_new_mod), ("st-touch", diff_touch_a)],
            base_module_exists=lambda rel: base_has_module_skeleton(d, rel),
            is_multimodule=True)
        assert dropped == {}, dropped
        assert [sid for sid, _ in kept] == ["st-new", "st-touch"]


def test_detect_multimodule_npm_workspaces_root():
    """F1 磁盘臂：根 package.json 带 workspaces 键=多模块（与 pom <modules> 同规格
    内容判据）；无 workspaces 键/畸形 JSON 不得冤判 True。"""
    from swarm.brain.nodes import _detect_multimodule_layout
    with tempfile.TemporaryDirectory() as d:
        Path(d, "package.json").write_text('{"name": "r", "workspaces": ["packages/*"]}')
        assert _detect_multimodule_layout(d, None) is True
    with tempfile.TemporaryDirectory() as d2:
        Path(d2, "package.json").write_text('{"name": "r"}')
        assert _detect_multimodule_layout(d2, None) is False
        Path(d2, "package.json").write_text("{broken json")
        assert _detect_multimodule_layout(d2, None) is False
        # ★批2a-R2 reviewer MED★ 合法 JSON 但非对象（null/[]）也不得抛 AttributeError
        # （原实现 .get 直接炸出捕集 ⇒ MERGE 崩死）——fail-safe=False。
        for doc in ("null", "[]", '"x"', "123"):
            Path(d2, "package.json").write_text(doc)
            assert _detect_multimodule_layout(d2, None) is False, doc


# ───────────── 批2a-R1 F2：files_from_unified_diff 收编唯一漏斗 ─────────────

def _c_quote(s: str) -> str:
    """git core.quotePath=true 的 C 风格引号形态（非 ASCII 字节 → \\ooo 八进制）。"""
    return '"' + "".join(
        c if ord(c) < 128 else "".join(f"\\{b:03o}" for b in c.encode("utf-8"))
        for c in s) + '"'


def test_ffud_c_quoted_header_collected_clean():
    """F2 fail-open 侧：quoted `+++ "b/…"` 头行不得整行蒸发（旧判据 6 字符硬前缀
    让它从枚举里消失 ⇒ D3 闸/L1 scope 复核对 quoted 文件隐形）。
    夹具用【新文件】形态（--- /dev/null）——只有 +++ 侧带路径，才能单独钉住
    +++ 臂（修改形态下 --- 臂会替 +++ 臂背书，突变 +++ 门控仍绿=零区分力）。"""
    rel = "中文模块/src/Foo.java"
    diff = (f"diff --git {_c_quote('a/' + rel)} {_c_quote('b/' + rel)}\n"
            "--- /dev/null\n"
            f"+++ {_c_quote('b/' + rel)}\n"
            "@@ -0,0 +1,1 @@\n+x\n")
    assert files_from_unified_diff(diff) == [rel]


def test_ffud_quoted_rename_unquoted_key():
    """F2 误杀侧：quoted rename from/to 出【反转义后的干净键】（旧版带引号毁形串
    进账 ⇒ 与任何正常键永不相等）。"""
    diff = ("diff --git \"a/old.txt\" \"b/\\344\\270\\255\\346\\226\\207.txt\"\n"
            "rename from \"old.txt\"\n"
            "rename to \"\\344\\270\\255\\346\\226\\207.txt\"\n")
    assert files_from_unified_diff(diff) == ["old.txt", "中文.txt"]


def test_ffud_rename_literal_a_prefix_not_stripped():
    """rename 行无 a//b/ 前缀语义：真名就叫 `a/x` 的文件不得被误剥成 `x`
    （rename 行只反转义，绝不走 strip_diff_path）。"""
    diff = ("diff --git a/a/x b/a/x\nrename from a/x\nrename to a/x\n")
    assert files_from_unified_diff(diff) == ["a/x"]


def test_ffud_deleted_line_lookalike_not_collected():
    """门控特异性回归：hunk 体内删除行的【内容】若以 `-- ` 开头，整行长成
    `--- x` 模样——payload 无 a/ 前缀不得误采为路径（否则 scope 复核冤判越界）。"""
    diff = ("--- a/f.py\n+++ b/f.py\n"
            "@@ -1,2 +1,1 @@\n"
            "--- x\n"
            " ctx\n")
    assert files_from_unified_diff(diff) == ["f.py"]


def test_parse_git_header_paths_mixed_quoted_first_falls_back():
    """hunter 不确定项钉锁：quoted-first 混合形态（`"a/中文" b/ascii`，真 git 实测
    存在）best-effort 返回 ("","")——此时文件身份由 rename from/to 行兜底，
    该【兜底关系】本身钉死（若哪天支持混合形态，本锁翻面提醒复核兜底链）。"""
    ap, bp = _parse_git_header_paths(
        'diff --git "a/\\344\\270\\255.txt" b/ascii.txt')
    assert (ap, bp) == ("", "")


# ───────────── 批2a-R1 F3：JSON 清单并集产畸形 → 如实回退 ─────────────

def _pkg_lines_mid(*deps: str) -> list[str]:
    """各写者版本：base/zzz 两条已有依赖之间插自家条目（带尾逗号=单独合法）。"""
    return ["+{", '+  "dependencies": {', '+    "base": "0.1.0",',
            *[f'+    "{d}": "1.0.0",' for d in deps],
            '+    "zzz": "9.9.9"', "+  }", "+}"]


def _pkg_lines_first(*deps: str) -> list[str]:
    """各写者版本：空 dependencies 里插首条依赖（无尾逗号=单独合法）。"""
    return ["+{", '+  "dependencies": {',
            *[f'+    "{d}": "1.0.0"' for d in deps],
            "+  }", "+}"]


def test_union_new_manifest_noop_invalid_json_still_unions_not_drops():
    """★批2a-R2 hunter LOW-4★ noop 子集 + owner 版内容本身无效 JSON（尾逗号）：
    noop 判定必须先于 F3 校验 ⇒ 返回 owner 行（账记 unions=一行没丢），不得
    return None 让调用方记 owner_drops 假账（M3 证伪的形态在此角点复活）。"""
    owner = ["+{", '+  "dependencies": {', '+    "a": "1.0.0",', "+  }", "+}"]
    subset = ["+{", '+  "dependencies": {', "+  }", "+}"]
    got = _union_new_manifest("package.json", "st-1", {"st-1": owner, "st-2": subset})
    assert got == owner, got


def test_union_new_manifest_json_invalid_falls_back():
    """★F3 反向锁（夹具坐实的 MED）★：空 dependencies 双写者各插首条依赖（各自
    合法、行尾无逗号）→ 并集拼出缺逗号无效 JSON ⇒ 必须 return None（调用方落
    owner 独占+drops 账），绝不产畸形记「并集成功」。"""
    got = _union_new_manifest("package.json", "st-1",
                              {"st-1": _pkg_lines_first("a"),
                               "st-2": _pkg_lines_first("b")})
    assert got is None, got


def test_union_new_manifest_json_valid_union_kept():
    """正向臂：中段插入（各带尾逗号）的并集是有效 JSON ⇒ 正常并集不受 F3 闸误伤。"""
    got = _union_new_manifest("package.json", "st-1",
                              {"st-1": _pkg_lines_mid("a"),
                               "st-2": _pkg_lines_mid("b")})
    assert got is not None
    import json as _json
    merged_text = "\n".join(ln[1:] for ln in got)
    parsed = _json.loads(merged_text)
    assert set(parsed["dependencies"]) == {"base", "a", "b", "zzz"}


def test_merge_diffs_json_bad_union_visible_not_silent():
    """F3 modify 路径接线锁：base 已存在的 package.json 双写者各插首条依赖 →
    并集产物无效 JSON ⇒ 弃用并集落 3-way ⇒ 同锚点不同插入=【可见冲突/rebase】，
    绝不以「干净消解」交付无效 JSON。删 :1281 附近的 F3 检查 → 本锁红。"""
    from swarm.brain.merge_engine import merge_diffs
    base_pkg = '{\n  "dependencies": {\n  }\n}\n'

    def _reader(p: str) -> str | None:
        return base_pkg if p == "package.json" else None

    da = ("--- a/package.json\n+++ b/package.json\n"
          '@@ -2,1 +2,2 @@\n   "dependencies": {\n+    "a": "1.0.0"\n')
    db = ("--- a/package.json\n+++ b/package.json\n"
          '@@ -2,1 +2,2 @@\n   "dependencies": {\n+    "b": "2.0.0"\n')
    result = merge_diffs(
        [("st-1", da), ("st-2", db)], base_reader=_reader,
        auto_resolve=True, subtask_order=["st-1", "st-2"])
    assert result.conflicts or result.rebase_subtask_ids, (
        f"无效 JSON 被当干净合并交付: {result.merged_diff!r}")
