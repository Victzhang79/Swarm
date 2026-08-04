"""P-M1（27 号文）：package.json 补进 `_MANIFEST_BACKEND`——npm 证据不再空交 LLM。

治前：纯 npm 工程 `signals.manifests=[]`、evidence 缺清单行、`frontend_kind=="none"
时置信罚 -0.3（0.75→0.45）→ 恒触发 needs_model_adjudication——「拿空证据交 LLM
裁决」正是 `_scan_failed_profile` 要防的幻觉产地。

治法定案（消费契约分档，血规 10③）：package.json 进表 → 收进 manifests
（signals/evidence/置信判据/指纹），但【不进后端仲裁候选】——它是唯一同时充当
前端子工程清单的清单名，仲裁按全仓源文件数投票时，前端 .ts/.vue 票仓会把分离
工程的 backend 从 python/java 翻成 npm。纯 npm 工程仍由 manifest_texts fallback
定栈（与治前逐字节一致）。
"""
from __future__ import annotations

import json
import os

from swarm.brain.stack_detect import _MANIFEST_BACKEND, detect_stack_deterministic


def _mk(tmp, rel, content="x"):
    p = os.path.join(str(tmp), rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(content)


def test_package_json_in_manifest_backend_table():
    """表内事实：value 域与 _LANG_SOURCE_EXTS 键同名（表完整性由
    test_g_batch_upstream_fidelity 的同源测试强制）。"""
    assert _MANIFEST_BACKEND["package.json"] == ("javascript/typescript", "npm")


def test_pure_npm_project_no_longer_adjudicated(tmp_path):
    """主治锁：纯 npm 工程（无前端）置信 0.75、不再白烧 LLM 裁决、证据面有清单。"""
    _mk(tmp_path, "package.json",
        json.dumps({"name": "api", "dependencies": {"express": "^4"}}))
    for i in range(5):
        _mk(tmp_path, f"src/mod{i}.ts", "export {}")
    p = detect_stack_deterministic(str(tmp_path))
    assert p["build"] == "npm"
    assert "javascript" in p["backend"]
    assert p["signals"]["manifests"] == ["package.json"]
    assert any("构建/清单文件" in e and "package.json" in e for e in p["evidence"])
    assert p["confidence"] >= 0.65, "治前=0.45（frontend none + 零清单 -0.3 罚）"
    assert p["needs_model_adjudication"] is False, \
        "npm 工程证据已足，绝不许再拿空证据白烧 LLM 裁决（幻觉产地）"


def test_root_package_json_does_not_hijack_backend_arbitration(tmp_path):
    """★消费契约锁（本项最关键的误杀方向）★：django+react 根布局——根 package.json
    是【前端】清单，backend/ 才是 API。若 package.json 进仲裁候选，前端 .tsx 票仓
    （8:3 压过 .py）会把 backend 翻成 npm → 给 django 工程下发 npm build。"""
    _mk(tmp_path, "package.json", json.dumps({"dependencies": {"react": "^18"}}))
    _mk(tmp_path, "backend/requirements.txt", "django>=4")
    _mk(tmp_path, "backend/manage.py", "x")
    for i in range(3):
        _mk(tmp_path, f"backend/app/m{i}.py")
    for i in range(8):   # 前端票仓刻意大于后端
        _mk(tmp_path, f"src/components/c{i}.tsx")
    p = detect_stack_deterministic(str(tmp_path))
    assert "python" in p["backend"].lower() or "Django" in p["backend"]
    assert p["build"] == "pip", f"backend 被前端票仓劫持: {p['backend']}/{p['build']}"


def test_subdir_package_json_still_counts_as_manifest_evidence(tmp_path):
    """子目录 package.json 同样进 manifests（evidence/signals），但不进仲裁候选：
    RuoYi 式分离工程 backend 保持 java/maven（与治前逐字节一致）。"""
    _mk(tmp_path, "pom.xml", "<project>thymeleaf</project>")
    _mk(tmp_path, "ruoyi-ui/package.json", json.dumps({"dependencies": {"vue": "^2"}}))
    _mk(tmp_path, "ruoyi-ui/src/views/x.vue", "<template/>")
    _mk(tmp_path, "ruoyi-admin/src/main/resources/templates/index.html", "<html/>")
    p = detect_stack_deterministic(str(tmp_path))
    assert p["build"] == "maven"
    assert "ruoyi-ui/package.json" in p["signals"]["manifests"]
    assert p["frontend_kind"] == "separated"


def test_multi_manifest_arbitration_unchanged_by_package_json(tmp_path):
    """G-H6 回归：pom+go.mod+package.json 并存仍按源文件数裁 java——package.json
    不进候选，裁决行为与治前逐字节一致。"""
    _mk(tmp_path, "pom.xml", "<project/>")
    _mk(tmp_path, "go.mod", "module x")
    _mk(tmp_path, "package.json", "{}")
    for n in "ABC":
        _mk(tmp_path, f"src/main/java/{n}.java")
    p = detect_stack_deterministic(str(tmp_path))
    assert "java" in p["backend"].lower() and p["build"] == "maven"
    assert p["needs_model_adjudication"] is False
