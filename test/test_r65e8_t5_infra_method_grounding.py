"""R65E8-T5（round65e8 st-56 方法级幻觉·grounding 根治）：infra 符号 grounding 从【类 FQN】
升到【类 FQN + public 方法签名】。

死因：既有 _detect_infra_symbols 只钉【类级】——告诉 worker "CacheUtils 真实存在、FQN=…"，
但 worker 不知其**方法签名**（get/put/remove），凭训练惯性调 `.set/.get`（裸 RedisTemplate 签名）
→ cannot find symbol 死循环。class 级 grounding 挡不住 method 级幻觉。

治本（确定性、不依赖 reranker 天花板）：确定性解析每个 infra 类的 public 方法签名，钉进栈画像，
喂 design+worker。worker 直接看到 `static get(String cacheName, String key)` → 方法级幻觉当场死。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from swarm.brain.stack_detect import (
    _detect_infra_symbols,
    _extract_public_method_sigs,
    format_stack_for_prompt,
)

# 仿真实 RuoYi CacheUtils（Allman 花括号换行 + javadoc + private 噪声）。
_CACHEUTILS = """package com.ruoyi.common.utils;

import org.apache.shiro.cache.Cache;

public class CacheUtils
{
    private static CacheManager cacheManager = ...;
    public static final String SYS_CACHE = "sys-cache";

    /** 获取缓存 */
    public static Object get(String key)
    {
        return get(SYS_CACHE, key);
    }

    public static Object get(String cacheName, String key)
    {
        return getCache(cacheName).get(getKey(key));
    }

    public static void put(String cacheName, String key, Object value)
    {
        getCache(cacheName).put(getKey(key), value);
    }

    public static void remove(String cacheName, String key)
    {
        getCache(cacheName).remove(getKey(key));
    }

    // private 不该出现
    private static Cache<String, Object> getCache(String cacheName)
    {
        return cacheManager.getCache(cacheName);
    }
}
"""


def test_extract_static_method_sigs_from_allman_class():
    sigs = _extract_public_method_sigs(_CACHEUTILS)
    joined = " | ".join(sigs)
    assert "static get(String key)" in sigs, f"缺 get(String key): {sigs}"
    assert "static get(String cacheName, String key)" in sigs
    assert "static put(String cacheName, String key, Object value)" in sigs
    assert "static remove(String cacheName, String key)" in sigs
    # private 方法绝不出现
    assert "getCache" not in joined, "private 方法泄漏"
    # 字段/常量不是方法
    assert "SYS_CACHE" not in joined


def test_extract_excludes_constructors_and_fields():
    body = """package p;
    public class Foo {
        public Foo() { }
        public Foo(int x) { this.x = x; }
        public int value = 3;
        public String name() { return n; }
    }"""
    sigs = _extract_public_method_sigs(body)
    assert sigs == ["name()"], f"构造器/字段应被排除，只留 name(): {sigs}"


def test_extract_instance_method_no_static_prefix():
    body = """package p;
    public class RedisCache {
        public <T> void setCacheObject(String key, T value) { }
        public <T> T getCacheObject(String key) { return null; }
        public boolean deleteObject(String key) { return true; }
    }"""
    sigs = _extract_public_method_sigs(body)
    assert "setCacheObject(String key, T value)" in sigs
    assert "getCacheObject(String key)" in sigs
    assert "deleteObject(String key)" in sigs
    # 实例方法无 static 前缀
    assert not any(s.startswith("static ") for s in sigs)


def test_extract_caps_and_truncates():
    # 20 个方法 → 应封顶；超长参数 → 截断
    methods = "\n".join(
        f"    public void m{i}(String aVeryLongParameterNameThatGoesOnAndOn{i}) {{ }}"
        for i in range(20))
    body = f"package p;\npublic class Big {{\n{methods}\n}}"
    sigs = _extract_public_method_sigs(body)
    assert len(sigs) <= 12, f"每类方法数应封顶（防 prefill 爆炸）: {len(sigs)}"


def test_extract_non_java_or_empty_no_crash():
    assert _extract_public_method_sigs("") == []
    assert _extract_public_method_sigs("just some text no methods") == []


# ── ★复核 F1 CONFIRMED HIGH 回归锁★ 注释/字符串里的伪 public 方法绝不当真签名 ──
def test_no_phantom_from_javadoc_example():
    body = """package p;
    /**
     * Example usage:
     * public void foo(String x) {
     *     doSomething();
     * }
     */
    public class Foo {
        public static Object get(String cacheName, String key) { return null; }
    }"""
    sigs = _extract_public_method_sigs(body)
    assert "static get(String cacheName, String key)" in sigs
    assert not any("foo" in s for s in sigs), f"javadoc 示例里的伪方法泄漏: {sigs}"


def test_no_phantom_from_string_literal_template():
    body = '''package p;
    public class Gen {
        public static String TEMPLATE = "public void genMethod(String x) { return; }";
        public void realMethod(String y) { doThing(); }
    }'''
    sigs = _extract_public_method_sigs(body)
    assert "realMethod(String y)" in sigs
    assert not any("genMethod" in s for s in sigs), f"字符串模板里的伪方法泄漏: {sigs}"


def test_no_phantom_from_line_comment():
    body = """package p;
    public class Foo {
        // public void oldMethod(String x) { legacy(); }
        public void realMethod(String y) { doThing(); }
    }"""
    sigs = _extract_public_method_sigs(body)
    assert "realMethod(String y)" in sigs
    assert not any("oldMethod" in s for s in sigs), f"行注释里的伪方法泄漏: {sigs}"


def test_detect_infra_symbols_returns_method_sigs():
    """集成：_detect_infra_symbols 应同时返回类 FQN 与其方法签名。"""
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "CacheUtils.java"
        p.write_text(_CACHEUTILS, encoding="utf-8")
        by_concept, methods = _detect_infra_symbols([str(p)])
        assert any("CacheUtils" in fqn for fqns in by_concept.values() for fqn in fqns)
        fqn = "com.ruoyi.common.utils.CacheUtils"
        assert fqn in methods, f"应含方法签名映射: {list(methods)}"
        assert any("get(String cacheName, String key)" in s for s in methods[fqn])


def test_format_stack_renders_method_sigs():
    """format_stack_for_prompt 应把方法签名渲染进 worker/design 提示。"""
    profile = {
        "infra_symbols": {"缓存": ["com.ruoyi.common.utils.CacheUtils"]},
        "infra_symbol_methods": {
            "com.ruoyi.common.utils.CacheUtils": [
                "static get(String cacheName, String key)",
                "static put(String cacheName, String key, Object value)",
            ]},
    }
    rendered = format_stack_for_prompt(profile)
    assert "get(String cacheName, String key)" in rendered, "方法签名未进提示"
    assert "put(String cacheName, String key, Object value)" in rendered


def test_format_stack_no_methods_still_renders_fqn():
    """向后兼容：只有类 FQN、无方法签名（旧画像/schema 升级前）→ 照常渲染 FQN，不崩。"""
    profile = {"infra_symbols": {"缓存": ["com.x.CacheUtils"]}}
    rendered = format_stack_for_prompt(profile)
    assert "CacheUtils" in rendered
