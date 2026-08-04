"""R65TR-T5：基线工程约定漂移——注解处理器（Lombok 形态）基线在位性钉死。

治后回放 C 路实证：RuoYi 基线零 Lombok，交付 12 文件引入 @Data；JDK17 编译绿但
JDK≥23 默认关闭隐式注解处理→112 处找不到符号必挂=环境条件性断裂；且 3 个 @Data
类无模块内调用者=Lombok 失效时静默编译通过的跨模块哑弹。漂移源=模型训练先验
（经验层排查无源头）。

治法=jakarta/javax 命名空间先例同型（_detect_jvm_facts：磁盘 ground truth 钉死
硬前提）：基线构建清单/源码双证探测 Lombok 在位性 → format_stack_for_prompt 渲染
硬约束（不在位=禁 Lombok 注解必须手写访问器；在位=可用）。JVM 专属事实放 per-stack
facts 是既有架构（非 JVM 返回 None 不污染别栈画像）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from swarm.brain.stack_detect import _detect_jvm_facts, format_stack_for_prompt


def _mk_maven(tmp_path: Path, pom_extra: str = "", src: dict | None = None) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion>"
        "<groupId>g</groupId><artifactId>a</artifactId><version>1</version>"
        "<properties><java.version>8</java.version></properties>"
        f"{pom_extra}</project>")
    for rel, text in (src or {}).items():
        p = proj / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return proj


def _jvm(proj: Path):
    java = [str(p) for p in proj.rglob("*.java")]
    return _detect_jvm_facts(str(proj), {"pom.xml": (proj / "pom.xml").read_text().lower()}, java)


def test_detect_lombok_absent_in_baseline(tmp_path):
    proj = _mk_maven(tmp_path, src={
        "src/main/java/com/x/User.java":
            "package com.x;\npublic class User { private String name; "
            "public String getName(){return name;} }\n"})
    facts = _jvm(proj)
    assert facts is not None
    assert facts.get("lombok_available") is False, facts


def test_detect_lombok_present_via_pom(tmp_path):
    proj = _mk_maven(
        tmp_path,
        pom_extra="<dependencies><dependency><groupId>org.projectlombok</groupId>"
                  "<artifactId>lombok</artifactId></dependency></dependencies>")
    facts = _jvm(proj)
    assert facts is not None
    assert facts.get("lombok_available") is True, facts


def test_detect_lombok_present_via_source(tmp_path):
    proj = _mk_maven(tmp_path, src={
        "src/main/java/com/x/Dto.java":
            "package com.x;\nimport lombok.Data;\n@Data\npublic class Dto {}\n"})
    facts = _jvm(proj)
    assert facts is not None
    assert facts.get("lombok_available") is True, facts


def test_prompt_renders_lombok_ban_when_absent():
    p = format_stack_for_prompt({
        "backend": "java", "build": "maven", "frontend": "无", "frontend_kind": "",
        "confidence": 0.9,
        "jvm": {"servlet_namespace": "javax", "namespace_source": "t",
                "spring_boot_version": "", "java_version": "8",
                "lombok_available": False},
    })
    assert "Lombok" in p and ("严禁" in p or "禁止" in p), p
    assert "@Data" in p, "硬约束必须点名典型注解"
    assert "手写" in p, "必须给出正向替代（手写访问器）"


def test_prompt_allows_lombok_when_present():
    p = format_stack_for_prompt({
        "backend": "java", "build": "maven", "frontend": "无", "frontend_kind": "",
        "confidence": 0.9,
        "jvm": {"servlet_namespace": "jakarta", "namespace_source": "t",
                "spring_boot_version": "3.2.0", "java_version": "17",
                "lombok_available": True},
    })
    assert "禁止 @Data" not in p and "严禁 Lombok" not in p, p


def test_prompt_silent_when_fact_unknown():
    """老画像/回放 profile 无该键 → 不渲染任何 Lombok 行（不猜）。"""
    p = format_stack_for_prompt({
        "backend": "java", "build": "maven", "frontend": "无", "frontend_kind": "",
        "confidence": 0.9,
        "jvm": {"servlet_namespace": "javax", "namespace_source": "t",
                "spring_boot_version": "", "java_version": "8"},
    })
    assert "Lombok" not in p, p


# ── 猎手整改锁 ────────────────────────────────────────────────────────


def test_exclusion_block_not_false_positive(tmp_path):
    """猎手 F2：蓄意传递排除块（挡三方 starter 引入 lombok）绝不算"在位"——
    误放行=探测器自己复现要防的哑弹。"""
    proj = _mk_maven(
        tmp_path,
        pom_extra="<dependencies><dependency><groupId>com.some</groupId>"
                  "<artifactId>starter</artifactId><exclusions><exclusion>"
                  "<!-- 避免传递引入 lombok，本项目未启用 -->"
                  "<groupId>org.projectlombok</groupId><artifactId>lombok</artifactId>"
                  "</exclusion></exclusions></dependency></dependencies>")
    facts = _jvm(proj)
    assert facts is not None
    assert facts.get("lombok_available") is False, facts


def test_submodule_pom_declaration_detected(tmp_path):
    """猎手 F1：依赖只声明在非根子模块 pom（常见：common/domain 模块）——
    manifest_texts 按 basename 累积后必须探到，不得被 last-write-wins 吞。"""
    from swarm.brain.stack_detect import detect_stack_deterministic

    proj = _mk_maven(tmp_path)
    for mod, extra in (("mod-a", ""), ("mod-b",
            "<dependencies><dependency><groupId>org.projectlombok</groupId>"
            "<artifactId>lombok</artifactId></dependency></dependencies>"),
            ("mod-c", "")):
        d = proj / mod
        d.mkdir()
        (d / "pom.xml").write_text(
            f"<project><artifactId>{mod}</artifactId>{extra}</project>")
    prof = detect_stack_deterministic(str(proj))
    jvm = prof.get("jvm") or {}
    assert jvm.get("lombok_available") is True, jvm


def _stack_cache_payload_digest() -> str:
    """被缓存内容的摘要：① 画像字段集（含 signals 嵌套键）② 决定答案的事实表。

    只盖"决定被缓存内容的东西"，**不盖整个模块源码**——否则改个注释都要 bump，
    使用者迟早绕开这道闸（本项目对"过宽的闸"有明确判据：使用者会绕开）。
    字段集从**真跑一次探测**取（测默认行为，不用 getsource 扫源码，纪律 6）。
    """
    import hashlib
    import os
    import tempfile

    from swarm.brain import stack_detect as sd

    parts: list[str] = []
    # ① 画像字段集。★两个夹具，不是一个（复核 A-4）★ 单个 Django 夹具下 `prof["jvm"]` 恒为
    # 空 dict ⇒ 代码即便新增 `jvm.<key>`，这个夹具也从不产生它 ⇒ 递归取键集也白搭。
    # 必须让每条**会被缓存的子结构**都真有内容：Django 覆盖 signals/frontend 侧，
    # Maven（含 lombok 坐标）覆盖 jvm 侧（v3 bump 的原因就住在那里）。
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "requirements.txt"), "w") as fh:
            fh.write("Django==5.0.6\n")
        prof_py = sd.detect_stack_deterministic(d)
    with tempfile.TemporaryDirectory() as d:
        # 形状与本文件 `_mk_maven` + `test_submodule_pom_declaration_detected` 同源
        # （那是已跑通的真夹具：根 pom 需 modelVersion/groupId/version/properties，
        #  lombok 坐标声明在**子模块** pom —— 自己编一个精简 pom 探不出 jvm）。
        with open(os.path.join(d, "pom.xml"), "w") as fh:
            fh.write("<project><modelVersion>4.0.0</modelVersion>"
                     "<groupId>g</groupId><artifactId>a</artifactId><version>1</version>"
                     "<properties><java.version>8</java.version></properties></project>")
        sub = os.path.join(d, "mod-b")
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, "pom.xml"), "w") as fh:
            fh.write("<project><artifactId>mod-b</artifactId><dependencies>"
                     "<dependency><groupId>org.projectlombok</groupId>"
                     "<artifactId>lombok</artifactId></dependency>"
                     "</dependencies></project>")
        prof_jvm = sd.detect_stack_deterministic(d)
    for nm, prof in (("py", prof_py), ("jvm", prof_jvm)):
        assert not prof.get("scan_failed"), f"{nm} 夹具走进扫描失败兜底，键集不完整"
    assert prof_jvm.get("jvm"), "Maven 夹具没产生 jvm 子字典 ⇒ A-4 的缺口又回来了"

    # ★递归取键集，不只取顶层 + signals（复核 A-4）★
    # 只取 `sorted(prof)` + `sorted(prof["signals"])` 会漏掉**嵌套子字典**的键集，而
    # v3 那次 bump 的原因（`lombok_available`）恰好住在 `prof["jvm"]` 里。实测：往 jvm 加键
    # 摘要不变 ⇒ 守卫不红 ⇒ 猎手 F3 的原型事故可原样复发。这道守卫当时只防住了 v5 那类
    # （顶层键 + signals + 事实表），没防住 v3 那类。
    def _key_paths(node, prefix: str = "") -> list[str]:
        if not isinstance(node, dict):
            return []
        out: list[str] = []
        for k in sorted(node):
            path = f"{prefix}{k}"
            out.append(path)
            out.extend(_key_paths(node[k], f"{path}."))
        return out

    parts.append("profile_key_paths_py=" + ",".join(_key_paths(prof_py)))
    parts.append("profile_key_paths_jvm=" + ",".join(_key_paths(prof_jvm)))
    # ② 决定答案的事实表（扩表会改变**已缓存项目的正确答案**，与新增字段同等必须 bump）
    for name in ("_TEMPLATE_EXT_ENGINE", "_TEMPLATE_COMPOUND_SUFFIX", "_SERVER_TEMPLATE_DEP",
                 "_WEBPAGE_EXTS", "_TEMPLATE_DIR_NAMES", "_MANIFEST_BACKEND"):
        t = getattr(sd, name)
        body = (";".join(f"{k}={t[k]!r}" for k in sorted(t)) if isinstance(t, dict)
                else ";".join(sorted(t)))
        parts.append(f"{name}={body}")
    # 值是编译后 Pattern，repr 含内存地址不稳定 ⇒ 取 .pattern
    rt = sd._SERVER_TEMPLATE_DEP_RE
    parts.append("_SERVER_TEMPLATE_DEP_RE="
                 + ";".join(f"{k}={rt[k].pattern}" for k in sorted(rt)))

    # ③ ★判定逻辑的输出值，不只是事实表（复核 A-5）★
    # 只盖表会漏掉**逻辑侧**改动：P-C3 复核的 CRITICAL-1 正是"全仓 `.html` 计数把 DRF 纯 API
    # 的正确答案翻成 conf=0.95 错答案"——改的是计数/阈值，一张表都没动 ⇒ 摘要不变 ⇒ 不 bump
    # ⇒ 已缓存项目继续吃旧画像。这里把三个代表形态的**结论**纳入摘要：结论变了就必须 bump，
    # 因为"已缓存项目的正确答案变了"正是 bump 的定义。
    # 仍刻意不盖整个模块源码（改注释不该触发 bump —— 过宽的闸使用者会绕开）。
    for nm, files in (
        ("drf_api_only", {"requirements.txt": "Django==5.0.6\ndjangorestframework==3.15.2\n",
                          "docs/index.html": "<html></html>"}),
        ("django_templates", {"requirements.txt": "Django==5.0.6\n",
                              "templates/base.html": "{% block content %}{% endblock %}"}),
        ("spa", {"package.json": '{"dependencies":{"vue":"^3.4.0"}}',
                 "src/App.vue": "<template></template>"}),
        # #21/MEDIUM-5：Helm chart 的 `templates/_helpers.tpl` 不得触发「认不得」——
        # 判定逻辑改动（Chart.yaml 排除）只有这个夹具捕得到，前三个对它零区分力。
        ("helm_chart", {"go.mod": "module example.com/api\n\ngo 1.21\n",
                        "main.go": "package main\nfunc main(){}\n",
                        "deploy/chart/Chart.yaml": "apiVersion: v2\nname: api\n",
                        "deploy/chart/templates/_helpers.tpl": "{{- end -}}\n"}),
        # P-C3 复核 R2-H1：Chart.yaml 在**仓根**（chart 仓标准布局）——与前一个夹具唯一
        # 差异是 chart 位置；排除判据对根目录的处理只有这个夹具捕得到（治前恒假⇒unrec=True）。
        ("helm_chart_root", {"go.mod": "module example.com/api\n\ngo 1.21\n",
                             "main.go": "package main\nfunc main(){}\n",
                             "Chart.yaml": "apiVersion: v2\nname: api\n",
                             "templates/_helpers.tpl": "{{- end -}}\n"}),
        # P-M1（27 号文）：纯 npm API（无前端）的正确答案从 adj=True（零清单证据 -0.3 罚）
        # 翻回不裁决——改的是置信罚判据的消费面（manifests 含不含 package.json），
        # 表侧/_LANG_SOURCE_EXTS 都盖不到这一格（v5~v9 同形状，逻辑侧改动）。
        ("npm_api_only", {"package.json": '{"name":"api","dependencies":{"express":"^4"}}',
                          "src/mod0.ts": "export {}", "src/mod1.ts": "export {}"}),
    ):
        with tempfile.TemporaryDirectory() as d:
            for rel, body in files.items():
                p = os.path.join(d, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as fh:
                    fh.write(body)
            pr = sd.detect_stack_deterministic(d)
        parts.append(
            f"verdict[{nm}]=kind:{pr.get('frontend_kind')}|conf:{pr.get('confidence')}"
            f"|adj:{pr.get('needs_model_adjudication')}"
            f"|unrec:{(pr.get('signals') or {}).get('tmpl_engine_unrecognized')}")

    # ④ ★值层覆盖（P-H1 复核 hunter H-1）★ key_paths 只锁「有哪些键」，锁不住「键里值的
    # 语义」——把 npm_facts/go_facts/python_facts 的取值重命名/改口径而不加键时 key_paths
    # 与 verdict 都不变 ⇒ 摘要不变 ⇒ 忘 bump ⇒ 已缓存项目继续吃旧值（P-C3 CRITICAL-2 同型）。
    # 三个代表夹具把三键的【完整值】纳入摘要：值变了摘要必变。
    for nm, files in (
        ("npm_esm", {"package.json": '{"name":"web","type":"module","engines":{"node":">=18"}}'}),
        ("go_mod", {"go.mod": "module github.com/BurntSushi/toml\n\ngo 1.21\n"}),
        ("py_src", {"pyproject.toml": '[project]\nrequires-python = ">=3.10"\n',
                    "src/mypkg/__init__.py": ""}),
    ):
        with tempfile.TemporaryDirectory() as d:
            for rel, body in files.items():
                p = os.path.join(d, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w") as fh:
                    fh.write(body)
            pr = sd.detect_stack_deterministic(d)
        parts.append(
            f"facts[{nm}]=npm:{pr.get('npm_facts')!r}|go:{pr.get('go_facts')!r}"
            f"|py:{pr.get('python_facts')!r}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def test_stack_schema_version_paired_with_cached_payload():
    """★27 号文 #19 / P-C3 复核 CRITICAL-2★ `_STACK_SCHEMA_VERSION` 是"画像内容变了"的
    **唯一**失效信号：`detect_stack` 的缓存命中判据是 `cached["schema_version"] == 常量`
    且指纹相同即 `return` 缓存画像。

    ★本条原来断的是 `_STACK_SCHEMA_VERSION >= 3`——下界断言是**永绿**的★：值停在 4 时
    `>= 3` 成立、递增到 5/6 也成立 ⇒ 它对自己要防的事（改了画像逻辑却**不**递增）零区分力。
    实证：fee9a2a 扩了模板引擎事实表 198 行、新增机读键 `signals.tmpl_engine_unrecognized`，
    `git show fee9a2a | grep -c _STACK_SCHEMA_VERSION` = **0**，而这条守卫全程绿。
    受害项目 5d0e9db8（RuoYi E2E 基线）缓存 schema_version=4 == 当时常量 4 ⇒ 命中缓存早返
    ⇒ P-C3 对它一行不执行；且缺键被消费者的 `.get()` 读成 None＝假值 ⇒ **静默 no-op 不报错**。

    正确形状＝把**被缓存内容的摘要**与版本常量钉成一对（同型守卫在 `_BUILDER_VERSION` 上
    已跑通：test/test_image_builder.py 的 `test_builder_version_bumped_so_old_images_are_invalidated`，
    4/4 突变全红）。生成物变了摘要必变，两者必须同时改。
    """
    from swarm.brain.planning_nodes import _STACK_SCHEMA_VERSION

    digest = _stack_cache_payload_digest()
    # v5→v6：#18 判据计数范围收敛（`verdict[drf_api_only]` 从 server-template/0.95 翻回 none）
    # + #20 四栈扩表（_TEMPLATE_EXT_ENGINE/_SERVER_TEMPLATE_DEP/_TEMPLATE_DIR_NAMES 三张表）。
    # v6→v7：#21 LOW-6 删零消费者键（signals.unengined_template_dir_files ⇒ key_paths 变）
    # + MEDIUM-5 Helm chart 排除（新增 verdict[helm_chart] 夹具位）。
    # v7→v8：P-C3 复核 R2-H1 根级 Helm chart 排除修复（新增 verdict[helm_chart_root] 夹具位，
    # 其结论从 unrec:True 翻回 False）+ R2-H3 半截采纳收口（节点裁决行为，摘要盖不到，
    # 但同属「已缓存项目正确答案变了」⇒ 必须同批 bump）。常量本体同批迁至 stack_detect.py
    # （R2-H4：worker 第二读取路径同源消费），planning_nodes re-export 保可寻址。
    # v8→v9：27 号文 P-H1 非 JVM 接地事实三键（npm_facts/go_facts/python_facts）进画像
    # ⇒ py/jvm 两夹具的 profile_key_paths 都变（键恒在场，空 dict 也占 key_path）。
    # hunter H-1 同批：摘要补④值层夹具——key_paths 锁不住「键里值的语义」，三键完整值
    # 纳入摘要（值变了摘要必变，忘 bump 会被本条拦住）。
    # v9→v10：27 号文 P-M1——package.json 进 _MANIFEST_BACKEND（表变摘要即变）+
    # 新增 verdict[npm_api_only] 夹具位（纯 npm API 正确答案从 adj=True 翻回不裁决，
    # 置信罚判据的消费面=逻辑侧改动，表侧锁不到）。
    # 摘要跨 hash 种子稳定（实测 PYTHONHASHSEED=0/1/12345/random 四轮同值）。
    assert (_STACK_SCHEMA_VERSION, digest) == (10, "719533d24a2bc6ec"), (
        f"栈画像的字段集或事实表变了（当前摘要 {digest}，版本 {_STACK_SCHEMA_VERSION}）。\n"
        "这不是让你改数字对付过去：**必须递增 `_STACK_SCHEMA_VERSION` 并同步更新本条的摘要**。\n"
        "只改摘要不递增版本 ⇒ 已缓存项目的 schema_version 仍等于常量 ⇒ detect_stack 命中缓存\n"
        "早返 ⇒ 你的改动对**所有已建档项目**一行都不生效（P-C3 复核 CRITICAL-2 的原始事故），\n"
        "而且是静默的：消费者用 `.get()` 读缺失的新键，得到 None 当假值，不会 KeyError。")


def test_non_jvm_unpolluted(tmp_path):
    proj = tmp_path / "npm"
    proj.mkdir()
    (proj / "package.json").write_text("{}")
    facts = _detect_jvm_facts(str(proj), {"package.json": "{}"}, [])
    assert facts is None


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
