"""P1-D 回归：fix 循环 no-progress 失败签名——连轮同一编译错→早停，杜绝烧满 900s。"""
from swarm.worker.executor import WorkerExecutor as W


def test_signature_stable_across_lineno_jitter():
    a = W._failure_signature({"build_output": "[ERROR] /ws/X.java:[3,5] cannot find symbol\n symbol: class Foo"})
    b = W._failure_signature({"build_output": "[ERROR] /ws/X.java:[9,1] cannot find symbol\n symbol: class Foo"})
    assert a and a == b  # 仅行列号变 → 同签名（无进展）


def test_signature_differs_on_real_change():
    a = W._failure_signature({"build_output": "[ERROR] cannot find symbol: class Foo"})
    b = W._failure_signature({"build_output": "[ERROR] cannot find symbol: class Bar"})
    assert a != b  # 改了符号 → 不同签名（有进展，重置）


def test_signature_empty_when_no_detail():
    assert W._failure_signature({}) == ""
    assert W._failure_signature({"build_output": ""}) == ""


def test_signature_ignores_maven_download_noise():
    a = W._failure_signature({"build_output": "[ERROR] boom\nProgress (1): 4/8 kB\nDownloading from public: x"})
    b = W._failure_signature({"build_output": "[ERROR] boom\nProgress (1): 7/8 kB\nDownloaded from public: y"})
    assert a == b  # 下载进度抖动不算进展
