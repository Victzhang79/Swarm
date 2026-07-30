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
    """★接线的起点★ 这些 token 取自 `stack_detect._LANG_EXTS` 的键，不是猜的拼法。

    突变判据：从 `_LANGUAGE_ALIASES` 删掉任一条，对应参数化用例必红。
    """
    assert normalize_language_key(display) == expect


# ── 真实代码故障日志（各语言运行时原样输出）→ 必须 failed:code_error ──
CODE_ERROR_LOGS = [
    ("php", "PHP Parse error:  syntax error, unexpected token \";\" in "
            "/app/routes/web.php on line 42"),
    ("php", "PHP Fatal error:  Uncaught TypeError: App\\Http\\UserController::show(): "
            "Argument #1 ($id) must be of type int, string given"),
    ("ruby", "app/models/user.rb:17:in `full_name': undefined method `upcase' for nil "
             "(NoMethodError)"),
    ("ruby", "/app/config/routes.rb:8: syntax error, unexpected ')', expecting "
             "end-of-input"),
    ("csharp", "/src/Controllers/UserController.cs(23,17): error CS0103: The name "
               "'_repo' does not exist in the current context"),
    ("csharp", "Unhandled exception. System.NullReferenceException: Object reference "
               "not set to an instance of an object."),
    ("elixir", "** (CompileError) lib/app_web/router.ex:12: undefined function get/2"),
    ("elixir", "** (FunctionClauseError) no function clause matching in "
               "AppWeb.UserController.show/2"),
    ("dart", "Unhandled exception:\nNoSuchMethodError: The method 'toUpperCase' was "
             "called on null."),
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
    missing_code = alias_keys - set(_CODE_ERROR_PATTERNS)
    assert not missing_code, (
        f"这些语言有别名但无 code_error 族 → 恒不产 failed（静默失效）：{sorted(missing_code)}")
    # 崩溃/环境两族允许只吃 generic，但缺口要可见（当前应为空；新增语言时刻意打红提醒）
    assert not (alias_keys - set(_STARTUP_CRASH_PATTERNS)), sorted(
        alias_keys - set(_STARTUP_CRASH_PATTERNS))
    assert not (alias_keys - set(_ENV_MISSING_PATTERNS)), sorted(
        alias_keys - set(_ENV_MISSING_PATTERNS))
