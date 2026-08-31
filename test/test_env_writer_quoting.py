"""#28（round65e12 踩坑）：env-writer 写复杂 JSON 值【无引号】→ restart-api.sh `source .env`
报 command-not-found(127) 起不来。PUT /api/model-providers 写 SWARM_MODEL_PROVIDERS/MODEL_PROVIDERS/
MODEL_SIZES 是 JSON([{...}]/{...})，裸拼进 .env → bash source 无法解析。

治：_env_quote 对含 bash 特殊字符的值单引号包裹（pydantic 读单引号 JSON 正常；单引号内无插值最安全），
值内单引号用 '\'' 转义。已安全的简单值不加引号（老行为不变）。
"""
from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path

from swarm.api.routers.config import _env_quote


def _bash_source_ok(line: str) -> bool:
    """真拿 bash source 验一行 .env 能否被解析（复现 restart-api.sh 的 `set -a; source .env`）。"""
    r = subprocess.run(["bash", "-c", f"set -a; {line}"], capture_output=True, text=True)
    return r.returncode == 0


# ── 核心：JSON 值必须被引号包裹且 bash source 通过 ──
def test_json_array_quoted_and_sourceable():
    v = '[{"id":"kimi-code","base_url":"https://api.kimi.com/coding/v1"}]'
    q = _env_quote(v)
    assert q.startswith("'") and q.endswith("'"), f"JSON 值须单引号包裹: {q}"
    assert _bash_source_ok(f"SWARM_MODEL_PROVIDERS={q}"), "bash source 必须通过（治 127）"


def test_json_object_quoted_and_sourceable():
    v = '{"k3":"kimi-code","REMOTE_BRAIN_PRIMARY":"siliconflow"}'
    q = _env_quote(v)
    assert _bash_source_ok(f"SWARM_MODEL_MODEL_PROVIDERS={q}")


def test_value_with_spaces_quoted():
    q = _env_quote("a b c")
    assert _bash_source_ok(f"K={q}")


# ── 不回归：简单值不加引号 ──
def test_simple_value_unquoted():
    assert _env_quote("k3") == "k3"
    assert _env_quote("https://api.kimi.com/coding/v1") == "https://api.kimi.com/coding/v1"
    assert _env_quote("") == ""


def test_simple_value_still_sourceable():
    assert _bash_source_ok(f"SWARM_MODEL_BRAIN_PRIMARY={_env_quote('k3')}")


# ── 值内含单引号：双引号转义，bash + python-dotenv 都读得对（复核 CRITICAL）──
def test_value_with_single_quote_bash_ok():
    q = _env_quote("it's a test")
    assert _bash_source_ok(f"K={q}"), f"含单引号的值也须 bash source 通过: {q}"


def test_value_with_single_quote_dotenv_ok(tmp_path):
    """★复核 CRITICAL 回归锁★ 含单引号的值经 python-dotenv 读回原值（不静默回退默认）。"""
    from dotenv import dotenv_values
    v = "it's a test"
    p = tmp_path / ".env"
    p.write_text(f"K={_env_quote(v)}\n", encoding="utf-8")
    assert dotenv_values(str(p)).get("K") == v, "python-dotenv 须读回原值（不丢行）"


# ── pydantic 仍读得对（引号不污染值）——round-trip ──
def test_pydantic_reads_quoted_json(monkeypatch, tmp_path):
    import json as _json
    val = [{"id": "kimi-code", "kind": "cloud", "base_url": "https://x/v1", "api_key": ""}]
    q = _env_quote(_json.dumps(val, ensure_ascii=False))
    # 模拟 .env 行被 source 进环境后 pydantic 读到的值（bash 去掉外层单引号）
    r = subprocess.run(
        ["bash", "-c", f"set -a; SWARM_TEST_PROVIDERS={q}; python3 -c 'import os,json;print(json.loads(os.environ[\"SWARM_TEST_PROVIDERS\"])[0][\"id\"])'"],
        capture_output=True, text=True, cwd=str(tmp_path))
    assert r.returncode == 0 and r.stdout.strip() == "kimi-code", f"pydantic/json 须读回原值: {r.stdout} {r.stderr}"


def test_run_with_dotenv_treats_values_as_literal_data(tmp_path):
    marker = tmp_path / "must-not-exist"
    value = f"it's $HOME `touch {marker}` $(touch {marker})"
    env_file = tmp_path / ".env"
    env_file.write_text(f"K={_env_quote(value)}\n", encoding="utf-8")
    helper = Path(__file__).resolve().parents[1] / "scripts" / "run_with_dotenv.py"
    proc = subprocess.run(
        [sys.executable, str(helper), "--env-file", str(env_file), "--",
         sys.executable, "-c", "import os; print(os.environ['K'])"],
        capture_output=True, text=True, check=True,
    )
    assert proc.stdout.strip() == value
    assert not marker.exists(), "dotenv 值不得被 shell 扩展或当作命令执行"


def test_run_with_dotenv_does_not_expand_other_secret_keys(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "SWARM_SECRET_KEY=supersecret\nSWARM_APP_NAME='${SWARM_SECRET_KEY}'\n",
        encoding="utf-8",
    )
    helper = Path(__file__).resolve().parents[1] / "scripts" / "run_with_dotenv.py"
    proc = subprocess.run(
        [sys.executable, str(helper), "--env-file", str(env_file), "--",
         sys.executable, "-c", "import os; print(os.environ['SWARM_APP_NAME'])"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip() == "${SWARM_SECRET_KEY}"


def _read_api_port(env_file: Path, env: dict[str, str] | None = None) -> str:
    helper = Path(__file__).resolve().parents[1] / "scripts" / "api_port.py"
    proc = subprocess.run(
        [sys.executable, str(helper), str(env_file)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def test_api_port_uses_registered_fallback(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("SWARM_API_PORT=18420\n", encoding="utf-8")
    clean_env = {k: v for k, v in os.environ.items()
                 if k not in {"SWARM_PORT", "SWARM_API_PORT"}}
    assert _read_api_port(env_file, clean_env) == "18420"


def test_api_port_matches_loaded_env_precedence(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("SWARM_PORT=18420\nSWARM_API_PORT=19420\n", encoding="utf-8")
    env = dict(os.environ)
    env.pop("SWARM_PORT", None)
    env["SWARM_API_PORT"] = "20420"
    # load_dotenv 后 app 仍优先 SWARM_PORT，所以文件 SWARM_PORT 胜进程 SWARM_API_PORT。
    assert _read_api_port(env_file, env) == "18420"
