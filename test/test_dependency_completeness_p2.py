#!/usr/bin/env python3
"""#31-P2：子任务【声明依赖坐标】完整性闸——test-first 红测（栈中立）。

真根（治本 #31/#35，round65e13/round65e2 实锤）：scaffold 子任务的 contract["dependencies"]
声明模块 manifest【必须声明】的第三方/内部坐标，但 worker 产出的 manifest（pom/package.json/
go.mod/Cargo.toml）实际【没声明】那些坐标 → 下游兄弟构建期 reactor 读不出 → 连坐。既有
create_files 闸只核验【必建文件】存在，核验不到"文件在但缺声明坐标"。本闸补刀。

设计（STOP 报审已批）：
- 事实源：subtask.contract["dependencies"]（scaffold 注入器写，同源已剪枝，永不索要模板没写的坐标）。
- 栈中立：manifest basename → verifier registry（maven/gradle/npm/go/cargo），各自 stack-特化解析。
- 匹配：按 artifactId / package-name / module-path 尾名，★忽略 group + version★（免疫 BOM 受管/
  版本范围/${project.version}/workspace:*）。
- fail-open 铁律：只在【本子任务 scope 拥有的 manifest + backend 有 verifier + 读到真 manifest 文本 +
  声明坐标名可证不在其中】才判缺失；读不到/空/未知栈/空契约/跨模块 → 一律跳过放行。

纯函数红测（不用 live/cassette_replay）：missing_declared_dependencies + 5 栈 verifier。
"""
from __future__ import annotations


# ── 夹具：真 manifest 文本 ──

_POM_WITH = """<project>
  <parent><groupId>com.ruoyi</groupId><artifactId>ruoyi</artifactId><version>1.0</version></parent>
  <artifactId>ruoyi-alarm</artifactId>
  <dependencies>
    <dependency><groupId>com.google.zxing</groupId><artifactId>core</artifactId><version>3.5.1</version></dependency>
    <dependency><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId></dependency>
  </dependencies>
</project>"""

_POM_MISSING_ZXING = """<project>
  <artifactId>ruoyi-alarm</artifactId>
  <dependencies>
    <dependency><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId></dependency>
  </dependencies>
</project>"""

_POM_MANAGED_ONLY = """<project>
  <artifactId>ruoyi-alarm</artifactId>
  <dependencyManagement><dependencies>
    <dependency><groupId>com.google.zxing</groupId><artifactId>core</artifactId><version>3.5.1</version></dependency>
  </dependencies></dependencyManagement>
  <dependencies>
    <dependency><groupId>org.projectlombok</groupId><artifactId>lombok</artifactId></dependency>
  </dependencies>
</project>"""

_POM_EXCLUSION_ONLY = """<project>
  <artifactId>ruoyi-alarm</artifactId>
  <dependencies>
    <dependency><groupId>org.springframework</groupId><artifactId>spring-core</artifactId>
      <exclusions><exclusion><groupId>commons-logging</groupId><artifactId>core</artifactId></exclusion></exclusions>
    </dependency>
  </dependencies>
</project>"""


def _mdd(contract_deps, scope_manifests, *, disk, exempt=None):
    """薄封装：disk 是 {rel: text} 映射，构造 read 回调（缺 key → None=读不到）。"""
    from swarm.worker.l1_pipeline import missing_declared_dependencies
    return missing_declared_dependencies(
        contract_deps, scope_manifests,
        read=lambda rel: disk.get(rel), exempt=exempt)


# ════════════════ 纯函数：missing_declared_dependencies（Maven）════════════════

def test_p2_all_declared_no_missing():
    """契约要求 zxing:core + lombok，pom 都声明 → 无缺失。"""
    deps = [{"module": "ruoyi-alarm", "artifacts": ["com.google.zxing:core:3.5.1", "org.projectlombok:lombok"]}]
    out = _mdd(deps, ["ruoyi-alarm/pom.xml"], disk={"ruoyi-alarm/pom.xml": _POM_WITH})
    assert out == [], out


