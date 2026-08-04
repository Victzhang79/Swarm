"""X-M9（27 号文）：H1 权威模板落盘多栈化——非 pom 模板也确定性 write-through。

治前：`_enforce_authoritative_template` 只认「权威 pom 模板」+ pom.xml 落点 +
```xml 围栏——P-H4 五栈 driver（npm/go/python/cargo/gradle）产的权威模板在 worker
侧零 deterministic write-through，机械产物照旧赌 LLM 服从度（round48c 徒手改写
pom 的同型死法在异栈敞着）。

治法：标记正则通用（目标路径从标记捕获，与 creates 交叉验证）+ 已知清单集从
STACK_SPEC 派生（module_manifest ∪ 别名）+ 按 basename 最小形状校验（fail-closed：
认不得/形状不符不落盘）+ pom 臂逐字节不变。
"""
from __future__ import annotations

import json
import os

from swarm.types import FileScope, SubTask, SubTaskDifficulty
from swarm.worker.executor_l1gate import (
    _H1_KNOWN_MANIFEST_BASENAMES,
    _L1GateMixin,
)


class _Host:
    """轻量宿主：H1 方法只消费 subtask/project_path/_log/_h1_enforced_templates。"""

    def __init__(self, subtask, project_path):
        self.subtask = subtask
        self.project_path = project_path
        self.logs = []

    def _log(self, msg):
        self.logs.append(str(msg))


def _st(desc, *, create=None, writable=None):
    return SubTask(id="st-1", description=desc,
                   difficulty=SubTaskDifficulty.TRIVIAL,
                   scope=FileScope(create_files=create or [], writable=writable or [],
                                   readable=[]),
                   acceptance_criteria=[])


