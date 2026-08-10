"""G-H11 收窄版：③d 禁令矛盾闸 + finisher 自愈的证据抽取多栈换源。

治前 `_exam_dependency_contradictions` / `reconcile_dep_ban_prose` 的证据面只有 Maven
形两臂（desc 整文 `_TEMPLATE_DEP_RE` / AC 的 `_MAVEN_COORD_RE` g:a:v 裸坐标）：
  - npm/go/python/cargo/gradle 权威模板（_extract_auth_templates 认得的围栏块）注入的
    依赖对禁令不可见 ⇒ 非 Maven 栈「禁令 vs 注入依赖」考卷自相矛盾 fail-open 洞；
  - 规则5 机器行（全栈同形 `<manifest> 必须声明依赖: ['x']（…）`，考卷自己强制声明的
    依赖权威面）连 Maven 裸 artifactId 形态都不可见 ⇒ st-8-1 死型的规则5 变体漏网。
治法（两闸同源=`_injected_dep_evidence` 单一事实源）：原两臂逐字节保留（Maven 行为
不变硬约束：pom 模板刻意不走 driver 防双份/口径漂移）+ desc 非 pom 模板走
`_EXAM_DRIVERS[basename].extract` + AC 规则5 行回读；表外落点/extract 返 None →
WARNING + fail-honest 跳过（绝不拿空清单当「无注入」）。
误豁面（评）：非 Maven 证据无 group，内部豁免退化裸名判据——撞名误豁由既有全豁免
WARNING（复核 A4）观测，反方向（go 内部 path 不豁免）落 hits 走自愈相对化不误 REJECT。
"""
from __future__ import annotations

import logging

from swarm.brain.contract_utils import _EXAM_DRIVERS
from swarm.brain.plan_finisher import reconcile_dep_ban_prose
from swarm.brain.plan_validator import _exam_dependency_contradictions
from swarm.stacks.spec import STACK_SPEC
from swarm.types import FileScope, SubTask, TaskPlan


def _st(sid, *, desc="x", writable=None, create=None, ac=None):
    return SubTask(id=sid, description=desc,
                   scope=FileScope(writable=writable or [], create_files=create or []),
                   depends_on=[], acceptance_criteria=ac or [])


def _plan(*sts):
    return TaskPlan(subtasks=list(sts))


def _tpl(label, path, body, lang=""):
    """权威模板围栏块（_extract_auth_templates 认得的唯一形态）。"""
    return f"【权威 {label} 模板（原样写入 {path}）】\n```{lang}\n{body}\n```"


_NPM_TPL = _tpl("package.json", "package.json",
                '{"name": "shop-web", "dependencies": {"express": "^4.18.2"}}', "json")
_GO_TPL = _tpl("go.mod", "go.mod",
               "module example.com/shop\n\ngo 1.22\n\nrequire (\n"
               "\tgithub.com/pkg/errors v0.9.1\n)\n")


# ── ① fail-open 洞锁：非 Maven 权威模板注入对禁令可见 ──

def test_npm_template_injection_visible_to_ban():
    """治前：package.json 模板注入 express，『任何第三方』禁令下 ③d 证据为空 → 静默放行
    （worker 拿一张必死卷）。治后：矛盾必须报出且点名 express。"""
    st = _st("st-n1", desc="实现订单 API。不得引入任何第三方运行时依赖。\n" + _NPM_TPL,
             writable=["web/src/index.js"])
    out = _exam_dependency_contradictions(_plan(st))
    assert out and out[0][0] == "st-n1", f"npm 模板注入对禁令不可见（fail-open 洞）: {out}"
    assert any("express" in c for c in out[0][2]), f"必须点名冲突依赖: {out[0][2]}"


def test_go_template_external_require_flagged():
    st = _st("st-g1", desc="实现订单服务。零第三方依赖。\n" + _GO_TPL,
             create=["cmd/server/main.go"])
    out = _exam_dependency_contradictions(_plan(st))
    assert out and any("github.com/pkg/errors" in c for c in out[0][2]), \
        f"go.mod require 外部模块对禁令不可见: {out}"


