"""P1-6（CODEWALK_AUDIT_2026-07-06 批3）：dedupe_file_plan basename 去重丢多模块清单。

原 bug：P5 按 basename 去重防 LLM 在两模块重复建同名类（正当），但把"每模块一份"
是生态惯例的清单/构建/配置文件（moduleA/pom.xml + moduleB/pom.xml、多 package.json、
application.yml）也当重复静默丢弃 → 与 contract_utils 规则3"每模块 pom 各自独立"矛盾，
多模块脚手架残缺（后建模块无 pom → reactor 缺注册/编译失败）。
修：模块惯例文件名白名单只按完全路径去重；源码文件保持 basename 去重（P5 保留）。
"""
from __future__ import annotations

from swarm.brain.plan_batch import dedupe_file_plan


def _fp(path: str, **kw) -> dict:
    return {"path": path, "action": "create", "responsibility": "r", **kw}


def test_per_module_manifests_all_kept():
    fp = [
        _fp("moduleA/pom.xml", module="moduleA"),
        _fp("moduleB/pom.xml", module="moduleB"),
        _fp("web/package.json"),
        _fp("admin/package.json"),
        _fp("svcA/src/main/resources/application.yml"),
        _fp("svcB/src/main/resources/application.yml"),
        # 外部复核补遗：Python/C++/Go/前端工具链的每模块清单
        _fp("pkgA/pyproject.toml"),
        _fp("pkgB/pyproject.toml"),
        _fp("libA/CMakeLists.txt"),
        _fp("libB/CMakeLists.txt"),
        _fp("cmd/serverA/main.go"),
        _fp("cmd/serverB/main.go"),
    ]
    out = dedupe_file_plan(fp)
    paths = [x["path"] for x in out]
    assert len(out) == 12, f"每模块清单/配置不得被 basename 去重丢弃: {paths}"


def test_duplicate_source_class_still_deduped():
    """P5 原保护不回退：两模块重复建同名类仍去重保留首个。"""
    fp = [
        _fp("channel/INotifyService.java", module="channel"),
        _fp("engine/INotifyService.java", module="engine"),
    ]
    out = dedupe_file_plan(fp)
    assert len(out) == 1
    assert out[0]["path"] == "channel/INotifyService.java"


def test_exact_same_manifest_path_still_deduped():
    fp = [_fp("m/pom.xml"), _fp("m/pom.xml")]
    assert len(dedupe_file_plan(fp)) == 1


def test_order_groups_ambiguous_basename_no_phantom_edge():
    """hunter MEDIUM：多份同名 pom 共存后，裸 basename 依赖("pom.xml")在 _order_groups
    的 last-writer-wins 兜底映射里会错连到最后登记的组 → 伪边与真实边成环 → 拓扑序被
    毁、降级分层序（消费者可能排到生产者前）。歧义 basename 不得参与依赖解析。"""
    from swarm.brain.plan_batch import _order_groups

    groups = {
        # zbase 的裸 "pom.xml" 依赖本意=自己模块的 pom；旧代码解析到 aconsumer（后写）
        # → 伪边 zbase→aconsumer 与真实边 aconsumer→zbase 成环 → 回退字典序 aconsumer 在前
        "zbase": [{"path": "zbase/pom.xml"},
                  {"path": "zbase/src/A.java", "depends_on": ["pom.xml"]}],
        "aconsumer": [{"path": "aconsumer/pom.xml"},
                      {"path": "aconsumer/src/B.java", "depends_on": ["zbase/src/A.java"]}],
    }
    out = _order_groups(groups)
    assert out.index("zbase") < out.index("aconsumer"), \
        f"生产者必须排消费者前（伪边成环会毁掉真实拓扑序）: {out}"


def test_order_groups_unambiguous_basename_still_resolves():
    """回归护栏：全计划唯一的 basename 依赖仍可解析（兜底能力不回退）。"""
    from swarm.brain.plan_batch import _order_groups

    groups = {
        "zcore": [{"path": "zcore/src/Common.java"}],
        "aapp": [{"path": "aapp/src/Main.java", "depends_on": ["Common.java"]}],
    }
    out = _order_groups(groups)
    assert out.index("zcore") < out.index("aapp"), out
