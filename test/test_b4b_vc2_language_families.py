"""B-4b V-C2：php/ruby/csharp/elixir(/dart) 的崩溃归因（27 号文 §3.3 CRITICAL 假过）。

原病灶：`_LANGUAGE_ALIASES` 无这四个键 → `language_key=None` → `_match_family` 只取
`generic`，而 `_CODE_ERROR_PATTERNS` **刻意无 generic 键**（无表命中绝不默认判代码错）
→ 这些栈**任何启动崩溃都落 inconclusive(skipped)** → `can_auto_accept_delivery` 放行
→ 坏产物直达交付。

★本文件用**真实运行时日志样本**喂 `classify_smoke_outcome` 生产本体★——不构造
"恰好命中正则"的合成串（那种夹具证不了 pattern 与真实输出对得上，是本仓假绿头号形态）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from swarm.brain.nodes.runtime_smoke import (  # noqa: E402
    _CODE_ERROR_PATTERNS,
    _ENV_MISSING_PATTERNS,
    _LANGUAGE_ALIASES,
    _STARTUP_CRASH_PATTERNS,
    classify_smoke_outcome,
    normalize_language_key,
)

# ── 生产者侧真实落盘形态：stack_detect 写 profile["backend"] = f"{fw} ({lang})" ──
BACKEND_DISPLAY = [
    ("Laravel (php)", "php"),
    ("Rails (ruby)", "ruby"),
    ("ASP.NET Core (csharp)", "csharp"),
    ("Phoenix (elixir)", "elixir"),
    ("Flutter (dart)", "dart"),
]


@pytest.mark.parametrize("display,expect", BACKEND_DISPLAY)
def test_producer_display_string_resolves_to_a_language_key(display, expect):
    """★接线的起点★ 这些 token 取自 `stack_detect._LANG_SOURCE_EXTS` 的键，不是猜的拼法。

    突变判据：从 `_LANGUAGE_ALIASES` 删掉任一条，对应参数化用例必红。
    """
    assert normalize_language_key(display) == expect


# ── 真实代码故障日志（各语言运行时原样输出）→ 必须 failed:code_error ──
CODE_ERROR_LOGS = [
    ("php", "PHP Parse error:  syntax error, unexpected token \";\" in "
            "/app/routes/web.php on line 42"),
    ("php", "PHP Fatal error:  Uncaught TypeError: App\\Http\\UserController::show(): "
            "Argument #1 ($id) must be of type int, string given"),
    # ★F-3 整改★ 原样本是 `for nil` ＝ nil-deref（`ENV["X"].split` 在无 env 的沙箱恒如此）
    # ＝**环境形态**，原先判 code_error 是误杀，而这条测试正替它背书。换成真·方法不存在。
    ("ruby", "app/models/user.rb:17:in `full_name': undefined method `upcase' for "
             "an instance of User (NoMethodError)"),
    ("ruby", "/app/config/routes.rb:8: syntax error, unexpected ')', expecting "
             "end-of-input"),
    ("csharp", "/src/Controllers/UserController.cs(23,17): error CS0103: The name "
               "'_repo' does not exist in the current context"),
    ("elixir", "** (CompileError) lib/app_web/router.ex:12: undefined function get/2"),
    ("elixir", "** (FunctionClauseError) no function clause matching in "
               "AppWeb.UserController.show/2"),
    # 同 F-3：`called on null` 是 nil-deref，换成真·无此方法
    ("dart", "Unhandled exception:\nNoSuchMethodError: Class 'Report' has no instance "
             "method 'toUpperCase'."),
]


@pytest.mark.parametrize("lang,log", CODE_ERROR_LOGS)
def test_real_code_fault_logs_are_failed_not_skipped(lang, log):
    """★V-C2 本尊★ 这些栈的确定性代码故障必须判 `failed:code_error`（硬拦交付）。

    原行为：全部 `skipped:inconclusive` → auto_accept 放行。
    突变判据：删掉该语言的 `_CODE_ERROR_PATTERNS` 条目，对应用例必红。
    """
    res = classify_smoke_outcome("1", log, [], language_key=lang)
    assert res.status == "failed", (
        f"{lang} 代码故障被判成 {res.status}/{res.classification}（假过通道）：{log[:60]}")
    assert res.classification == "code_error", res.classification


# ── 真实环境缺失日志 → 必须 skipped（绝不冤枉成代码失败）──
ENV_MISSING_LOGS = [
    ("php", "SQLSTATE[HY000] [2002] Connection refused (Connection: mysql)"),
    ("php", "PDOException: could not find driver"),
    ("ruby", "PG::ConnectionBad: could not connect to server: Connection refused"),
    ("ruby", "Redis::CannotConnectError: Error connecting to Redis on localhost:6379"),
    ("csharp", "System.Net.Sockets.SocketException (111): Connection refused"),
    ("elixir", "** (DBConnection.ConnectionError) tcp connect (localhost:5432): "
               "connection refused - :econnrefused"),
]


@pytest.mark.parametrize("lang,log", ENV_MISSING_LOGS)
def test_real_env_failure_logs_are_skipped_not_code_error(lang, log):
    """反方向同样致命：沙箱没有 DB 不许判成代码失败（fail-honest）。

    这条同时是上一组的**对照臂**——没有它，"一律 failed" 的错实现也能满足上一组。
    """
    res = classify_smoke_outcome("1", log, [], language_key=lang)
    assert res.status == "skipped", (
        f"{lang} 环境缺失被冤判 {res.status}/{res.classification}：{log[:60]}")
    assert res.classification in ("env_missing", "dependency_missing"), res.classification


# ★断言区分力★ 上一组的样本都含字面 `Connection refused`/`connection refused` ——
# 那是 `_ENV_MISSING_PATTERNS["generic"]` 抓的，**通用族替语言族背书**，删掉语言族条目
# 上一组照旧全绿（突变 Q7/Q8 当场证实）。下面这组刻意只留**通用族抓不到**的驱动层形态，
# 让"该语言的环境族是否在场"成为唯一变量。
ENV_MISSING_ISOLATING = [
    ("php", "SQLSTATE[HY000] [1049] Unknown database 'app_prod'"),
    ("ruby", "Mysql2::Error: Unknown database 'app_prod'"),
    ("csharp", "A network-related or instance-specific error occurred while "
               "establishing a connection to SQL Server"),
    ("elixir", "** (DBConnection.ConnectionError) connection not available and "
               "request was dropped from queue after 2950ms"),
]


@pytest.mark.parametrize("lang,log", ENV_MISSING_ISOLATING)
def test_driver_level_env_failures_need_the_language_family(lang, log):
    """通用族抓不到的驱动层连接失败 → 只能靠语言族判 skipped。

    没有这条，语言族条目全删也不会有任何测试变红（＝"改了但没锁"）。
    """
    res = classify_smoke_outcome("1", log, [], language_key=lang)
    assert res.status == "skipped", (
        f"{lang} 驱动层环境失败被冤判 {res.status}/{res.classification}：{log[:60]}")
    assert res.classification == "env_missing", res.classification


@pytest.mark.parametrize("lang,log", [
    ("php", "PHP Fatal error:  Uncaught Error: Class \"App\\Svc\" not found"),
    ("ruby", "rails aborted!\nActiveRecord::NoDatabaseError"),
    ("csharp", "Application startup exception: Autofac.Core.DependencyResolutionException"),
    ("elixir", "** (Mix) Could not start application app: exited in: App.start(:normal, [])"),
])
def test_crashed_but_unattributable_gets_its_own_name(lang, log):
    """崩了但归不到代码 → `startup_crash_unattributed`，不与"什么都没发生"共用 inconclusive。

    （26 号文 C-6 的语义在这四栈上原先完全不存在。）
    """
    res = classify_smoke_outcome("1", log, [], language_key=lang)
    assert res.classification == "startup_crash_unattributed", (
        f"{lang}：{res.status}/{res.classification}")


def test_no_language_key_still_never_defaults_to_code_error():
    """兜底铁律未被本批削弱：无 language_key 时绝不默认判代码错（类3 兜底）。"""
    res = classify_smoke_outcome("1", "PHP Parse error:  syntax error", [],
                                 language_key=None)
    assert res.status == "skipped", res.status


def test_alias_keys_and_pattern_families_do_not_drift_apart():
    """★接线覆盖 ≠ 机制存在★ 新增语言别名却不给 `_CODE_ERROR_PATTERNS` 条目 ＝
    `_match_family` 只回 generic，而该表**无 generic 键** → 该语言恒不产 failed，
    机制看着"支持了"实际静默失效。

    本条把两张表的键集**绑成一个不变量**（V-C2 的根因就是它们漂开了）。
    崩溃族/环境族有 generic 兜底、不在此约束内，但也一并列出缺口便于下轮补齐。
    """
    alias_keys = {v for _, v in _LANGUAGE_ALIASES}
    # ★I-5（reviewer 复核）★ 原来只判**键在不在**，而 `{"x": ()}` 空 tuple 满足包含判定、
    # 却完整复现 V-C2 病灶（该表无 generic 兜底 → 空＝该语言恒不产 failed）。空 tuple 不是
    # 假想形态：`_ENV_MISSING_PATTERNS["node"]`、`_BIND_SUCCESS_PATTERNS` 里已有先例，
    # 下一个人照抄很自然。故判**非空**。
    weak = {k for k in alias_keys if not _CODE_ERROR_PATTERNS.get(k)}
    assert not weak, (
        f"这些语言的 code_error 族缺失或为空 → 恒不产 failed（静默失效）：{sorted(weak)}")
    # 崩溃/环境两族允许只吃 generic，但缺口要可见（当前应为空；新增语言时刻意打红提醒）
    assert not (alias_keys - set(_STARTUP_CRASH_PATTERNS)), sorted(
        alias_keys - set(_STARTUP_CRASH_PATTERNS))
    assert not (alias_keys - set(_ENV_MISSING_PATTERNS)), sorted(
        alias_keys - set(_ENV_MISSING_PATTERNS))


# ══════════════════════════════════════════════
# 自查整改：CS0246/CS0234 被 `error CS\d{4}` 吞掉 → 环境被冤枉成代码
# ══════════════════════════════════════════════

@pytest.mark.parametrize("log", [
    "/src/Api/UserController.cs(4,7): error CS0246: The type or namespace name "
    "'Newtonsoft' could not be found (are you missing a using directive or an "
    "assembly reference?)",
    "/src/Api/UserController.cs(4,7): error CS0234: The type or namespace name "
    "'Json' does not exist in the namespace 'Newtonsoft'",
])
def test_nuget_unrestored_compile_errors_are_not_code_error(log):
    """CS0246/CS0234 ＝ NuGet 未 restore 的**编译期**形态 → 绝不判 code_error。

    本表已按"环境不许伪装代码失败"把 `Could not load file or assembly`（运行期同义形态）
    排除，而原 `\\berror CS\\d{4}\\b` 把编译期形态又收回来了＝自相矛盾。
    突变判据：把否定预查 `(?!0246\\b|0234\\b)` 去掉，本条必红。
    """
    res = classify_smoke_outcome("1", log, [], language_key="csharp")
    assert res.status != "failed", (
        f"NuGet 未还原被冤判 {res.status}/{res.classification}（环境伪装成代码）")
    # ★F-5 整改★ 原来只断 `!= "failed"` ＝零区分力：`inconclusive`（什么都没观测到）与
    # `startup_crash_unattributed`（崩了但归因待定）都满足，于是测试**主动允许**了
    # "连编译都没过"被写成"什么都没发生"——正是本文件 C-6 批判的病灶。
    assert res.classification == "startup_crash_unattributed", res.classification


def test_genuine_cs_compile_error_still_fails():
    """★对照臂★ 真代码错（CS0103 未定义名）照旧 `failed:code_error`。

    没有这条，"把整条 CS 规则删掉"也能满足上面那条（零区分力——Q7/Q8 同一个坑）。
    """
    res = classify_smoke_outcome(
        "1", "/src/Api/UserController.cs(23,17): error CS0103: The name '_repo' "
        "does not exist in the current context", [], language_key="csharp")
    assert res.status == "failed", f"{res.status}/{res.classification}"
    assert res.classification == "code_error"


# ══════════════════════════════════════════════
# hunter 复核整改：F-3 / F-4 / F-5 边界
# ══════════════════════════════════════════════

@pytest.mark.parametrize("lang,log", [
    ("ruby", "/app/config/database.rb:7:in `<main>': undefined method `split' for nil"
             ":NilClass (NoMethodError)"),
    ("ruby", "/app/config/app.rb:3: undefined method `upcase' for nil (NoMethodError)"),
    ("dart", "Unhandled exception:\nNoSuchMethodError: The method 'split' was called "
             "on null."),
])
def test_nil_deref_is_not_code_error(lang, log):
    """★F-3★ nil/null 解引用不判 code_error——沙箱缺 ENV 时 `ENV["X"].split(":")` 恒如此。

    `failed:code_error` 会硬拦交付 + 回灌重派写者，把环境问题变成烧预算的重试循环
    （fail-honest 铁律：环境绝不伪装代码失败）。
    突变判据：把否定预查 `(?!nil)` / `(?!...called on null)` 去掉，本条必红。
    """
    res = classify_smoke_outcome("1", log, [], language_key=lang)
    assert res.status != "failed", (
        f"{lang} nil-deref 被冤判 {res.status}/{res.classification}（环境伪装成代码）")
    # 但"崩了"这个事实必须机读可辨（否则回到 C-6：崩溃与"什么都没发生"同值）
    assert res.classification == "startup_crash_unattributed", res.classification


@pytest.mark.parametrize("lang,log", [
    ("ruby", "app/models/user.rb:17: undefined method `upcase' for an instance of User "
             "(NoMethodError)"),
    ("dart", "NoSuchMethodError: Class 'Report' has no instance method 'toUpperCase'."),
])
def test_genuine_missing_method_still_code_error(lang, log):
    """★F-3 对照臂★ 真·方法不存在（接收者非 nil）照旧 `failed:code_error`。

    没有这条，"把 NoMethodError 整条删掉"也能满足上面那组（零区分力）。
    """
    res = classify_smoke_outcome("1", log, [], language_key=lang)
    assert res.status == "failed" and res.classification == "code_error", \
        f"{res.status}/{res.classification}"


@pytest.mark.parametrize("log", [
    "SQLSTATE[42000]: Syntax error or access violation: 1064 You have an error in your "
    "SQL syntax near 'SELECT * FORM users'",
    "SQLSTATE[42S02]: Base table or view not found: 1146 Table 'app.orders' doesn't exist",
])
def test_sql_syntax_and_missing_table_are_not_env_missing(log):
    """★F-4（假过）★ `SQLSTATE[42xxx]` 是 SQL 语法错/表不存在＝代码或迁移缺陷，不是"环境缺失"。

    原前缀 `SQLSTATE\\[` 通吃全域 → 判 env_missing → skipped → auto_accept 放行坏产物，
    且消息自信地写"沙箱内无外部服务"。收窄到连接类 class code（08/28/3D/HY000）。
    突变判据：把 class code 限定去掉退回 `SQLSTATE\\[`，本条必红。
    """
    res = classify_smoke_outcome("1", log, [], language_key="php")
    assert res.classification != "env_missing", (
        f"SQL 缺陷被归成环境缺失（自信且错误的归因）：{res.classification}")


def test_connection_class_sqlstate_still_env_missing():
    """★F-4 对照臂★ 连接类 SQLSTATE 照旧 env_missing（收窄没把这档一起砍掉）。"""
    # ★样本刻意不含字面 `Connection refused`★ 那是 `_ENV_MISSING_PATTERNS["generic"]`
    # 抓的——**通用族会替语言族背书**，删掉 php 条目本条照旧绿（突变 H4b 当场证实，
    # 与本文件上方 Q7/Q8 同一个坑）。08xxx 是连接类 class code。
    res = classify_smoke_outcome(
        "1", "SQLSTATE[08006] [7] server closed the connection unexpectedly",
        [], language_key="php")
    assert res.classification == "env_missing", res.classification


@pytest.mark.parametrize("lang,log,why", [
    ("ruby", "Could not find gem 'pg' in locally installed gems", "gem 未装（ruby 最高频）"),
    ("ruby", "/usr/lib/ruby/rubygems.rb:275:in `to_spec': LoadError", "LoadError 裸形态"),
    ("csharp", "/src/Api/UserController.cs(4,7): error CS0246: The type or namespace "
               "name 'Newtonsoft' could not be found\nThe build failed.", "NuGet 未 restore"),
])
def test_dependency_absence_is_crash_not_nothing_observed(lang, log, why):
    """★F-5★ "依赖没装/编译没过"必须机读可辨为**崩了**，不许落 `inconclusive`。

    `inconclusive` 的语义是"探活窗口耗尽/无任何已知形态命中"——把「日志明写 error CS0246 +
    The build failed」与「什么都没发生」写成同一个值，正是本文件 C-6 批判的病灶。
    仍 skipped（不冤枉代码），但交付面与复盘能一眼看出应用没起来。
    """
    res = classify_smoke_outcome("1", log, [], language_key=lang)
    assert res.status == "skipped", f"{why}: {res.status}"
    assert res.classification == "startup_crash_unattributed", f"{why}: {res.classification}"


# ══════════════════════════════════════════════
# reviewer 复核整改：C-1 / C-2 / I-2 / I-3
# ══════════════════════════════════════════════

@pytest.mark.parametrize("log", [
    # Rails `rescue => e; logger.error "#{e.class}: #{e.message} for request #{path}"`
    "NoMethodError: undefined method `split' for nil:NilClass (NoMethodError) for "
    "request /api/users",
    # lograge / semantic_logger 的单行 JSON
    '{"msg":"undefined method `fetch\' for nil:NilClass (NoMethodError)",'
    '"tag":"retry for queue default"}',
])
def test_greedy_backtrack_cannot_bypass_nil_receiver_exclusion(log):
    """★C-1★ 同一行里出现**第二个** " for " 时，否定预查不许被回溯绕过。

    原 `[^\\n]{0,80}` 贪婪且能跨空格 → `(?!nil)` 落到靠后那个 " for " 上 → nil-deref
    又变回 `failed:code_error` → 硬拦交付 + 回灌重派写者去修一个环境问题（F-3 原病复发）。
    突变判据：把 `\\S{1,80}` 改回 `[^\\n]{0,80}`，本条必红。
    """
    res = classify_smoke_outcome("1", log, [], language_key="ruby")
    assert res.status != "failed", (
        f"贪婪回溯绕过了 nil 排除 → {res.status}/{res.classification}")


def test_sqlite_general_error_bucket_is_not_env_missing():
    """★C-2★ `SQLSTATE[HY000]` 是"通用错误"桶（PDO_SQLITE 全映射到它），不是连接类。

    SQLite 恰是沙箱里唯一不需要外部服务就能真跑的配置＝最可能实际执行到的那个。
    worker 漏写 migration → `no such table` → 原判 env_missing → skipped → auto_accept 放行。
    突变判据：把 HY000 的驱动码限定去掉、退回裸 `SQLSTATE\\[HY000`，本条必红。
    """
    res = classify_smoke_outcome(
        "1", "SQLSTATE[HY000]: General error: 1 no such table: users", [],
        language_key="php")
    assert res.classification != "env_missing", (
        f"SQLite 迁移缺陷被归成『沙箱没有外部服务』：{res.classification}")


@pytest.mark.parametrize("log,why", [
    # 样本刻意不含 generic 族的字面（`Connection refused` 等）——否则通用族替语言族背书，
    # 删掉 HY000 驱动码那行本条照旧绿（本会话第 4 次同型，突变 R-C2b 当场证实）
    ("SQLSTATE[HY000] [2005] Unknown MySQL server host 'db' (-2)", "MySQL 连接类驱动码"),
    ("SQLSTATE[08006] [7] server closed the connection unexpectedly", "08 连接类"),
    ("SQLSTATE[57P03]: the database system is starting up", "PG cannot_connect_now"),
])
def test_real_connection_failures_still_env_missing(log, why):
    """★C-2 对照臂★ 真连接失败照旧 env_missing（收窄没把这档砍掉）。"""
    res = classify_smoke_outcome("1", log, [], language_key="php")
    assert res.classification == "env_missing", f"{why}: {res.classification}"


def test_nuget_restore_cascade_companion_codes_are_not_code_error():
    """★I-2★ NuGet 未 restore 会**级联**出伴生码：声明处 CS0246，表达式处 **CS0103**。

    共存是 restore 失败的常态 → 类1 的 `\\d{4}` 黑名单命中伴生码 → 判 code_error →
    环境冤枉成代码。治法＝依赖缺失形态在场时本轮不进类1（与 ambiguous 同族的保守规则）。
    突变判据：把 `code_hits and _dep_absent_hits` 那段删掉，本条必红。
    """
    log = ("/src/Api/Svc.cs(4,7): error CS0246: The type or namespace name 'Newtonsoft' "
           "could not be found\n"
           "/src/Api/Svc.cs(19,13): error CS0103: The name 'JsonConvert' does not exist "
           "in the current context")
    res = classify_smoke_outcome("1", log, [], language_key="csharp")
    assert res.status != "failed", (
        f"restore 失败的级联伴生码被判代码缺陷：{res.status}/{res.classification}")


def test_dart_null_receiver_second_wording_is_excluded():
    """★I-3★ Dart 对 null 接收者有两套措辞，`Class 'Null' has no instance …` 也要排除。

    它与"真·无此方法"（`Class 'Report' has no instance method`）**字面同构、只差类名**。
    突变判据：把 `Class 'Null' has no instance` 从否定预查里删掉，本条必红。
    """
    res = classify_smoke_outcome(
        "1", "NoSuchMethodError: Class 'Null' has no instance getter 'databaseUrl'.",
        [], language_key="dart")
    assert res.status != "failed", f"{res.status}/{res.classification}"