# ── ② AC 规则5 机器行回读（全栈，含 Maven 裸 artifactId 形态——治前也不可见）──

def test_rule5_line_npm_specific_ban_hit():
    """无模板、仅 AC 规则5 行强制声明 express + 具名禁令 → 矛盾（st-8-1 死型的规则5 变体）。"""
    st = _st("st-r1", desc="实现订单 API。禁止使用 express。",
             writable=["web/src/index.js"],
             ac=["package.json 必须声明依赖: ['express']（缺一即整模块编译失败）"])
    out = _exam_dependency_contradictions(_plan(st))
    assert out and any("express" in c for c in out[0][2]), \
        f"AC 规则5 机器行（npm）回读缺失: {out}"


def test_rule5_line_maven_bare_artifact_now_visible():
    """Maven 规则5 行是裸 artifactId 形态（不匹配 g:a:v 正则）——治前 AC 侧同样看不见。"""
    st = _st("st-r2", desc="实现工具类。禁止使用 lombok。",
             writable=["ruoyi-common/src/main/java/com/ruoyi/common/utils/A.java"],
             ac=["ruoyi-common/pom.xml 必须声明依赖: ['lombok']（缺一即整模块 mvn compile 失败）"])
    out = _exam_dependency_contradictions(_plan(st))
    assert out and any("lombok" in c for c in out[0][2]), \
        f"Maven 规则5 机器行（裸 artifactId）回读缺失: {out}"


# ── ③ Maven 行为逐字节保留锁 ──

def test_maven_prose_xml_arm_byte_preserved():
    """Maven 臂=整 desc 正则（非只认围栏块）：散文里的 <dependency> 仍被抓（逐字节保留）。"""
    desc = ("实现 X。不得引入任何第三方运行时依赖。\n"
            "可参考形态：<dependency><groupId>org.springframework</groupId>"
            "<artifactId>spring-core</artifactId></dependency> 但不得真引入。")
    st = _st("st-m1", desc=desc,
             writable=["ruoyi-common/src/main/java/com/ruoyi/common/A.java"])
    out = _exam_dependency_contradictions(_plan(st))
    assert out and any("spring-core" in c for c in out[0][2]), \
        f"Maven 臂整 desc 扫描行为漂移: {out}"


def test_maven_internal_bare_artifact_exemption_unchanged():
    """round67m st-1 型：third_party 禁令下裸 artifactId 撞模块物理根=内部接线豁免
    （行为锁，R67M-T1 判据不漂移）；真第三方 spring 坐标仍报。"""
    tpl = _tpl("pom", "ruoyi-common/pom.xml",
               "<project><dependencies>\n"
               "<dependency><groupId>com.ruoyi</groupId>"
               "<artifactId>ruoyi-common</artifactId></dependency>\n"
               "<dependency><groupId>org.springframework.boot</groupId>"
               "<artifactId>spring-boot-starter-web</artifactId></dependency>\n"
               "</dependencies></project>", "xml")
    st = _st("st-m2", desc="实现 X。任何第三方依赖不得引入。\n" + tpl,
             writable=["ruoyi-common/src/main/java/com/ruoyi/common/A.java",
                       "ruoyi-system/src/main/java/com/ruoyi/system/B.java"])
    out = _exam_dependency_contradictions(_plan(st))
    assert out, "真第三方坐标必须仍判矛盾"
    hits = out[0][2]
    assert any("spring-boot-starter-web" in c for c in hits)
    assert not any("ruoyi-common" == c or c.endswith(":ruoyi-common") for c in hits), \
        f"内部 reactor 裸坐标豁免行为漂移: {hits}"


# ── ④ fail-honest 两臂：认不得绝不拿空清单当「无注入」──

