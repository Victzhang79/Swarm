"""round65e4 死因治本（#67 物理层收口）——G1 模块 coherence：`src/**/resources/**` 资源树
不主张模块物理构建根（打包不编译，永不定义/扩张 build 单元）。

死因（三路交叉 + HEAD 实跑坐实）：RuoYi 单体 feature `ruoyi-alarm` 的业务 Java 落 `ruoyi-alarm/`，
其 Thymeleaf 视图 + 静态资源按框架**必须**落 `ruoyi-admin` webapp。`.html`/`.css`/mapper `.xml`/
`.yml` 早已在 `_AUX_EXTENSIONS`（不主张根）；唯独 **`static/**/*.js` 未被覆盖** → `_evidence_class`
撞 `resources`/`src`/`main` 布局段升格 `_EV_STRONG` → 主张第二物理根 `ruoyi-admin` → G1 硬打回一个
**本可 build** 的 plan（`ruoyi-admin` 既存无需脚手架、`ruoyi-alarm/pom.xml` 由子任务自认领）。
每个带 UI 的 RuoYi feature 必撞此闸 = "越治本越过不了 planning" 的物理层根。

治本（非扩名单打地鼠）：按真不变量一次收口——**Maven/Gradle `src/**/resources/**` 子树打包不编译、
永不定义构建单元**，其下任意扩展名（含 `.js`/`.png`/未来类型）皆不主张物理根。路径限定（须同时含
`src` 且 `resources` 且 resources 在 src 之后），故真 JS 工程源码 `web/src/App.js`（无 resources 段）
不受影响、仍主张根——不回归。
"""
from types import SimpleNamespace

from brain.contract_utils import _resolve_module_dirs
from brain.plan_validator import validate_module_coherence


def _plan():
    """最小 plan：无 subtasks / 无 shared_contract —— 只驱动 file_plan 通道的物理根判定。"""
    return SimpleNamespace(subtasks=[], shared_contract={})


def _fp(*pairs):
    return [{"module": m, "path": p} for m, p in pairs]


# ── 治本目标：RuoYi feature 视图/静态资源落 admin 壳，不得判本模块跨物理根 ──────────────
def test_ruoyi_feature_with_admin_resources_is_coherent():
    """module=ruoyi-alarm：Java 落 ruoyi-alarm/，视图(.html)+静态(.js/.css)落 ruoyi-admin 壳，
    DDL 落 sql/ —— 单一 build 单元 ruoyi-alarm，G1 必须放行（当前因 .js RED）。"""
    file_plan = _fp(
        ("ruoyi-alarm", "ruoyi-alarm/src/main/java/com/ruoyi/alarm/domain/Alarm.java"),
        ("ruoyi-alarm", "ruoyi-alarm/src/main/java/com/ruoyi/alarm/service/AlarmService.java"),
        ("ruoyi-alarm", "ruoyi-alarm/src/main/java/com/ruoyi/alarm/controller/AlarmController.java"),
        # 视图 + 静态资源：物理必落 admin webapp（框架约束），非本模块第二 build 单元
        ("ruoyi-alarm", "ruoyi-admin/src/main/resources/templates/alarm/task.html"),
        ("ruoyi-alarm", "ruoyi-admin/src/main/resources/static/alarm/js/task.js"),
        ("ruoyi-alarm", "ruoyi-admin/src/main/resources/static/alarm/css/task.css"),
        # DDL：辅助交付物
        ("ruoyi-alarm", "sql/alarm.sql"),
    )
    _resolved, ambiguous, _collision = _resolve_module_dirs(_plan(), None, file_plan)
    assert "ruoyi-alarm" not in ambiguous, (
        f"资源树(.js 含)不得让 feature 判跨物理根；实得 ambiguous={ambiguous}")


def test_pure_view_subtask_resolves_single_root_no_empty():
    """纯视图子任务（全落 admin 资源树，无 Java）→ 单根 ruoyi-admin，绝不空根/歧义。"""
    file_plan = _fp(
        ("ruoyi-view", "ruoyi-admin/src/main/resources/templates/alarm/list.html"),
        ("ruoyi-view", "ruoyi-admin/src/main/resources/static/alarm/js/list.js"),
    )
    resolved, ambiguous, _collision = _resolve_module_dirs(_plan(), None, file_plan)
    assert "ruoyi-view" not in ambiguous, f"纯资源模块不得歧义；ambiguous={ambiguous}"
    assert resolved.get("ruoyi-view") == "ruoyi-admin", (
        f"纯资源模块应回退资源顶层目录；resolved={resolved}")


