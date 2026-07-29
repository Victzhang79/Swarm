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
    "HTTP 429 rate limit reached",   # 真限流必带 429；裸 `rate limit` 刻意不作独立判据
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
# 接线：★行为级★（两个透镜的突变实验都证明初版是结构焊死的假绿）
# ══════════════════════════════════════════════
#
# 初版两条守卫全是源码文本断言，突变实验里 4/9 存活：
#   · `_KEY_SLOT_MAX = 1`（多槽整个删掉、备用 key 永远选不到）→ 全绿
#   · `_active_slot` 恒返 1（归因整个删掉）→ 全绿
#   · 逐行扫 router.py 找 `key_slot` 字面量 → 改成 `key_slot=0`（语义已坏）照绿；
#     把构造点格式化成 6 行（超出 4 行窗口）→ 正确代码反被判红。双向失灵。
# 下面这条一发同时杀掉全部四个突变。


def _fake_slots(monkeypatch, values: dict[int, str], pid="p-rot"):
    """给指定 provider 造多槽 secret；其余键返回 None。"""
    from swarm.config import secret_store as ss
    from swarm.models import key_rotation as _kr
    names = {_kr.secret_name(pid, sl): v for sl, v in values.items()}
    monkeypatch.setattr(ss, "get_secret", lambda name, *a, **k: names.get(name))


def test_rotation_actually_changes_the_key_in_use(monkeypatch):
    """★真配槽2 → 断言取出的 key 变了（这是唯一能证明机制活着的断言）★
    冷却槽1 后，`_resolve_api_key` 必须返回**槽2 的那把 key**，且槽号随之变。"""
    from swarm.config.settings import ModelConfig
    cfg = ModelConfig()
    _fake_slots(monkeypatch, {1: "KEY-A", 2: "KEY-B"})

    assert cfg._resolve_api_key("p-rot", "env") == ("KEY-A", 1)
    kr.mark_exhausted("p-rot", 1, "429")
    assert cfg._resolve_api_key("p-rot", "env") == ("KEY-B", 2), \
        "槽1 冷却后必须真的换到槽2 的 key"


def test_slot_travels_with_the_key_not_through_globals(monkeypatch):
    """★槽号必须与 key 同生命周期同对象（对抗双复核 CRITICAL：并发 3 秒 64 次串槽）★
    初版槽号写在 ModelConfig 的 PrivateAttr、回调回查【全局】get_config()——
    reload 会 new 一个 AppConfig 让它归零，于是把**健康槽**打进冷却、
    真正耗尽的那槽永不冷却，轮换永不推进。"""
    from swarm.config.settings import ModelConfig, ProviderConfig
    from swarm.models.router import _active_slot

    cfg = ModelConfig(providers=[ProviderConfig(id="p-rot", base_url="http://x")])
    _fake_slots(monkeypatch, {1: "KEY-A", 2: "KEY-B"})
    kr.mark_exhausted("p-rot", 1, "429")

    prov = cfg._effective_providers()[0]
    assert (prov.api_key, prov.key_slot) == ("KEY-B", 2), "槽号必须落在 provider 对象上"
    assert _active_slot(prov) == 2, "回调必须从 provider 取槽，不查任何全局态"


def test_no_multislot_means_no_cooldown_at_all(monkeypatch):
    """★没走多槽解析的 provider 撞 429 → 不冷却任何槽（hunter H4）★
    初版默认按槽1 冷却——冷却一个**根本不存在的槽**，还打一条"已切换到下一个可用槽"
    的 ERROR（作假宣称）。本地 vLLM 队列满返 429 同样中招。"""
    from swarm.config.settings import ModelConfig
    from swarm.models.router import ModelInvocationLogger
    cfg = ModelConfig()
    _fake_slots(monkeypatch, {})                    # secret_store 里一个槽都没有
    assert cfg._resolve_api_key("p-env", "env-key") == ("env-key", 0)

    ModelInvocationLogger("brain/primary", "m", "p-env",
                          key_slot=0).on_llm_error(RuntimeError("429 too many requests"))
    assert not kr.snapshot()["cooling"], "无槽可换时绝不冷却任何东西"
    assert "p-env" not in kr.snapshot()["rotations"]


def test_callback_does_not_rebuild_global_config():
    """★回调里绝不 reload_config（对抗双复核 HIGH，两个透镜独立实证）★
    实证：不 reload 换槽照样生效（`_effective_providers` 每次重跑解析）；
    而它 49ms/次、无节流无上界、连带清空 5 个与 key 毫无关系的缓存。"""
    # 行为级：spy 掉 reload_config，喂一条配额错，断言它一次都没被调
    # （初版断源码里没有 `reload_config` 字面量——被自己的解释性注释坑红了；
    #   文本从来不是行为，这正是"禁结构焊死测试"要防的。）
    import swarm.config.settings as _st
    from swarm.models.router import ModelInvocationLogger
    calls = []
    _orig = _st.reload_config
    _st.reload_config = lambda *a, **k: calls.append(1) or _orig()
    try:
        ModelInvocationLogger("brain/primary", "m", "p-noreload",
                              key_slot=1).on_llm_error(RuntimeError("429 too many requests"))
    finally:
        _st.reload_config = _orig
    assert not calls, "回调绝不能重建全局配置（49ms/次 + 清 5 个无关缓存 + 无上界）"


def test_credential_error_still_reaches_the_human_channel():
    """★配额判据抢在 auth 之前——判宽了就会顶掉 A3 的凭据 ERROR★
    无效 key 的 401 被判成配额去静默轮换，等于把 round67m2「k3 403 静默 2h20m」
    那个刚治好的洞重新打开。"""
    from swarm.models.errors import is_auth_shaped_error
    msg = "Error code: 401 - {'code':'invalid_api_key'}, 'id':'chatcmpl-b429c'"
    assert not kr.is_quota_shaped_error(msg), "确定的凭据错绝不能走轮换"
    assert is_auth_shaped_error(msg), "它必须落到 auth 通道等人修"


def test_credential_marker_wins_when_both_shapes_present():
    """★两种形态同现时【凭据优先】——这是 fail-safe 方向，也是唯一能鉴别该排除的用例★
    （上一条其实鉴别不了：`\b429\b` 修完后 `chatcmpl-b429c` 本就不命中，
      删掉凭据排除它照样绿——突变实验当场证伪。真正的判据是"两者都在时谁赢"。）
    换一把同样无效的 key 只是把故障换个地方发生，还会顶掉 A3 的凭据 ERROR。"""
    both = "Error code: 403 - invalid_api_key; quota exceeded for this key"
    assert not kr.is_quota_shaped_error(both), "凭据错必须压过配额判据"


def test_transient_marker_wins_over_quota_code():
    """同理另一侧：`read timeout=429` 是瞬时基建故障，归 breaker 不归轮换。"""
    assert not kr.is_quota_shaped_error("Read timed out. (read timeout=429)")


def test_rotation_env_is_registered():
    """新增环境开关必须登记（本仓有强制测试，此处是提前自检）。"""
    from swarm.config.env_registry import REGISTERED_ENVS
    assert "SWARM_KEY_ROTATION_COOLDOWN_S" in REGISTERED_ENVS
