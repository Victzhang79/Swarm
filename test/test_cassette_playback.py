"""Task#12：LLM cassette playback（cassette_record 回放对偶）——按 request_sha 喂回录制 chunks。

覆盖：同源指纹匹配（record↔playback 同 sha）/chunk 重建（含 reasoning/finish_reason）/
miss 策略（passthrough vs error）/FIFO 同指纹重复消费/门控 env/fail-open。
"""
from __future__ import annotations

import json

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

import swarm.models.cassette_playback as pb
from swarm.models.cassette_record import compute_request_sha


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("SWARM_CASSETTE_REPLAY_DIR", raising=False)
    monkeypatch.delenv("SWARM_CASSETTE_REPLAY_MISS", raising=False)
    pb.reset_index()
    yield
    pb.reset_index()


def _args(msgs):
    """模拟 _astream(self, messages, ...)——绑定方法 self 不入 args，args[0]=messages。"""
    return (msgs,), {}


def _write_cassette(tmp_path, records):
    d = tmp_path / "cass"
    d.mkdir()
    with open(d / "llm-1234.jsonl", "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return str(d)


def _rec(msgs, chunks, seq=0, node="plan", model="m"):
    _m, sha = compute_request_sha(*_args(msgs)) if False else (None, None)
    from swarm.models.cassette_record import compute_request_sha as _c
    _md, sha = _c(*_args(msgs))
    return {"schema": 1, "seq": seq, "node": node, "model": model,
            "request_sha": sha, "messages": _md, "chunks": chunks}


# ── 门控 ──
def test_disabled_by_default():
    assert pb.playback_enabled() is False


def test_enabled_when_env_set(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_CASSETTE_REPLAY_DIR", str(tmp_path))
    assert pb.playback_enabled() is True


# ── 同源指纹匹配 ──
def test_lookup_matches_recorded_sha(tmp_path, monkeypatch):
    msgs = [SystemMessage(content="sys"), HumanMessage(content="hi")]
    d = _write_cassette(tmp_path, [_rec(msgs, [{"content": "hello"}])])
    monkeypatch.setenv("SWARM_CASSETTE_REPLAY_DIR", d)
    a, k = _args(msgs)
    rec = pb.lookup("plan", "m", a, k)
    assert rec is not None, "同 messages 必须命中（record↔playback 同源指纹）"
    assert rec["chunks"][0]["content"] == "hello"


def test_lookup_miss_on_different_messages(tmp_path, monkeypatch):
    d = _write_cassette(tmp_path, [_rec([HumanMessage(content="A")], [{"content": "x"}])])
    monkeypatch.setenv("SWARM_CASSETTE_REPLAY_DIR", d)
    a, k = _args([HumanMessage(content="B")])   # 不同 prompt → sha 不同 → miss
    assert pb.lookup("plan", "m", a, k) is None


# ── chunk 重建 ──
async def test_replay_chunks_reconstructs(tmp_path, monkeypatch):
    msgs = [HumanMessage(content="q")]
    chunks = [
        {"content": "par", "additional_kwargs": {"reasoning_content": "think"},
         "generation_info": {"finish_reason": None}},
        {"content": "t2", "generation_info": {"finish_reason": "stop"}},
    ]
    d = _write_cassette(tmp_path, [_rec(msgs, chunks)])
    monkeypatch.setenv("SWARM_CASSETTE_REPLAY_DIR", d)
    a, k = _args(msgs)
    rec = pb.lookup("plan", "m", a, k)
    out = [c async for c in pb.replay_chunks(rec)]
    assert len(out) == 2
    assert out[0].message.content == "par"
    assert out[0].message.additional_kwargs.get("reasoning_content") == "think"
    assert out[1].generation_info.get("finish_reason") == "stop", "finish_reason 必须保真（round64 教训）"
    full = "".join(c.message.content for c in out)
    assert full == "part2"


# ── FIFO 同指纹重复（retry 场景）──
def test_fifo_duplicate_sha(tmp_path, monkeypatch):
    msgs = [HumanMessage(content="dup")]
    d = _write_cassette(tmp_path, [
        _rec(msgs, [{"content": "first"}], seq=1),
        _rec(msgs, [{"content": "second"}], seq=2),
    ])
    monkeypatch.setenv("SWARM_CASSETTE_REPLAY_DIR", d)
    a, k = _args(msgs)
    r1 = pb.lookup("plan", "m", a, k)
    r2 = pb.lookup("plan", "m", a, k)
    r3 = pb.lookup("plan", "m", a, k)
    assert r1["chunks"][0]["content"] == "first"
    assert r2["chunks"][0]["content"] == "second", "同指纹重复按 seq FIFO 消费"
    assert r3 is None, "队列耗尽 → miss"


# ── miss 策略（★复核：默认 error/fail-loud，杜绝静默烧云端）──
def test_on_miss_error_is_default(tmp_path, monkeypatch):
    """★复核回归锁★ 默认=error：miss 立即抛，绝不静默直连云端（成本反转防线）。"""
    monkeypatch.setenv("SWARM_CASSETTE_REPLAY_DIR", str(tmp_path))
    with pytest.raises(pb.CassetteReplayMiss):
        pb.on_miss("plan", "m")


def test_on_miss_passthrough_opt_in(tmp_path, monkeypatch):
    """passthrough 需显式 opt-in：不抛，仅 WARNING。"""
    monkeypatch.setenv("SWARM_CASSETTE_REPLAY_DIR", str(tmp_path))
    monkeypatch.setenv("SWARM_CASSETTE_REPLAY_MISS", "passthrough")
    pb.on_miss("plan", "m")   # 不抛


# ── ★复核 HIGH★ 失败尝试录像不当成功回放 ──
def test_errored_record_skipped_for_success(tmp_path, monkeypatch):
    """录制中途 error 的 record（切备前失败尝试）跳过 → 取同指纹下一条成功 record。"""
    msgs = [HumanMessage(content="retry-me")]
    err = _rec(msgs, [{"content": "partial"}], seq=1)
    err["error"] = "TimeoutError: stream stalled"
    ok = _rec(msgs, [{"content": "good-result"}], seq=2)
    d = _write_cassette(tmp_path, [err, ok])
    monkeypatch.setenv("SWARM_CASSETTE_REPLAY_DIR", d)
    a, k = _args(msgs)
    rec = pb.lookup("plan", "m", a, k)
    assert rec is not None and rec["chunks"][0]["content"] == "good-result", \
        "失败尝试应跳过，回放成功那次"


def test_only_errored_record_is_miss(tmp_path, monkeypatch):
    """该指纹只有失败尝试 → 视作 miss（无可信成功回放）。"""
    msgs = [HumanMessage(content="only-fail")]
    err = _rec(msgs, [{"content": "x"}], seq=1)
    err["error"] = "boom"
    d = _write_cassette(tmp_path, [err])
    monkeypatch.setenv("SWARM_CASSETTE_REPLAY_DIR", d)
    a, k = _args(msgs)
    assert pb.lookup("plan", "m", a, k) is None


# ── ★复核★空 dir loud + record_degrade ──
def test_empty_dir_warns_and_degrades(tmp_path, monkeypatch):
    d = tmp_path / "empty"
    d.mkdir()
    monkeypatch.setenv("SWARM_CASSETTE_REPLAY_DIR", str(d))
    cats = []
    monkeypatch.setattr("swarm.infra.degrade.record_degrade", lambda c, *a, **k: cats.append(c))
    pb._ensure_index()
    assert any("replay_dir_empty" in c for c in cats), f"空 dir 必须 record_degrade: {cats}"


# ── 半截行/坏 jsonl 不炸（录制中途被 kill）──
def test_load_tolerates_broken_lines(tmp_path, monkeypatch):
    msgs = [HumanMessage(content="ok")]
    d = tmp_path / "cass"
    d.mkdir()
    with open(d / "llm-1.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps(_rec(msgs, [{"content": "good"}])) + "\n")
        f.write('{"broken": ')   # 半截行
    monkeypatch.setenv("SWARM_CASSETTE_REPLAY_DIR", str(d))
    a, k = _args(msgs)
    assert pb.lookup("plan", "m", a, k) is not None, "半截行跳过，合法记录仍可查"


# ── fail-open ──
def test_lookup_failopen_on_bad_args(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_CASSETTE_REPLAY_DIR", str(tmp_path))
    # args 无 messages → 指纹算不出/空 → miss（None），绝不抛
    assert pb.lookup("plan", "m", (), {}) is None