def _mk(root, rel, content):
    p = os.path.join(str(root), rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(content)


def _run(host):
    _L1GateMixin._enforce_authoritative_template(host)


def _npm_block(rel, body):
    return (f"\n【权威 package.json 模板（确定性生成，原样写入 {rel}；仅当项目另有"
            f"明确约定才允许在此基础上增改）】\n```json\n{body}\n```")


def test_known_manifest_set_derived_from_stack_spec():
    """单一事实源锁：已知清单集=STACK_SPEC 派生（加栈=加表行，本处零改动）。"""
    from swarm.stacks import STACK_SPEC
    expect = {mf for s in STACK_SPEC.values()
              for mf in (s.module_manifest, *s.module_extra_manifests)}
    assert _H1_KNOWN_MANIFEST_BASENAMES == frozenset(expect)
    assert {"pom.xml", "package.json", "go.mod", "pyproject.toml",
            "Cargo.toml", "build.gradle", "build.gradle.kts"} <= set(expect)


def test_npm_template_written_through(tmp_path):
    """★主治锁★：package.json 权威模板确定性覆写 CREATE 目标（治前恒不触发）。"""
    tpl = json.dumps({"name": "web", "dependencies": {"express": "^4.18.2"}},
                     indent=2)
    _mk(tmp_path, "web/package.json", '{"name": "web", "dependencies": {"express": "latest"}}')
    host = _Host(_st("任务\n" + _npm_block("web/package.json", tpl),
                     create=["web/package.json"]), str(tmp_path))
    _run(host)
    with open(os.path.join(str(tmp_path), "web/package.json")) as f:
        assert json.load(f)["dependencies"]["express"] == "^4.18.2", \
            "worker 徒手的 latest 必须被权威模板覆写（不赌 LLM 服从度）"
    assert host._h1_enforced_templates["web/package.json"] == tpl.strip()


def test_go_mod_template_bare_fence(tmp_path):
    """go 臂：裸围栏（``` 无语言标签）也认 + `module ` 前缀形状校验。"""
    tpl = "module example.com/web\n\ngo 1.22\n"
    _mk(tmp_path, "web/go.mod", "module wrong\n")
    desc = (f"\n【权威 go.mod 模板（确定性生成，原样写入 web/go.mod）】"
            f"\n```\n{tpl}\n```")
    host = _Host(_st("任务\n" + desc, create=["web/go.mod"]), str(tmp_path))
    _run(host)
    with open(os.path.join(str(tmp_path), "web/go.mod")) as f:
        assert f.read().strip() == tpl.strip()


def test_pyproject_toml_fence(tmp_path):
    """python 臂：```toml 围栏 + 非空校验（登记边界=无统一首行可断）。"""
    tpl = '[project]\nname = "svc"\ndependencies = ["django>=4"]\n'
    _mk(tmp_path, "svc/pyproject.toml", "[project]\nname = \"svc\"\n")
    desc = (f"\n【权威 pyproject.toml 模板（确定性生成，原样写入 svc/pyproject.toml；"
            f"仅当项目另有明确约定才允许在此基础上增改）】\n```toml\n{tpl}\n```")
    host = _Host(_st("任务\n" + desc, create=["svc/pyproject.toml"]), str(tmp_path))
    _run(host)
    with open(os.path.join(str(tmp_path), "svc/pyproject.toml")) as f:
        assert 'dependencies = ["django>=4"]' in f.read()


def test_pom_arm_byte_identical(tmp_path):
    """★回归锁★：pom 臂与治前逐字节一致（```xml 围栏 + <?xml/<project 校验）。"""
    tpl = '<?xml version="1.0"?>\n<project><artifactId>m</artifactId></project>'
    _mk(tmp_path, "m/pom.xml", "<project/>")
    desc = (f"\n【权威 pom 模板（确定性生成，原样写入 m/pom.xml）】\n```xml\n{tpl}\n```")
    host = _Host(_st("任务\n" + desc, create=["m/pom.xml"]), str(tmp_path))
    _run(host)
    with open(os.path.join(str(tmp_path), "m/pom.xml")) as f:
        assert f.read().strip() == tpl
    assert host._h1_enforced_templates["m/pom.xml"] == tpl


def test_unknown_manifest_fail_closed(tmp_path):
    """fail-closed：认不得的清单名（不在 STACK_SPEC 派生集）绝不落盘。

    ★夹具为什么用 gradle 形状的内容★（R2 整改）：模板体若以 `all:` 开头，即使把
    已知清单闸整删（突变 XM9-b），落底的 gradle 形状校验也会把它拦下——两道闸
    互相兜底=任一单独突变都仍绿（冗余防御不可证伪血泪）。用「能过落底校验」的
    错位内容（多块描述里围栏截取错位的真实形态），已知清单闸才可独立证伪。"""
    _mk(tmp_path, "x/Makefile", "all:\n\techo old\n")
    desc = ("\n【权威 Makefile 模板（确定性生成，原样写入 x/Makefile）】"
            "\n```\nplugins {\n    id 'java'\n}\n```")
    host = _Host(_st("任务\n" + desc, create=["x/Makefile"]), str(tmp_path))
    _run(host)
    with open(os.path.join(str(tmp_path), "x/Makefile")) as f:
        assert "echo old" in f.read(), "未收录清单名的模板绝不许确定性覆写"
    assert not getattr(host, "_h1_enforced_templates", None)


def test_malformed_package_json_not_written(tmp_path):
    """fail-closed：package.json 模板不是合法 JSON（围栏截取错位）→ 不落盘。"""
    _mk(tmp_path, "web/package.json", '{"name": "web"}')
    host = _Host(_st("任务\n" + _npm_block("web/package.json", "{not json"),
                     create=["web/package.json"]), str(tmp_path))
    _run(host)
    with open(os.path.join(str(tmp_path), "web/package.json")) as f:
        assert json.load(f)["name"] == "web"
    assert not getattr(host, "_h1_enforced_templates", None)


def test_modify_form_not_written(tmp_path):
    """CREATE-only 守约：scope 带 writable（MODIFY 形态）→ 覆写=clobber，绝不落盘
    （R41-F5 铁律，逐字节保持）。"""
    _mk(tmp_path, "web/package.json", '{"name": "web"}')
    tpl = json.dumps({"name": "web", "dependencies": {"express": "^4"}})
    host = _Host(_st("任务\n" + _npm_block("web/package.json", tpl),
                     create=["web/package.json"],
                     writable=["web/src/index.ts"]), str(tmp_path))
    _run(host)
    with open(os.path.join(str(tmp_path), "web/package.json")) as f:
        assert "dependencies" not in json.load(f)


def test_multiple_manifest_blocks_each_written(tmp_path):
    """一子任务带多个清单模板：逐块处理各落各的（npm+go 双清单脚手架形态）。"""
    tpl_json = json.dumps({"name": "web", "dependencies": {}})
    tpl_go = "module example.com/api\n\ngo 1.22\n"
    _mk(tmp_path, "web/package.json", "{}")
    _mk(tmp_path, "api/go.mod", "module wrong\n")
    desc = ("任务\n" + _npm_block("web/package.json", tpl_json)
            + f"\n【权威 go.mod 模板（确定性生成，原样写入 api/go.mod）】"
              f"\n```\n{tpl_go}\n```")
    host = _Host(_st(desc, create=["web/package.json", "api/go.mod"]), str(tmp_path))
    _run(host)
    with open(os.path.join(str(tmp_path), "web/package.json")) as f:
        assert json.load(f)["name"] == "web"
    with open(os.path.join(str(tmp_path), "api/go.mod")) as f:
        assert f.read().strip() == tpl_go.strip()


def test_idempotent_when_content_matches(tmp_path):
    """幂等：目标内容已等于模板 → 登记但不重写（log 无覆写记录）。"""
    tpl = "module example.com/web\n\ngo 1.22\n"
    _mk(tmp_path, "web/go.mod", tpl + "\n")
    desc = (f"\n【权威 go.mod 模板（确定性生成，原样写入 web/go.mod）】"
            f"\n```\n{tpl}\n```")
    host = _Host(_st("任务\n" + desc, create=["web/go.mod"]), str(tmp_path))
    _run(host)
    assert host._h1_enforced_templates["web/go.mod"] == tpl.strip()
    assert not any("确定性落盘" in m for m in host.logs), "幂等命中不该报覆写"


# ────────────────────────────────────────────────────────────────────────────
# R2 整改锁（双复核 R1：reviewer 3 MEDIUM + hunter 1 CRITICAL/1 HIGH/2 MEDIUM，
# 全部探针独立复现后治）
# ────────────────────────────────────────────────────────────────────────────

def test_same_basename_two_manifests_each_written(tmp_path):
    """★hunter R1 CRITICAL 锁★：同 basename 不同目录（web/api 双 package.json）
    必须各落各的——治前 basename 匹配 len(cands)=2 → 整栈全跳（生产常见形态：
    一个脚手架 owner 子任务带前后端两块清单）。"""
    _mk(tmp_path, "web/package.json", "{}")
    _mk(tmp_path, "api/package.json", "{}")
    desc = ("任务\n" + _npm_block("web/package.json", json.dumps({"name": "web"}))
            + _npm_block("api/package.json", json.dumps({"name": "api"})))
    host = _Host(_st(desc, create=["web/package.json", "api/package.json"]),
                 str(tmp_path))
    _run(host)
    with open(os.path.join(str(tmp_path), "web/package.json")) as f:
        assert json.load(f)["name"] == "web"
    with open(os.path.join(str(tmp_path), "api/package.json")) as f:
        assert json.load(f)["name"] == "api", \
            "同 basename 第二块也必须落盘（治前两块全跳）"
    assert set(host._h1_enforced_templates) == {"web/package.json", "api/package.json"}


def test_marker_path_mismatch_not_written(tmp_path):
    """★reviewer R1 MEDIUM-2 锁★：标记写 backend/package.json 而 create_files 只有
    frontend/package.json → 绝不按 basename 猜落点（治前会写进 frontend）。
    backend/ 目录留空在场：突变（回退 basename 匹配）有处可写才会红。"""
    _mk(tmp_path, "frontend/package.json", '{"name": "frontend"}')
    os.makedirs(os.path.join(str(tmp_path), "backend"))
    desc = ("任务\n" + _npm_block("backend/package.json", json.dumps({"name": "x"})))
    host = _Host(_st(desc, create=["frontend/package.json"]), str(tmp_path))
    _run(host)
    with open(os.path.join(str(tmp_path), "frontend/package.json")) as f:
        assert json.load(f)["name"] == "frontend", "路径不一致绝不写到别处"
    assert not os.path.exists(os.path.join(str(tmp_path), "backend", "package.json")), \
        "标记路径不在 create_files：连标记路径自己也不许写"
    assert not getattr(host, "_h1_enforced_templates", None)
    assert any("h1_skip:path_not_created" in m for m in host.logs), \
        "★hunter R1 HIGH 锁★：skip 必须机读可辨（h1_skip:<reason> 进执行日志）"


def test_package_json_array_rejected(tmp_path):
    """★reviewer R1 MEDIUM-3 锁★：package.json 模板是合法 JSON 但非 dict（`[]`）
    → 形状不符不落盘（治前 json.loads 成功即过）。"""
    _mk(tmp_path, "web/package.json", '{"name": "web"}')
    host = _Host(_st("任务\n" + _npm_block("web/package.json", "[]"),
                     create=["web/package.json"]), str(tmp_path))
    _run(host)
    with open(os.path.join(str(tmp_path), "web/package.json")) as f:
        assert json.load(f)["name"] == "web"
    assert not getattr(host, "_h1_enforced_templates", None)
    assert any("h1_skip:shape_mismatch" in m for m in host.logs)


def test_non_toml_pyproject_rejected(tmp_path):
    """★reviewer R1 MEDIUM-3 锁★：pyproject 形状=渲染器首行契约 `[project]`
    （治前非空即过，任意文本都覆写）。"""
    _mk(tmp_path, "svc/pyproject.toml", "[project]\nname = \"svc\"\n")
    desc = ("\n【权威 pyproject.toml 模板（确定性生成，原样写入 svc/pyproject.toml）】"
            "\n```toml\nnot toml at all\n```")
    host = _Host(_st("任务\n" + desc, create=["svc/pyproject.toml"]), str(tmp_path))
    _run(host)
    with open(os.path.join(str(tmp_path), "svc/pyproject.toml")) as f:
        assert 'name = "svc"' in f.read()
    assert not getattr(host, "_h1_enforced_templates", None)


def test_root_level_manifest_written(tmp_path):
    """★hunter R1 MEDIUM 补测试★：根级清单（路径无目录前缀）照常落盘。"""
    tpl = "module example.com/root\n\ngo 1.22\n"
    _mk(tmp_path, "go.mod", "module wrong\n")
    desc = (f"\n【权威 go.mod 模板（确定性生成，原样写入 go.mod）】"
            f"\n```\n{tpl}\n```")
    host = _Host(_st("任务\n" + desc, create=["go.mod"]), str(tmp_path))
    _run(host)
    with open(os.path.join(str(tmp_path), "go.mod")) as f:
        assert f.read().strip() == tpl.strip()


def test_renderer_output_passes_shape_check():
    """★渲染器同源锁★：形状校验判据=brain 渲染器首行契约——真渲染器产物必须
    过 `_h1_template_shape_ok`（渲染器首行改了而校验没同步，本条当场红）。"""
    from swarm.brain.contract_utils import (
        _render_build_gradle,
        _render_cargo_toml,
        _render_go_mod,
        _render_package_json,
        _render_pyproject_toml,
    )
    from swarm.worker.executor_l1gate import _h1_template_shape_ok
    cases = {
        "package.json": _render_package_json("web", []),
        "go.mod": _render_go_mod("example.com/web", "1.22", [], []),
        "pyproject.toml": _render_pyproject_toml("svc", []),
        "Cargo.toml": _render_cargo_toml("svc", [], []),
        "build.gradle": _render_build_gradle("groovy", [], []),
        "build.gradle.kts": _render_build_gradle("kts", [], []),
    }
    for base, tpl in cases.items():
        assert _h1_template_shape_ok(base, tpl.strip()), \
            f"渲染器产物必须过形状校验（{base}）——判据与渲染器首行漂移"


def test_unknown_manifest_skip_is_logged(tmp_path):
    """★hunter R1 HIGH 锁★：认不得的清单名 skip 必须留机读痕迹（治前全路径静默）。"""
    _mk(tmp_path, "x/Makefile", "all:\n\techo old\n")
    desc = ("\n【权威 Makefile 模板（确定性生成，原样写入 x/Makefile）】"
            "\n```\nplugins {\n    id 'java'\n}\n```")
    host = _Host(_st("任务\n" + desc, create=["x/Makefile"]), str(tmp_path))
    _run(host)
    assert any("h1_skip:unknown_manifest:Makefile" in m for m in host.logs)


def test_non_pom_h1_rels_skip_content_assert_in_pipeline(tmp_path):
    """★reviewer R1 MEDIUM-1 锁（wiring）★：`_h1_enforced_templates` → pipeline
    `template_enforced_rels` → `verify_skipped_h1` 这条豁免链对【非 pom】同样接上
    （R65D-T2④ 冤案防护在 go 臂的端到端证明，非只测实现）。"""
    from unittest.mock import patch

    from swarm.types import TaskHarness
    from swarm.worker.l1_pipeline import run_l1_pipeline
    tpl = "module example.com/web\n\ngo 1.22"
    _mk(tmp_path, "go.mod", tpl + "\n")
    st = SubTask(
        id="st-go", description="x", difficulty=SubTaskDifficulty.TRIVIAL,
        scope=FileScope(create_files=["go.mod"], writable=[], readable=[]),
        acceptance_criteria=[],
        harness=TaskHarness(verify_commands=["grep -q 'example.com/web' go.mod"]))
    diff = ("--- /dev/null\n+++ b/go.mod\n@@ -0,0 +1 @@\n+module example.com/web\n")
    with patch("swarm.worker.l1_pipeline._derive_full_build_command",
               lambda *a, **k: ""):  # 构建闸显式中立（同 test_r65d_t2 夹具纪律）
        ok, details = run_l1_pipeline(
            str(tmp_path), st, diff, timeout=30,
            template_enforced_rels={"go.mod": tpl})
    assert ok is True, f"H1 覆写的 go.mod 旧内容断言不得判死 worker: {details}"
    assert details.get("verify_skipped_h1"), \
        f"非 pom 豁免必须机读留痕 verify_skipped_h1: {details}"


def test_missing_created_files_exempt_is_stack_neutral():
    """★reviewer R1 MEDIUM-1 锁（#31-P1 臂）★：必建文件闸的 exempt 契约对非 pom
    rel 一视同仁（executor 侧 `_exempt |= _h1_enforced_templates.keys()` 是 generic
    集合并——本条锁的是 pipeline 侧 exempt 语义不按 pom 特判）。"""
    from swarm.worker.l1_pipeline import missing_created_files
    missing = missing_created_files(
        ["go.mod"], "", exists=lambda _rel: False, exempt={"go.mod"})
    assert missing == [], "H1 登记的非 pom rel 必须同享必建闸豁免"
    missing = missing_created_files(
        ["go.mod"], "", exists=lambda _rel: False, exempt=set())
    assert missing == ["go.mod"], "对照面：无豁免时确凿遗漏必须判出（防假绿夹具）"