# ── 回归护栏：真源码 / 真违①绝不因本次放宽被静默放行 ─────────────────────────────
def test_genuine_flat_js_project_still_asserts_root():
    """真 JS 工程源码 web/src/App.js（无 resources 段）仍主张根——两个真 JS 落点仍判违①歧义。
    证明放宽是【路径限定 resources 树】而非【扩展名 .js 全局豁免】（不回归 JS 工程）。"""
    file_plan = _fp(
        ("frontend", "web-a/src/App.js"),
        ("frontend", "web-a/src/util.js"),
        ("frontend", "web-b/src/App.js"),
    )
    _resolved, ambiguous, _collision = _resolve_module_dirs(_plan(), None, file_plan)
    assert "frontend" in ambiguous, (
        f".js 于 resources 树外仍是模块定义源码，真双根须打回；ambiguous={ambiguous}")


def test_resources_named_package_is_not_demoted():
    """★复核① CONFIRMED HIGH 回归锁★ 名为 `resources` 的【包】里的真编译源码
    `mod-b/src/main/java/com/x/resources/B.java` 绝不被误降 aux——它是真源码、须主张根，
    与 mod-a 的 Java 构成真跨模块违①仍须打回。锚定必须是【源集根 src/<set>/resources】而非
    "src 之后任意出现 resources"。"""
    file_plan = _fp(
        ("mixed", "mod-a/src/main/java/com/x/A.java"),
        ("mixed", "mod-b/src/main/java/com/x/resources/B.java"),
    )
    _resolved, ambiguous, _collision = _resolve_module_dirs(_plan(), None, file_plan)
    assert "mixed" in ambiguous, (
        f"`resources` 包名下的真源码须仍主张根，跨模块违①须打回；ambiguous={ambiguous}")


def test_genuine_java_two_module_split_still_caught():
    """真 Java 跨两模块（各带 src/main/java）仍判违①——放宽只针对资源树，不碰编译源码。"""
    file_plan = _fp(
        ("mixed", "mod-a/src/main/java/com/x/A.java"),
        ("mixed", "mod-b/src/main/java/com/x/B.java"),
    )
    _resolved, ambiguous, _collision = _resolve_module_dirs(_plan(), None, file_plan)
    assert "mixed" in ambiguous, f"真跨模块 Java 双根须打回；ambiguous={ambiguous}"


# ── 复核② CONFIRMED HIGH（silent-hunter）整改：退位不阻断，但必须结构化可观测 ──────────
def test_cross_module_resource_is_soft_warned_not_hard_blocked():
    """资源落构建根之外（含 round65e4 合法案 + 潜在误路由，物理层不可分）→ 不硬打回（valid=True，
    不重现 round65e4 误杀），但 G1 升【软 warn】把跨边界资源移交 #67，绝不只剩 logger.info 湮没。"""
    file_plan = _fp(
        ("ruoyi-alarm", "ruoyi-alarm/src/main/java/com/ruoyi/alarm/service/AlarmService.java"),
        ("ruoyi-alarm", "ruoyi-admin/src/main/resources/static/alarm/js/task.js"),
    )
    plan = _plan()
    plan.subtasks = []
    res = validate_module_coherence(plan, project_path=None, file_plan=file_plan)
    assert res.valid, f"跨边界资源按设计不阻断规划；issues={res.issues}"
    joined = " ".join(res.warnings)
    assert "ruoyi-admin" in joined and "#67" in joined, (
        f"跨边界资源必须结构化 warn 移交 #67（可观测）；warnings={res.warnings}")


def test_cross_res_absent_when_module_stays_within_root():
    """资源与代码同根（无跨边界）→ 不产生 cross_res warn（不误报噪声）。"""
    file_plan = _fp(
        ("ruoyi-alarm", "ruoyi-alarm/src/main/java/com/ruoyi/alarm/Alarm.java"),
        ("ruoyi-alarm", "ruoyi-alarm/src/main/resources/mapper/AlarmMapper.xml"),
    )
    _resolved, _amb, _coll, cross = _resolve_module_dirs(
        _plan(), None, file_plan, with_cross_res=True)
    assert "ruoyi-alarm" not in cross, f"同根资源不应进 cross_res；cross={cross}"
