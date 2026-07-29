"""26 号文 E 路：E-H1 熔断器把并发在飞结果当探针 + E-H2「连续失败」在并发下不成立。

E-H1 的日志谎报尤其要紧：生产日志 `logs/8df2a953:139-144` 实证 open 那行之后紧跟
"探针失败"，中间**没有**"冷却期满→放行探针"——round67m 复盘把 k3 事故压缩成"三连超时"
很可能就是被这条假日志误导的。
"""
from __future__ import annotations

import pytest

from swarm.models import breaker as b


@pytest.fixture(autouse=True)
def _clean():
    b._reset_for_tests()
    yield
    b._reset_for_tests()


def _open(key="m", n=3):
    for _ in range(n):
        b.record_failure(key)
    assert b.snapshot()[key]["open"]


# ══════════════════════════════════════════════
# E-H1：熔断前在飞的调用被当成半开探针
# ══════════════════════════════════════════════

def test_straggler_failure_does_not_extend_cooldown():
    """★冷却被 straggler 无限续期（26 号文 E-H1）★
    原判据只看 `opened_at is not None`：**熔断之前就已在飞**的并发调用失败落到这里，
    被当成"探针失败"重置 opened_at。饱和场景下 straggler 源源不断 = 冷却永不结束。"""
    _open()
    t0 = b._states["m"].opened_at
    b.record_failure("m")            # straggler：没持探针名额
    assert b._states["m"].opened_at == t0, "冷却起点绝不能被 straggler 重置"


def test_straggler_success_does_not_close_early():
    """★对称面：一次早发成功即提前复位★
    熔断前在飞的调用成功返回，不代表模型已恢复——提前复位会让刚熔断的死模型立刻被打满。"""
    _open()
    b.record_success("m")
    assert b.snapshot()["m"]["open"], "straggler 的成功不该提前解除熔断"


def test_real_probe_failure_still_reopens():
    """闸不能矫枉过正：**真拿到探针名额**的调用失败，仍要重新熔断、冷却重计。"""
    _open()
    b._states["m"].opened_at -= 10_000        # 冷却已满
    assert b.allow("m") is True               # 领到探针
    assert b._states["m"].probing is True
    t0 = b._states["m"].opened_at
    b.record_failure("m")
    assert b._states["m"].opened_at != t0 and b.snapshot()["m"]["open"]


def test_real_probe_success_closes():
    """真探针成功 → 闭合复位（既有语义不变）。"""
    _open()
    b._states["m"].opened_at -= 10_000
    assert b.allow("m") is True
    b.record_success("m")
    assert not b.snapshot()["m"]["open"]


def test_probe_slot_is_the_discriminator_not_open_state():
    """★判别依据必须是"有没有持探针名额"而不是"熔断开着没"★
    这两件事在并发下完全不同——前者是 allow() 发的凭证，后者只是状态。"""
    _open()
    assert b._states["m"].probing is False, "熔断开启时没有在飞探针"
    b.record_failure("m")                      # 无名额 → straggler
    b._states["m"].opened_at -= 10_000
    b.allow("m")                               # 领名额
    assert b._states["m"].probing is True


# ══════════════════════════════════════════════
# E-H2：「连续失败」是进程级全局交织
# ══════════════════════════════════════════════

def _feed(seq, key="m"):
    for x in seq:
        (b.record_failure if x == "f" else b.record_success)(key)
    return b.snapshot().get(key, {}).get("open", False)


def test_interleaved_failures_now_open():
    """★实测 12 失败 + 6 成功交织 → `consecutive_failures=0` **永不熔断**（26 号文 E-H2）★
    计数是**进程级**的，而调用是并发交织的：每次成功都把"连续"清零。
    改为滑窗内失败占优即熔断。"""
    assert _feed(["f", "f", "s"] * 6) is True


def test_consecutive_failures_still_open():
    """旧判据保留：连着挂仍是强信号，任一成立即熔断（不削弱既有防护）。"""
    assert _feed(["f"] * 3) is True


def test_alternating_success_failure_does_not_open():
    """★50/50 交替不熔断★：失败必须真的【占优】。
    此处踩过一个坑——`record_success` 在无状态时早退不建状态，首次成功被丢弃，
    交替序列被算成"失败多一个"而误熔断。成功也必须进窗口。"""
    assert _feed(["s", "f"] * 10) is False


def test_success_dominant_does_not_open():
    """健康模型（成功占多数）绝不熔断——误伤会把好模型踢出机队。"""
    assert _feed(["s", "s", "f"] * 7) is False


def test_success_creates_state_so_window_is_symmetric():
    """★窗口两侧必须对称记账★：只记失败不记成功的窗口，判据必然偏向熔断。"""
    b.record_success("brand-new")
    assert "brand-new" in b._states


