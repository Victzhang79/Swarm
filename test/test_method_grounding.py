#!/usr/bin/env python3
"""P5：臆造【方法】javap 接地（残留 ③ 缺口，996db614 实测 18×900s 主因之一）。

模型在真实存在的类上调不存在的方法（java.util.Base64.Encoder.encodeToByte，真方法
encodeToString/encode）→ symbol-repair 近邻接不住(无项目近邻)、codegraph 跳过 method →
worker 反复臆造烧满 900s。治本：javap 取类真实方法集喂模型。本套测纯解析/组装函数。
"""
from __future__ import annotations

from swarm.worker.symbol_resolver import (
    build_method_grounding,
    parse_javap_methods,
    parse_missing_methods,
    to_javap_class_name,
)


# ── parse_missing_methods：从 javac 输出抽 (方法, 所属类) ──

def test_parse_method_location_class():
    out = (
        "[ERROR] /workspace/ruoyi-common/.../Sha512Utils.java:[42,39] cannot find symbol\n"
        "  symbol:   method encodeToByte(byte[])\n"
        "  location: class java.util.Base64.Encoder\n"
    )
    assert parse_missing_methods(out) == [("encodeToByte", "java.util.Base64.Encoder")]


def test_parse_method_location_variable_of_type():
    out = (
        "X.java:[10,5] cannot find symbol\n"
        "  symbol:   method getFoo()\n"
        "  location: variable bar of type com.ruoyi.alarm.domain.Alarm\n"
    )
    assert parse_missing_methods(out) == [("getFoo", "com.ruoyi.alarm.domain.Alarm")]


def test_parse_method_dedup_and_ansi():
    out = (
        "\x1b[1;31m  symbol:   method isEmtpy()\x1b[m\n  location: class java.lang.String\n"
        "  symbol:   method isEmtpy()\n  location: class java.lang.String\n"
    )
    assert parse_missing_methods(out) == [("isEmtpy", "java.lang.String")]


def test_parse_no_method_errors():
    assert parse_missing_methods("symbol: class Foo\n") == []
    assert parse_missing_methods("") == []


# ── to_javap_class_name：点分嵌套 → 二进制 $ 名 ──

def test_javap_name_nested():
    assert to_javap_class_name("java.util.Base64.Encoder") == "java.util.Base64$Encoder"


def test_javap_name_toplevel():
    assert to_javap_class_name("com.ruoyi.alarm.domain.Alarm") == "com.ruoyi.alarm.domain.Alarm"
    assert to_javap_class_name("java.lang.String") == "java.lang.String"


def test_javap_name_double_nested():
    assert to_javap_class_name("com.x.Outer.Mid.Inner") == "com.x.Outer$Mid$Inner"


# ── parse_javap_methods：从 javap 输出抽方法名 ──

def test_parse_javap_methods():
    javap = (
        'Compiled from "Base64.java"\n'
        "public static class java.util.Base64$Encoder {\n"
        "  public byte[] encode(byte[]);\n"
        "  public int encode(byte[], byte[]);\n"
        "  public java.lang.String encodeToString(byte[]);\n"
        "  public java.util.Base64$Encoder withoutPadding();\n"
        "}\n"
    )
    methods = parse_javap_methods(javap)
    assert "encode" in methods and "encodeToString" in methods and "withoutPadding" in methods
    assert "encodeToByte" not in methods  # 臆造的不在真实方法集
    # 去重：encode 出现两次只算一次
    assert methods.count("encode") == 1


def test_parse_javap_empty():
    assert parse_javap_methods("") == []
    assert parse_javap_methods("no methods here") == []


# ── build_method_grounding：渲染提示 ──

def test_build_method_grounding():
    hint = build_method_grounding([
        ("encodeToByte", "java.util.Base64.Encoder", ["encode", "encodeToString", "withoutPadding"]),
    ])
    assert "encodeToByte" in hint and "没有方法" in hint
    assert "encodeToString" in hint  # 真实方法被列出供模型选
    assert "java.util.Base64.Encoder" in hint


def test_build_method_grounding_skips_empty():
    # 没查到真实方法（javap 失败）→ 不产生误导提示
    assert build_method_grounding([("foo", "com.X", [])]) == ""
    assert build_method_grounding([]) == ""


if __name__ == "__main__":
    import sys
    fails = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            try:
                v()
            except Exception as e:  # noqa: BLE001
                import traceback
                print(f"  ❌ {k}: {e}")
                traceback.print_exc()
                fails += 1
    print("OK" if not fails else f"{fails} FAILED")
    sys.exit(1 if fails else 0)