def test_p2_missing_zxing_flagged():
    """#35 核心红测：契约要求 zxing:core，pom 只声明 lombok → 缺失=zxing core。"""
    deps = [{"module": "ruoyi-alarm", "artifacts": ["com.google.zxing:core:3.5.1", "org.projectlombok:lombok"]}]
    out = _mdd(deps, ["ruoyi-alarm/pom.xml"], disk={"ruoyi-alarm/pom.xml": _POM_MISSING_ZXING})
    assert len(out) == 1, out
    assert out[0]["coordinate"] == "com.google.zxing:core:3.5.1"
    assert out[0]["manifest"] == "ruoyi-alarm/pom.xml"
    assert out[0]["module"] == "ruoyi-alarm"


def test_p2_managed_only_still_missing():
    """R4 反面：坐标只在 <dependencyManagement>（受管非 direct）→ 仍算缺失（模块自身未 direct 声明）。"""
    deps = [{"module": "ruoyi-alarm", "artifacts": ["com.google.zxing:core:3.5.1"]}]
    out = _mdd(deps, ["ruoyi-alarm/pom.xml"], disk={"ruoyi-alarm/pom.xml": _POM_MANAGED_ONLY})
    assert len(out) == 1 and out[0]["coordinate"] == "com.google.zxing:core:3.5.1", out


def test_p2_exclusion_artifactid_not_counted_present():
    """R6：坐标名只作为别的依赖的 <exclusion> 出现 → 不算 present（仍判缺失）。"""
    # 契约要 zxing:core；pom 里 'core' 只作为 spring-core 的 exclusion 出现 → 不是 direct dep。
    deps = [{"module": "ruoyi-alarm", "artifacts": ["com.google.zxing:core"]}]
    out = _mdd(deps, ["ruoyi-alarm/pom.xml"], disk={"ruoyi-alarm/pom.xml": _POM_EXCLUSION_ONLY})
    assert len(out) == 1, out


def test_p2_bare_artifact_spec_matches():
    """坐标裸名（无 group）也按尾名匹配。"""
    deps = [{"module": "ruoyi-alarm", "artifacts": ["lombok"]}]
    out = _mdd(deps, ["ruoyi-alarm/pom.xml"], disk={"ruoyi-alarm/pom.xml": _POM_WITH})
    assert out == [], out


# ════════════════ fail-open 路径 ════════════════

def test_p2_empty_contract_deps_failopen():
    """空契约依赖（老 checkpoint / 非 scaffold 子任务）→ []（no-op）。"""
    assert _mdd([], ["ruoyi-alarm/pom.xml"], disk={"ruoyi-alarm/pom.xml": _POM_MISSING_ZXING}) == []
    assert _mdd(None, ["ruoyi-alarm/pom.xml"], disk={"ruoyi-alarm/pom.xml": _POM_MISSING_ZXING}) == []


def test_p2_manifest_unreadable_failopen():
    """manifest 读不到（infra/未回拉）→ 跳过（绝不当"零依赖"误杀）。"""
    deps = [{"module": "ruoyi-alarm", "artifacts": ["com.google.zxing:core"]}]
    out = _mdd(deps, ["ruoyi-alarm/pom.xml"], disk={})  # read 返回 None
    assert out == [], out


def test_p2_manifest_empty_text_failopen():
    """manifest 读到纯空白 → 跳过（不当零依赖）。"""
    deps = [{"module": "ruoyi-alarm", "artifacts": ["com.google.zxing:core"]}]
    out = _mdd(deps, ["ruoyi-alarm/pom.xml"], disk={"ruoyi-alarm/pom.xml": "   \n  "})
    assert out == [], out


