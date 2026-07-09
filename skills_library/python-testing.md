---
id: python-testing
title: Python 测试（pytest·fixture·TDD）
applies_to_stacks: ["python"]
applies_to_intents: ["create", "debug"]
applies_to_phases: ["code"]
target: ["worker"]
priority: 48
max_chars: 1800
tags: ["python", "testing", "pytest"]
---

# Python 测试要点（pytest / TDD）

## TDD 循环（先红后绿）
- RED：先写一个必然失败的测试，跑一次确认它 fail（拿到退出码证据），再写实现。
- GREEN：写刚好让测试通过的最小实现。
- REFACTOR：保持全绿的前提下重构。
- 覆盖目标 80%+，关键路径 100%；`pytest --cov=<pkg> --cov-report=term-missing`。

## 断言与异常
- 用裸 `assert`，一测一行为，测名可读：`test_login_with_bad_password_fails`。
- 异常用 `with pytest.raises(ValueError, match="..."):`，别在测试里 try/except 吞异常。
- 用 `exc_info = pytest.raises(...)` 后 `exc_info.value.code` 校验异常属性。

## Fixture
- `@pytest.fixture` + `yield` 做 setup/teardown；scope 可选 function/module/session。
- 共享 fixture 放 `conftest.py`；`autouse=True` 每测自动跑（如重置全局配置）。
- 临时文件用内置 `tmp_path`（自动清理），别手写删除逻辑。

## 参数化
```python
@pytest.mark.parametrize("inp,exp", [
    ("a", True), ("", False),
], ids=["ok", "empty"])
def test_x(inp, exp): assert f(inp) is exp
```

## Mock（隔离外部依赖）
- `@patch("pkg.api_call")`；`.return_value` 设返回，`.side_effect=Err()` 造异常。
- `autospec=True` 防调用签名漂移；`assert_called_once_with(...)` 校验调用。
- 异步：`@pytest.mark.asyncio` + `assert_awaited_once()`。

## 纪律
- 测行为不测实现细节；测边界（空/None/越界）。
- 测试间零共享状态，彼此独立；慢测打 `@pytest.mark.slow`，`pytest -m "not slow"` 跳过。
- 常用：`-x`(首败停) `--lf`(只跑上次失败) `-k <pattern>`。
