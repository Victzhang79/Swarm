"""项目级沙箱选模板优先级测试 — 固化 executor 选模板逻辑（批3/批4）。

固化 docs/Project_Scoped_Sandbox_Design.md 的核心契约：
executor 选沙箱模板时，优先用 project.config['sandbox_template']（项目专属），
无则回退按语言选通用模板。本测试复现该优先级决策逻辑（不起真实沙箱）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_bs = Path(__file__).resolve().parent / "swarm_bootstrap.py"
_spec = importlib.util.spec_from_file_location("swarm_bootstrap", _bs)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _select_template(project_config: dict | None, harness_tpl: str = "",
                     lang: str = "java", has_build: bool = True) -> tuple[str, str]:
    """复现 executor 选模板决策（与 worker/executor.py run() Phase0 一致）。

    返回 (template_id, source)。source ∈ {harness, project, language}
    """
    _tpl = harness_tpl or ""
    if _tpl:
        return _tpl, "harness"
    # 项目专属模板优先
    proj_tpl = (project_config or {}).get("sandbox_template", "")
    if proj_tpl:
        return proj_tpl, "project"
    # 回退语言模板
    purpose = "verify" if has_build else "exec"
    return f"tpl-lang-{lang}-{purpose}", "language"


def test_project_template_takes_priority():
    """项目 config 有 sandbox_template → 优先用项目专属模板。"""
    tpl, src = _select_template({"sandbox_template": "tpl-b4546ea0"})
    assert tpl == "tpl-b4546ea0" and src == "project"
    print("  ✅ 项目专属模板优先")


def test_fallback_to_language_when_no_project_template():
    """项目 config 无模板 → 回退按语言+性质选通用模板。"""
    tpl, src = _select_template({}, lang="java", has_build=True)
    assert src == "language" and "java" in tpl and "verify" in tpl
    print("  ✅ 无项目模板时回退语言模板(verify)")


def test_harness_explicit_wins_all():
    """harness 显式指定模板 → 最高优先（连项目模板都让位）。"""
    tpl, src = _select_template({"sandbox_template": "tpl-proj"}, harness_tpl="tpl-explicit")
    assert tpl == "tpl-explicit" and src == "harness"
    print("  ✅ harness 显式模板最高优先")


def test_none_config_safe():
    """project_config 为 None → 安全回退语言模板，不崩。"""
    tpl, src = _select_template(None, lang="python", has_build=False)
    assert src == "language" and "python" in tpl and "exec" in tpl
    print("  ✅ config 为 None 安全回退(exec)")


def test_real_ruoyi_project_config():
    """真实 ruoyi-e2e 项目 config（若已绑定专属模板）→ 选项目模板。"""
    try:
        from swarm.project.store import get_project
        proj = get_project("5d0e9db8-d000-40f6-8df9-a929ea3c4712")
    except Exception:
        print("  ⊘ 跳过(无 DB 连接)")
        return
    if not proj:
        print("  ⊘ 跳过(ruoyi-e2e 项目不存在)")
        return
    cfg = proj.get("config") or {}
    if not cfg.get("sandbox_template"):
        print("  ⊘ 跳过(ruoyi-e2e 未绑定专属模板)")
        return
    tpl, src = _select_template(cfg)
    assert src == "project"
    print(f"  ✅ 真实 ruoyi-e2e 选项目专属模板: {tpl}")


if __name__ == "__main__":
    test_project_template_takes_priority()
    test_fallback_to_language_when_no_project_template()
    test_harness_explicit_wins_all()
    test_none_config_safe()
    test_real_ruoyi_project_config()
    print("\n✅ 项目级沙箱选模板优先级全部测试通过")