def test_window_is_pruned_and_bounded():
    """窗口按时间裁剪 + 容量兜底——病理高频调用不该把内存撑爆。"""
    from swarm.models.breaker import _WINDOW_MAX, _prune_window
    st = b._BState()
    st.recent = [(0.0, False)] * (_WINDOW_MAX + 500)
    _prune_window(st, 0.0)
    assert len(st.recent) <= _WINDOW_MAX


def test_open_clears_window():
    """熔断开启即清窗口——否则解除后旧失败仍在窗口里，会立刻二次误熔断。"""
    _feed(["f"] * 3)
    assert b._states["m"].recent == []


def test_small_sample_falls_back_to_consecutive_rule():
    """★比例判据需要足够样本，否则把误伤换个方向再犯一遍★
    3 失败 + 1 成功也满足"失败占优"，但那是小样本噪声——既有语义"一次成功证明模型
    还活着"在这个规模上是对的（test_success_resets_breaker_counter 编码的正是它）。"""
    assert _feed(["f", "f", "s", "f"]) is False


def test_replay_rejects_recording_from_another_model(monkeypatch, tmp_path):
    """★回放命中判据只哈希 messages → 跨模型照样命中（26 号文 E-M3）★
    复核实测不同模型 + 不同节点 + temperature 1.9 全部命中旧录像。而离线重放是拍板的
    "验治本主力手段"——验"换模型/关 thinking"这类改动时静默返回旧结果，结论方向与真实相反。"""
    import swarm.models.cassette_playback as cp
    from swarm.models.cassette_record import compute_request_sha

    # lookup 前有 `enabled()` 门控（读 SWARM_CASSETTE_REPLAY_DIR）——不设它整条早退
    monkeypatch.setenv("SWARM_CASSETTE_REPLAY_DIR", str(tmp_path))
    args = ([{"role": "user", "content": "hi"}],)
    _msgs, sha = compute_request_sha(args, {})
    rec = {"request_sha": sha, "node": "brain", "model": "模型A", "chunks": []}
    # `_ensure_index` 按 `_indexed_dir != playback_dir()` 重建索引——只塞 _index 会被
    # 当场重建成空。两个都要钉（这个坑本身就是"缓存失效键不止一个"的小实例）。
    cp._index = {sha: __import__("collections").deque([rec])}
    cp._indexed_dir = str(tmp_path)
    try:
        assert cp.lookup("brain", "模型B", args, {}) is None, "别的模型的录像绝不能冒充"
        # 还回队列：真正同模型的调用仍要能命中（未消费）
        assert cp.lookup("brain", "模型A", args, {}) is rec
    finally:
        cp._index = None
        cp._indexed_dir = None


def test_replay_lax_match_escape_hatch(monkeypatch, tmp_path):
    """★留逃生门而不是无声收紧★：历史录像可能缺 model 字段或跨模型复用是刻意的。
    但默认必须严格——默认宽松正是 E-M3 那条"验了个寂寞"的成因。"""
    import collections

    import swarm.models.cassette_playback as cp
    from swarm.models.cassette_record import compute_request_sha

    # lookup 前有 `enabled()` 门控（读 SWARM_CASSETTE_REPLAY_DIR）——不设它整条早退
    monkeypatch.setenv("SWARM_CASSETTE_REPLAY_DIR", str(tmp_path))
    args = ([{"role": "user", "content": "hi"}],)
    _msgs, sha = compute_request_sha(args, {})
    monkeypatch.setenv("SWARM_CASSETTE_LAX_MATCH", "1")
    cp._index = {sha: collections.deque([{"request_sha": sha, "model": "模型A", "chunks": []}])}
    cp._indexed_dir = str(tmp_path)
    try:
        assert cp.lookup("brain", "模型B", args, {}) is not None
    finally:
        cp._index = None
        cp._indexed_dir = None


def test_replay_tolerates_legacy_records_without_model(monkeypatch, tmp_path):
    """旧格式录像无 model 字段 → 放行，不砸历史录像。"""
    import collections

    import swarm.models.cassette_playback as cp
    from swarm.models.cassette_record import compute_request_sha

    # lookup 前有 `enabled()` 门控（读 SWARM_CASSETTE_REPLAY_DIR）——不设它整条早退
    monkeypatch.setenv("SWARM_CASSETTE_REPLAY_DIR", str(tmp_path))
    args = ([{"role": "user", "content": "hi"}],)
    _msgs, sha = compute_request_sha(args, {})
    cp._index = {sha: collections.deque([{"request_sha": sha, "chunks": []}])}
    cp._indexed_dir = str(tmp_path)
    try:
        assert cp.lookup("brain", "任意模型", args, {}) is not None
    finally:
        cp._index = None
        cp._indexed_dir = None


def test_model_miss_is_counted_separately():
    """★分项统计★：miss_model（该换新录像）与 miss_sha（指纹漂移/录像陈旧）的运维处置
    完全不同，混在一起会误诊。"""
    from swarm.models.cassette_playback import _stats
    assert "miss_model" in _stats
