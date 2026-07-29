"""provider API key 多槽 + 配额感知轮换（用户 2026-07-30 拍板）。

病灶：`_resolve_api_key` 每 provider 只认一把 key——额度耗尽后该 provider 每次调用必然
失败直到人工换 key；而额度往往过几天自动恢复，"换掉"既要人守着、又丢掉旧 key 的价值。
"""
from __future__ import annotations

import inspect

import pytest

from swarm.models import key_rotation as kr
from swarm.models.errors import is_auth_shaped_error


@pytest.fixture(autouse=True)
def _clean():
    kr.reset_for_tests()
    yield
    kr.reset_for_tests()


# ══════════════════════════════════════════════
# 判据：配额形态 vs 凭据形态必须分得开
# ══════════════════════════════════════════════

@pytest.mark.parametrize("msg", [
    "Error code: 429 - Too Many Requests",
    "Error code: 403 - insufficient_quota",
    "quota exceeded for this month",
    "rate limit reached",
    "Insufficient Balance",
])
def test_quota_shapes_detected(msg):
    """额度/限流类**有专属出路**（换一把 key），故必须与"配置错"分开判。"""
    assert kr.is_quota_shaped_error(msg)


@pytest.mark.parametrize("msg", [
    "Error code: 403 - PermissionDenied: invalid api key",
    "Error code: 401 - Unauthorized",
    "connect timeout after 30s",
    "errcode=42900 供应商业务错误",      # 数字码裸子串误判面
    "listening on port 4290",
])
def test_non_quota_shapes_not_flagged(msg):
    """★误伤半径大：把好 key 冷却掉＝自己制造故障★
    数字码只取首行 + 词边界（纪律同 models/errors.py：errcode/端口/trace id 会误判）。"""
    assert not kr.is_quota_shaped_error(msg)


def test_pure_credential_error_still_goes_to_auth_channel():
    """纯凭据错（invalid api key）只能等人修，绝不能被当配额去换槽——
    换一把同样无效的 key 只是把故障换个地方发生。"""
    msg = "Error code: 403 - PermissionDenied: invalid api key"
    assert is_auth_shaped_error(msg) and not kr.is_quota_shaped_error(msg)


def test_quota_403_is_routed_to_rotation_not_to_human():
    """★配额类 403 两个判据都会命中——回调必须【先判配额】★
    否则 is_auth_shaped_error 先吞掉它升 ERROR 等人工，而它其实是自动可解的。"""
    msg = "Error code: 403 - insufficient_quota"
    assert kr.is_quota_shaped_error(msg) and is_auth_shaped_error(msg)
    # 行为级：喂一条配额类 403 给真回调，断言它走了【换槽】而不是【升 ERROR 等人修】。
    # （初版断言两个函数名在源码里的先后位置——被自己的注释坑了：注释里先提到 auth。
    #   文本位置从来不是执行顺序，这正是"禁结构焊死测试"要防的。）
    from swarm.models.router import ModelInvocationLogger
    lg = ModelInvocationLogger("brain/primary", "m", "p-quota", key_slot=1)
    lg.on_llm_error(RuntimeError(msg))
    assert kr.snapshot()["rotations"].get("p-quota") == 1, "配额类 403 必须触发换槽"


# ══════════════════════════════════════════════
# 选槽 / 冷却 / 回池
# ══════════════════════════════════════════════

def test_slot_1_keeps_the_legacy_secret_name():
    """★零迁移★：槽 1 就是既有命名，已有部署一行都不用改。"""
    assert kr.secret_name("kimi-code", 1) == "provider_api_key:kimi-code"
    assert kr.secret_name("kimi-code", 2) == "provider_api_key:kimi-code#2"


def test_exhausted_slot_is_skipped():
    assert kr.select_slot("p", [1, 2]) == 1
    kr.mark_exhausted("p", 1, "429")
    assert kr.select_slot("p", [1, 2]) == 2


def test_all_cooling_still_returns_a_key():
    """★绝不静默无 key（fail-open）★：一把可能过期的 key 好过没有 key——
    后者是**确定**失败。但"全都在冷却"必须 WARNING 可见。"""
    kr.mark_exhausted("p", 1)
    kr.mark_exhausted("p", 2)
    assert kr.select_slot("p", [1, 2]) in (1, 2)


def test_single_slot_degrades_to_old_behaviour():
    """未配备用槽 → 行为与改动前逐字等价（只有槽 1 可选）。"""
    assert kr.select_slot("q", [1]) == 1
    kr.mark_exhausted("q", 1)
    assert kr.select_slot("q", [1]) == 1


def test_cooldown_expiry_returns_slot_to_the_pool(monkeypatch):
    """★额度恢复无需任何人工动作★——这是"冷却而非淘汰"的全部意义。"""
    monkeypatch.setenv("SWARM_KEY_ROTATION_COOLDOWN_S", "0.05")
    kr.mark_exhausted("p", 1)
    assert kr.select_slot("p", [1, 2]) == 2
    import time
    time.sleep(0.08)
    assert kr.select_slot("p", [1, 2]) == 1, "冷却期满必须自动回池"


def test_no_slots_returns_none():
    """无任何槽 → None（调用方回退 .env 明文，与改动前一致）。"""
    assert kr.select_slot("p", []) is None


def test_snapshot_is_observable():
    """★降级面必须机读可查（方法论硬检查④）★"""
    kr.mark_exhausted("p", 1, "429")
    snap = kr.snapshot()
    assert snap["rotations"]["p"] == 1
    assert "p#1" in snap["cooling"] and snap["cooling"]["p#1"] > 0


# ══════════════════════════════════════════════
# 接线：所有构造点都要带 slot（元教训①）
# ══════════════════════════════════════════════

def test_every_logger_construction_carries_the_slot():
    """★接线覆盖 ≠ 机制存在（本轮元教训①，已有 3 个实例）★
    漏一处，那条路径上的配额耗尽就会把**别的槽**（或默认槽1）打进冷却——
    轮换在该路径上不仅无效，还有害。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "models" / "router.py").read_text()
    lines = src.splitlines()
    missing = []
    for i, ln in enumerate(lines):
        if "ModelInvocationLogger(" not in ln or ln.lstrip().startswith("class "):
            continue
        if "key_slot" not in "\n".join(lines[i:i + 4]):
            missing.append(i + 1)
    assert not missing, f"这些 ModelInvocationLogger 构造点没带 key_slot：行 {missing}"


def test_resolve_api_key_enumerates_slots():
    """接线事实：配置侧必须真去枚举多个槽（否则备用 key 永远选不到）。"""
    from swarm.config import settings
    src = inspect.getsource(settings.ModelConfig._resolve_api_key)
    assert "_KEY_SLOT_MAX" in src and "select_slot" in src


def test_active_slot_is_recorded_for_error_attribution():
    """选中的槽号必须落地——否则错误回调无从知道"是哪把 key 撞的额度"。"""
    from swarm.config.settings import get_config
    cfg = get_config().model
    cfg._effective_providers()
    assert isinstance(cfg._active_key_slot, dict)


def test_rotation_env_is_registered():
    """新增环境开关必须登记（本仓有强制测试，此处是提前自检）。"""
    from swarm.config.env_registry import REGISTERED_ENVS
    assert "SWARM_KEY_ROTATION_COOLDOWN_S" in REGISTERED_ENVS
