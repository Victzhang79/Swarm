"""第二批-3：模板指纹只认依赖/构建文件，业务源码变不重建模板。"""
import os
import tempfile
from unittest.mock import MagicMock

from swarm.worker.image_builder import _dependency_fingerprint, compute_project_fingerprint


def _mk_proj():
    d = tempfile.mkdtemp()
    with open(f"{d}/pom.xml", "w") as f:
        f.write("<project><dependencies></dependencies></project>")
    os.makedirs(f"{d}/src/main/java", exist_ok=True)
    with open(f"{d}/src/main/java/Foo.java", "w") as f:
        f.write("class Foo {}\n")
    return d


def test_business_file_change_keeps_fingerprint():
    """新增/改业务文件 → 依赖指纹不变（不该触发模板重建）。"""
    d = _mk_proj()
    fp1 = _dependency_fingerprint(d)
    # 新增业务文件
    with open(f"{d}/src/main/java/Bar.java", "w") as f:
        f.write("class Bar {}\n")
    # 改已有业务文件
    with open(f"{d}/src/main/java/Foo.java", "w") as f:
        f.write("class Foo { void x(){} }\n")
    fp2 = _dependency_fingerprint(d)
    assert fp1 == fp2, "业务文件变不应改变依赖指纹"


def test_dependency_change_changes_fingerprint():
    """改 pom.xml（依赖）→ 指纹变（该触发重建）。"""
    d = _mk_proj()
    fp1 = _dependency_fingerprint(d)
    with open(f"{d}/pom.xml", "w") as f:
        f.write("<project><dependencies><dep>new</dep></dependencies></project>")
    fp2 = _dependency_fingerprint(d)
    assert fp1 != fp2, "依赖文件变应改变指纹"


def test_compute_fingerprint_stable_across_business_change():
    """compute_project_fingerprint 整体：业务文件变保持稳定。"""
    d = _mk_proj()
    spec = MagicMock()
    spec.deps_hash.return_value = "abc123"
    fp1 = compute_project_fingerprint(spec, d)
    with open(f"{d}/src/main/java/New.java", "w") as f:
        f.write("class New {}\n")
    fp2 = compute_project_fingerprint(spec, d)
    assert fp1 == fp2