def test_p2_truncated_pom_failopen():
    """复核 HIGH 治本红测：pom 非空但【截断】（<dependencies> 开而无 </project>）→ regex 解析
    返回空≠零依赖 → 必须 fail-open 跳过，绝不把契约坐标全判缺失冤杀（Phase1 F1b 同类）。"""
    truncated = ("<project>\n  <artifactId>ruoyi-alarm</artifactId>\n  <dependencies>\n"
                 "    <dependency><groupId>org.projectlombok</groupId>"
                 "<artifactId>lombok</artifactId></dependency>\n")  # 无 </dependencies></project>
    deps = [{"module": "ruoyi-alarm", "artifacts": ["com.google.zxing:core", "org.projectlombok:lombok"]}]
    out = _mdd(deps, ["ruoyi-alarm/pom.xml"], disk={"ruoyi-alarm/pom.xml": truncated})
    assert out == [], out  # 截断 → 整闸跳，不误杀


def test_p2_truncated_gomod_failopen():
    """go.mod require 块截断（左括号无右）→ fail-open 跳。"""
    truncated = "module example.com/app\n\ngo 1.21\n\nrequire (\n\tgithub.com/gin-gonic/gin v1.9.1\n"
    deps = [{"module": "app", "artifacts": ["gorm.io/gorm"]}]
    out = _mdd(deps, ["app/go.mod"], disk={"app/go.mod": truncated})
    assert out == [], out


def test_p2_manifest_complete_sentinel():
    """_manifest_complete：maven 无 </project> → False；完整 → True。"""
    from swarm.worker.l1_pipeline import _manifest_complete
    assert _manifest_complete("maven", _POM_WITH) is True
    assert _manifest_complete("maven", "<project><dependencies>") is False
    assert _manifest_complete("go", "module x\nrequire (\ngithub.com/a/b v1\n)\n") is True
    assert _manifest_complete("go", "module x\nrequire (\ngithub.com/a/b v1\n") is False


def test_p2_go_block_no_paren_token():
    """复核 LOW 治本：块形态 go.mod 不把开括号 `(` 当模块路径。"""
    from swarm.worker.l1_pipeline import _go_declared_modules
    ids = _go_declared_modules("module x\nrequire (\n\tgithub.com/gin-gonic/gin v1.9.1\n)\n")
    assert "(" not in ids and "github.com/gin-gonic/gin" in ids, ids


# ════════════════ 复核 HIGH：R58-1 改名模块归属（物理 dir / 独占配对）════════════════

def test_p2_renamed_module_dir_match_flags_missing():
    """契约 module=逻辑标签 alarm-admin，dir=物理 ruoyi-admin；产出 ruoyi-admin/pom.xml 缺 zxing
    → 按物理 dir 归属命中 → 判缺失（旧行为：标签≠目录名 → 静默跳过=假过）。"""
    deps = [{"module": "alarm-admin", "dir": "ruoyi-admin",
             "artifacts": ["com.google.zxing:core:3.5.1"]}]
    out = _mdd(deps, ["ruoyi-admin/pom.xml"], disk={"ruoyi-admin/pom.xml": _POM_MISSING_ZXING})
    assert len(out) == 1 and out[0]["coordinate"] == "com.google.zxing:core:3.5.1", out


def test_p2_renamed_module_sole_pair_trust():
    """老 checkpoint 无 dir：契约仅一 entry（改名标签）+ scope 仅一候选 manifest → 按独占归属
    信任 → 仍判缺失（不靠猜标签）。"""
    deps = [{"module": "alarm-admin", "artifacts": ["com.google.zxing:core"]}]  # 无 dir
    out = _mdd(deps, ["ruoyi-admin/pom.xml"], disk={"ruoyi-admin/pom.xml": _POM_MISSING_ZXING})
    assert len(out) == 1, out