def test_extract_failure_warns_not_silent(caplog):
    """非法 JSON 的 package.json 模板 → extract None → WARNING + 跳过该模板证据
    （绝不解析失败=零依赖放行，那是比不对账更坏的假同源）。"""
    bad = _tpl("package.json", "package.json", "{not-json", "json")
    st = _st("st-b1", desc="实现 X。不得引入任何第三方运行时依赖。\n" + bad,
             writable=["web/src/index.js"])
    with caplog.at_level(logging.WARNING):
        out = _exam_dependency_contradictions(_plan(st))
    assert out == [], "抽取失败臂无证据→无矛盾（fail-honest 跳过，非假绿背书）"
    assert any("依赖抽取失败" in r.getMessage() for r in caplog.records), \
        "解析失败必须机读 WARNING 可辨（空返回与真没有不可分=静默失效温床）"


def test_unknown_manifest_warns_not_silent(caplog):
    """_EXAM_DRIVERS 表外落点（stack.toml）→ WARNING 机读可辨，绝不静默跳过。"""
    weird = _tpl("stack", "stack.toml", "[dependencies]\nfoo = \"1.0\"\n")
    st = _st("st-b2", desc="实现 X。不得引入任何第三方运行时依赖。\n" + weird,
             writable=["src/main.rs"])
    with caplog.at_level(logging.WARNING):
        _exam_dependency_contradictions(_plan(st))
    assert any("无证据抽取 driver" in r.getMessage() for r in caplog.records), \
        "表外落点必须机读 WARNING（免疫『兜底与主判据枚举缺口重合』族）"


# ── ⑤ 误豁面登记锁：裸名判据 + 全豁免 WARNING 观测点 ──

def test_npm_bare_name_internal_exemption_observed(caplog):
    """npm workspace 包名撞模块物理根=内部接线豁免（裸名判据，登记的误豁面）——
    豁免必须带全豁免 WARNING（复核 A4 观测点：撞名误豁时这是唯一信号）。"""
    tpl = _tpl("package.json", "package.json",
               '{"name": "common", "dependencies": {"common": "1.0.0"}}', "json")
    st = _st("st-x1", desc="实现 X。零第三方依赖。\n" + tpl,
             writable=["common/index.js"])
    with caplog.at_level(logging.WARNING):
        out = _exam_dependency_contradictions(_plan(st))
    assert out == [], f"裸名撞模块根=内部接线豁免（登记行为）: {out}"
    assert any("豁免" in r.getMessage() for r in caplog.records), \
        "全豁免必须 WARNING 可观测（误豁面的唯一信号）"


# ── ⑥ 自愈多栈：非 Maven 真矛盾同样相对化消弭 ──

def test_self_heal_npm_ban_relativized():
    """npm 真矛盾（express 第三方 + 硬禁令）→ 自愈把禁令相对化（保否定），③d 转净。"""
    st = _st("st-h1", desc="实现订单 API。不得引入任何第三方运行时依赖。\n" + _NPM_TPL,
             writable=["web/src/index.js"])
    plan = _plan(st)
    assert _exam_dependency_contradictions(plan), "前置：自愈前 ③d 必须判真矛盾"
    out = reconcile_dep_ban_prose(plan)
    assert "st-h1" in out, f"npm 真矛盾必须自愈（治前证据为空=自愈面同样盲）: {out}"
    assert "不得引入" in st.description, "自愈必须保否定（复核 A1 纪律跨栈成立）"
    assert _exam_dependency_contradictions(plan) == [], "自愈后 ③d 必须净（相对表述豁免）"


# ── ⑦ _EXAM_DRIVERS 键域 ⊇ STACK_SPEC 清单（加栈从 spec 开始，driver 缺口=闸静默盲）──

def test_exam_drivers_cover_stack_spec_manifests():
    """STACK_SPEC 每栈的 module_manifest（含别名）必须有 _EXAM_DRIVERS 条目——否则该栈
    权威模板对③d/自愈/考卷同源三闸全盲，只能靠 WARNING 日志被人看见（纪律：加栈从这里
    开始；本测试把「认不得」从日志债升级为收集期红）。"""
    missing = []
    for stack_key, spec in STACK_SPEC.items():
        for mf in (spec.module_manifest, *getattr(spec, "module_extra_manifests", ())):
            if mf.lower() not in _EXAM_DRIVERS:
                missing.append(f"{stack_key}:{mf}")
    assert not missing, f"STACK_SPEC 清单无考卷证据 driver: {missing}"
