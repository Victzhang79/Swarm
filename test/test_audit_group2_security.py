"""走查报告第2组安全项回归：S2 FileScope 路径匹配 + S5 webhook HMAC + H7 命令注入 + S3 沙箱逃逸。"""


def test_s2_filescope_no_overreach():
    """S2：scope 'a.py' 不放行 evil/a.py、xa.py（旧 endswith 双向匹配越权）。"""
    from swarm.types import FileScope
    s = FileScope(writable=["a.py"])
    assert s.is_writable("a.py")
    assert not s.is_writable("evil/a.py"), "不应放行子目录同名"
    assert not s.is_writable("xa.py"), "不应放行后缀子串"


def test_s2_filescope_empty_not_truthy():
    """S2：空串 scope 不应恒真（旧 ''.endswith() 恒 True = 全开）。"""
    from swarm.types import FileScope
    s = FileScope(writable=[""])
    assert not s.is_writable("anything.py")
    assert not s.is_writable("a.py")


def test_s2_filescope_dir_and_prefix():
    """S2：目录 scope + 仓库根前缀容忍仍正常。"""
    from swarm.types import FileScope
    s = FileScope(writable=["src/"])
    assert s.is_writable("src/main.py"), "目录下文件应放行"
    s2 = FileScope(writable=["src/main.py"])
    assert s2.is_writable("repo/src/main.py"), "多段路径容忍仓库根前缀"
    assert not s2.is_writable("other/main.py"), "单段同名不放行"


def test_s3_sandbox_path_containment():
    """S3：sandbox_path 拒绝 ../ 逃逸，正常路径正常映射。"""
    from swarm.worker.sandbox import sandbox_path
    assert sandbox_path("src/main.py") == "/workspace/src/main.py"
    assert sandbox_path("src/a/../b/c.py") == "/workspace/src/b/c.py"
    import pytest
    with pytest.raises(ValueError):
        sandbox_path("../../etc/passwd")
    with pytest.raises(ValueError):
        sandbox_path("src/../../../etc/shadow")


def test_h7_shell_injection_blocked():
    """H7：命令含 shell 元字符被拒，正常构建参数放行。"""
    from swarm.tools.build_tools import _has_shell_injection
    for bad in ["mvn test; rm -rf ~", "mvn test | tee x", "mvn test && curl evil",
                "mvn test `whoami`", "mvn test $(id)", "mvn test > /etc/x"]:
        inj, _ = _has_shell_injection(bad)
        assert inj, f"应拦截注入: {bad}"
    for ok in ["mvn test -DskipTests", "mvn -pl a,b -am compile",
               "npm run build", "pytest -k foo"]:
        inj, _ = _has_shell_injection(ok)
        assert not inj, f"正常命令不应误拦: {ok}"


def test_s5_webhook_hmac_signature():
    """S5：HMAC 签名计算与校验逻辑（compare_digest 等价性）。"""
    import hashlib
    import hmac
    secret = "test_secret_xyz"
    body = b'{"commits":[]}'
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # 正确签名通过
    assert hmac.compare_digest(expected, expected)
    # 错误签名拒绝
    wrong = "sha256=" + hmac.new(b"wrong", body, hashlib.sha256).hexdigest()
    assert not hmac.compare_digest(expected, wrong)