def test_p2_two_modules_no_sole_pair_uses_label():
    """两 entry/两 manifest（非独占）→ 回退标签/dir 严格匹配，不误配。"""
    deps = [{"module": "a", "dir": "a", "artifacts": ["com.google.zxing:core"]},
            {"module": "b", "dir": "b", "artifacts": ["org.projectlombok:lombok"]}]
    disk = {"a/pom.xml": _POM_MISSING_ZXING, "b/pom.xml": _POM_WITH}
    out = _mdd(deps, ["a/pom.xml", "b/pom.xml"], disk=disk)
    # a 缺 zxing（判缺失）；b 有 lombok（不缺）——不跨模块错配
    assert len(out) == 1 and out[0]["module"] == "a", out


def test_p2_unknown_stack_skipped():
    """scope manifest 是未知栈（无 verifier）→ 跳过。"""
    deps = [{"module": "svc", "artifacts": ["whatever"]}]
    out = _mdd(deps, ["svc/Gemfile"], disk={"svc/Gemfile": "gem 'rails'"})
    assert out == [], out


def test_p2_dir_mismatch_skips():
    """R1（有 ground-truth dir）：entry dir=ruoyi-alarm，但 scope 里只有 ruoyi-system/pom.xml
    （本模块自己的 pom 不在 scope）→ 物理目录确不匹配 → 跳（fail-open，绝不跨模块核验）。
    有 dir 时不被 sole_pair 覆盖——dir 是 ground truth。"""
    deps = [{"module": "ruoyi-alarm", "dir": "ruoyi-alarm", "artifacts": ["com.google.zxing:core"]}]
    out = _mdd(deps, ["ruoyi-system/pom.xml"], disk={"ruoyi-system/pom.xml": _POM_MISSING_ZXING})
    assert out == [], out


def test_p2_exempt_manifest_skipped():
    """白名单豁免（H1 权威模板/repaired）→ 不核验。"""
    deps = [{"module": "ruoyi-alarm", "artifacts": ["com.google.zxing:core"]}]
    out = _mdd(deps, ["ruoyi-alarm/pom.xml"], disk={"ruoyi-alarm/pom.xml": _POM_MISSING_ZXING},
               exempt={"ruoyi-alarm/pom.xml"})
    assert out == [], out


def test_p2_read_raises_failopen():
    """read 抛异常 → 跳过（fail-open）。"""
    from swarm.worker.l1_pipeline import missing_declared_dependencies

    def _boom(_rel):
        raise RuntimeError("sandbox unreachable")

    deps = [{"module": "ruoyi-alarm", "artifacts": ["com.google.zxing:core"]}]
    out = missing_declared_dependencies(deps, ["ruoyi-alarm/pom.xml"], read=_boom)
    assert out == [], out


# ════════════════ 栈中立：npm / go / gradle / cargo verifier ════════════════

def test_p2_npm_declared_and_missing():
    """npm：package.json 声明 axios，缺 lodash。"""
    pkg = '{"name":"web","dependencies":{"axios":"^1.6.0"},"devDependencies":{"vite":"^5"}}'
    deps = [{"module": "web", "artifacts": ["axios", "lodash"]}]
    out = _mdd(deps, ["web/package.json"], disk={"web/package.json": pkg})
    assert len(out) == 1 and out[0]["coordinate"] == "lodash", out


def test_p2_npm_scoped_package():
    """npm：@scope/pkg 全名匹配（尾名不截）。"""
    pkg = '{"name":"web","dependencies":{"@nestjs/core":"^10"}}'
    deps = [{"module": "web", "artifacts": ["@nestjs/core"]}]
    out = _mdd(deps, ["web/package.json"], disk={"web/package.json": pkg})
    assert out == [], out


def test_p2_npm_malformed_json_failopen():
    """npm：package.json 解析异常 → 跳过（fail-open）。"""
    deps = [{"module": "web", "artifacts": ["axios"]}]
    out = _mdd(deps, ["web/package.json"], disk={"web/package.json": "{ not json"})
    assert out == [], out


