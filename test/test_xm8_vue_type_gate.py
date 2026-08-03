"""X-M8（27 号文 §3.2）：.vue 改动零类型闸治本。

根因：`_compile_files` 的 js_ts 触发集不含 .vue，且 tsc 解析不了 .vue SFC——
Vue 工程的 .vue 改动在 L1.2 类型面**零覆盖**（autofix 面 `_TS_EXTS` 早已含 .vue，
独缺类型闸，是"接线覆盖 ≠ 机制存在"的同族半接）。

治本：触发集补 .vue；有 .vue 时先试 `npx --no-install vue-tsc --noEmit`
（Vue 官方 tsc 封装，覆盖 .vue+.ts 超集，rc=0 即不再跑 tsc）；项目没装 vue-tsc /
基础设施瞬时错误 → WARNING（降级可观测，血规 3/10④）+ 退 tsc；
vue-tsc 非 infra 失败 → 与 tsc 同口径 fail-closed（A2）。

行为测试：只 patch `_run_check_split`/`_manifest_present`，
`_tool_missing`/`_is_infra_failure` 用**真身**（假探针窄于真断言会冤报/假绿）。
"""
from __future__ import annotations

import logging

from swarm.worker import l1_pipeline


class _Recorder:
    """按命令路由的 _run_check_split 替身：记录调用顺序，按关键字给结果。"""

    def __init__(self, results: dict[str, tuple[int, str, str]]):
        self.calls: list[str] = []
        self._results = results

    def __call__(self, shell_cmd: str, project_path: str, timeout: int = 60):
        self.calls.append(shell_cmd)
        for key, res in self._results.items():
            if key in shell_cmd:
                return res
        raise AssertionError(f"未登记的命令被调用: {shell_cmd}")

    def _install(self, monkeypatch, tmp_path):
        (tmp_path / "package.json").write_text("{}")
        monkeypatch.setattr(l1_pipeline, "_run_check_split", self)
        monkeypatch.setattr(l1_pipeline, "_manifest_present", lambda *a, **k: True)
        return self


_VUE_ERR = "src/App.vue(3,5): error TS2322: Type 'string' is not assignable to type 'number'."
_VUE_MISSING = "npm error could not determine executable to run"  # 真身 _tool_missing 命中


def test_vue_change_is_type_checked_by_vue_tsc(monkeypatch, tmp_path):
    """.vue 改动 → vue-tsc 被调用且 rc=0 即过；纯 tsc **不再跑**（vue-tsc 是超集）。

    突变「触发集删掉 .vue」/「vue-tsc 优选块整块删掉」→ 本条红（调用记录为空/只有 tsc）。"""
    rec = _Recorder({"vue-tsc": (0, "", ""), "tsc": (0, "", "")})._install(monkeypatch, tmp_path)
    ok, msg = l1_pipeline._compile_files(str(tmp_path), ["src/App.vue"])
    assert ok is True, msg
    assert any("vue-tsc" in c for c in rec.calls), f"vue-tsc 未被调用: {rec.calls}"
    assert not any("vue-tsc" not in c for c in rec.calls), f"vue-tsc 过了还跑 tsc: {rec.calls}"


def test_vue_tsc_type_error_fails_closed(monkeypatch, tmp_path):
    """vue-tsc 非 infra 失败 → False（与 A2 tsc 同口径 fail-closed），不再退 tsc。"""
    rec = _Recorder({"vue-tsc": (2, _VUE_ERR, ""), "tsc": (0, "", "")})._install(monkeypatch, tmp_path)
    ok, msg = l1_pipeline._compile_files(str(tmp_path), ["src/App.vue"])
    assert ok is False, f"vue-tsc 类型错误必须判不过, got {ok} {msg!r}"
    assert not any("vue-tsc" not in c for c in rec.calls), f"已 fail-closed 还跑 tsc: {rec.calls}"


def test_missing_vue_tsc_falls_back_to_tsc_with_warning(monkeypatch, tmp_path, caplog):
    """项目没装 vue-tsc（真身 _tool_missing 命中的真实输出形态）→ 退 tsc + WARNING。"""
    rec = _Recorder({"vue-tsc": (1, "", _VUE_MISSING), "tsc": (0, "", "")})._install(monkeypatch, tmp_path)
    with caplog.at_level(logging.WARNING):
        ok, msg = l1_pipeline._compile_files(str(tmp_path), ["src/App.vue", "src/main.ts"])
    assert ok is True, msg
    assert any("vue-tsc" in c for c in rec.calls) and any("tsc" in c and "vue-tsc" not in c for c in rec.calls), \
        f"缺 vue-tsc 应退纯 tsc: {rec.calls}"
    assert any("X-M8" in r.message and "无类型覆盖" in r.message for r in caplog.records), \
        f"降级必须 WARNING 可观测: {[r.message for r in caplog.records]}"


def test_missing_vue_tsc_and_tsc_error_still_fails_closed(monkeypatch, tmp_path, caplog):
    """退 tsc 后 tsc 自身非 infra 失败 → 仍 fail-closed（降级不放宽闸门）。"""
    rec = _Recorder({"vue-tsc": (1, "", _VUE_MISSING), "tsc": (2, _VUE_ERR, "")})._install(monkeypatch, tmp_path)
    with caplog.at_level(logging.WARNING):
        ok, _ = l1_pipeline._compile_files(str(tmp_path), ["src/App.vue"])
    assert ok is False


def test_ts_only_files_do_not_invoke_vue_tsc(monkeypatch, tmp_path):
    """无 .vue 的纯 ts 改动 → 不碰 vue-tsc（回归锁：触发集改动没扰动原路径）。"""
    rec = _Recorder({"tsc": (0, "", "")})._install(monkeypatch, tmp_path)
    ok, msg = l1_pipeline._compile_files(str(tmp_path), ["src/app.ts"])
    assert ok is True, msg
    assert not any("vue-tsc" in c for c in rec.calls), f"纯 ts 不该调 vue-tsc: {rec.calls}"