def test_p2_go_declared_and_missing():
    """go：go.mod require 块声明 gin，缺 gorm。"""
    gomod = ("module example.com/app\n\ngo 1.21\n\nrequire (\n"
             "\tgithub.com/gin-gonic/gin v1.9.1\n)\n")
    deps = [{"module": "app", "artifacts": ["github.com/gin-gonic/gin", "gorm.io/gorm"]}]
    out = _mdd(deps, ["app/go.mod"], disk={"app/go.mod": gomod})
    assert len(out) == 1 and out[0]["coordinate"] == "gorm.io/gorm", out


def test_p2_go_single_line_require():
    """go：单行 require path v 形态也识别。"""
    gomod = "module example.com/app\n\ngo 1.21\n\nrequire github.com/gin-gonic/gin v1.9.1\n"
    deps = [{"module": "app", "artifacts": ["github.com/gin-gonic/gin"]}]
    out = _mdd(deps, ["app/go.mod"], disk={"app/go.mod": gomod})
    assert out == [], out


def test_p2_gradle_declared():
    """gradle：implementation 'g:a:v' 声明识别（尾名 artifactId）。"""
    gradle = "dependencies {\n  implementation 'com.google.zxing:core:3.5.1'\n}\n"
    deps = [{"module": "app", "artifacts": ["com.google.zxing:core", "org.projectlombok:lombok"]}]
    out = _mdd(deps, ["app/build.gradle"], disk={"app/build.gradle": gradle})
    assert len(out) == 1 and out[0]["coordinate"] == "org.projectlombok:lombok", out


def test_p2_cargo_declared():
    """cargo：[dependencies] 表键识别。"""
    cargo = '[package]\nname = "app"\n\n[dependencies]\nserde = "1.0"\ntokio = { version = "1" }\n'
    deps = [{"module": "app", "artifacts": ["serde", "reqwest"]}]
    out = _mdd(deps, ["app/Cargo.toml"], disk={"app/Cargo.toml": cargo})
    assert len(out) == 1 and out[0]["coordinate"] == "reqwest", out


# ════════════════ verifier 直测 ════════════════

def test_p2_verifier_maven_direct_only():
    """maven verifier 只取 direct 依赖 artifactId（排除 depMgmt/parent）。"""
    from swarm.worker.l1_pipeline import _maven_declared_artifact_ids
    ids = _maven_declared_artifact_ids(_POM_MANAGED_ONLY)
    assert "lombok" in ids and "core" not in ids, ids  # zxing core 只在 depMgmt


def test_p2_verifier_npm_all_sections():
    """npm verifier 收 dependencies + devDependencies + peer/optional。"""
    from swarm.worker.l1_pipeline import _npm_declared_deps
    pkg = ('{"dependencies":{"a":"1"},"devDependencies":{"b":"1"},'
           '"peerDependencies":{"c":"1"},"optionalDependencies":{"d":"1"}}')
    assert _npm_declared_deps(pkg) == {"a", "b", "c", "d"}


# ════════════════ 接线：_deterministic_l1_gate ════════════════

from unittest.mock import patch  # noqa: E402

from swarm.types import FileScope, SubTask, SubTaskDifficulty, SubTaskModality  # noqa: E402
from swarm.worker.executor import WorkerExecutor  # noqa: E402


def _mk_executor_dep(scope, contract, *, project_path="/tmp/swarm-p2-test"):
    st = SubTask(
        id="st-scaffold-ruoyi-alarm",
        description="脚手架：ruoyi-alarm 模块 pom",
        difficulty=SubTaskDifficulty.TRIVIAL,
        modality=SubTaskModality.TEXT,
        scope=scope,
        intent="create",
        contract=contract,
    )
    return WorkerExecutor(subtask=st, project_path=project_path)


def _modify_diff(rel: str) -> str:
    return f"--- a/{rel}\n+++ b/{rel}\n@@ -1 +1 @@\n-old\n+new\n"


def test_p2_wiring_missing_dep_fails_capability():
    """接线红测：契约要 zxing:core，产出 pom（盘上）缺声明 → 确定性闸判 False，
    reason=declared_dependency_missing，归因自身 capability（非 BLOCKED）。"""
    scope = FileScope(writable=["ruoyi-alarm/pom.xml"])
    contract = {"dependencies": [{"module": "ruoyi-alarm",
                                  "artifacts": ["com.google.zxing:core:3.5.1"]}]}
    ex = _mk_executor_dep(scope, contract)
    with patch.object(ex, "_get_git_diff", return_value=_modify_diff("ruoyi-alarm/pom.xml")), \
            patch("swarm.worker.l1_pipeline._run_check_split",
                  return_value=(0, _POM_MISSING_ZXING, "")):
        det_ok, details = ex._deterministic_l1_gate()
    assert det_ok is False, details
    assert details.get("reason") == "declared_dependency_missing", details
    assert details.get("missing_dependencies"), details
    assert details["missing_dependencies"][0]["coordinate"] == "com.google.zxing:core:3.5.1"
    assert details.get("not_run_kind") is None, details  # capability，非 BLOCKED


def test_p2_wiring_all_declared_not_flagged():
    """接线：产出 pom 声明了全部契约坐标 → 新闸不拦，放行进 pipeline（patch 短路避免真构建）。"""
    scope = FileScope(writable=["ruoyi-alarm/pom.xml"])
    contract = {"dependencies": [{"module": "ruoyi-alarm",
                                  "artifacts": ["com.google.zxing:core", "org.projectlombok:lombok"]}]}
    ex = _mk_executor_dep(scope, contract)
    with patch.object(ex, "_get_git_diff", return_value=_modify_diff("ruoyi-alarm/pom.xml")), \
            patch("swarm.worker.l1_pipeline._read_project_file", return_value=_POM_WITH), \
            patch("swarm.worker.l1_pipeline.run_l1_pipeline",
                  return_value=(True, {"deterministic_gate": "pass"})):
        det_ok, details = ex._deterministic_l1_gate()
    assert details.get("reason") != "declared_dependency_missing", details


def test_p2_wiring_no_contract_deps_skips():
    """接线：子任务无 contract dependencies（非 scaffold/老 checkpoint）→ 闸不触发。"""
    scope = FileScope(writable=["ruoyi-alarm/pom.xml"])
    ex = _mk_executor_dep(scope, {})
    with patch.object(ex, "_get_git_diff", return_value=_modify_diff("ruoyi-alarm/pom.xml")), \
            patch("swarm.worker.l1_pipeline.run_l1_pipeline",
                  return_value=(True, {"deterministic_gate": "pass"})):
        det_ok, details = ex._deterministic_l1_gate()
    assert details.get("reason") != "declared_dependency_missing", details


# ════════════════ 渲染 + 归因分类 ════════════════

def test_p2_det_fail_reason_renders():
    """_det_fail_reason 机读账列出 manifest←coordinate。"""
    from swarm.worker.l1_verdict import _det_fail_reason
    d = {"reason": "declared_dependency_missing",
         "missing_dependencies": [{"manifest": "a/pom.xml", "coordinate": "g:core", "module": "a"}]}
    r = _det_fail_reason(d)
    assert "declared_dependency_missing×1" in r and "a/pom.xml" in r, r


def test_p2_det_fail_source_sticky_not_flippable():
    """归因：declared_dependency_missing 走确定性兜底 → sticky（不在 _FLIPPABLE_SOURCES）。"""
    from swarm.worker.l1_verdict import _FLIPPABLE_SOURCES, _det_fail_source
    src, _reason = _det_fail_source(
        {"reason": "declared_dependency_missing", "deterministic_gate": "fail",
         "missing_dependencies": [{"manifest": "a/pom.xml", "coordinate": "g:core"}]})
    assert src not in _FLIPPABLE_SOURCES, src  # 永不翻盘（capability 真错误）
