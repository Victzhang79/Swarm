"""W2.3 — tree-sitter 多语言切分回归测试。

验证:
- Java 按方法切（不是一个巨块）
- Go / TS 也能按函数/方法切
- grammar 不可用 → 优雅回退字符切兜底（不抛、不丢内容）
- Python 仍走原 AST/缩进路径（未回归）
"""

from __future__ import annotations

from unittest.mock import patch

from swarm.knowledge.semantic_index import SemanticIndexer

JAVA_SRC = """package com.example.service;

import java.util.List;

public class UserService {

    public User findById(Long id) {
        return userMapper.selectById(id);
    }

    public List<User> listAll(int pageNum, int pageSize) {
        PageHelper.startPage(pageNum, pageSize);
        return userMapper.selectAll();
    }

    private void audit(String action) {
        log.info("audit: {}", action);
    }
}
"""

GO_SRC = """package main

import "fmt"

func Add(a, b int) int {
    return a + b
}

func Greet(name string) string {
    return fmt.Sprintf("hi %s", name)
}
"""

TS_SRC = """export class Calc {
    add(a: number, b: number): number {
        return a + b;
    }
    sub(a: number, b: number): number {
        return a - b;
    }
}

export function topLevel(x: number): number {
    return x * 2;
}
"""


def test_java_chunks_by_method_not_one_block():
    """Java 文件按方法切分，至少 3 个方法 chunk，且不是一个巨块。"""
    chunks = SemanticIndexer.chunk_source_code(JAVA_SRC, "svc/UserService.java")
    assert len(chunks) >= 3, f"Java 应按方法切，实际 {len(chunks)} 块"
    method_chunks = [c for c in chunks if c.chunk_type == "method"]
    assert len(method_chunks) >= 3, [c.chunk_type for c in chunks]
    # 各方法独立、且 class_name 接地
    contents = "\n---\n".join(c.content for c in method_chunks)
    assert "findById" in contents
    assert "listAll" in contents
    assert "audit" in contents
    # 不应把整个类塞进单个 method chunk
    for c in method_chunks:
        assert not (("findById" in c.content) and ("audit" in c.content)), "方法没拆开"
    # tree-sitter 元信息标记
    assert any(c.metadata.get("chunker") == "tree-sitter" for c in chunks)


def test_go_chunks_by_function():
    chunks = SemanticIndexer.chunk_source_code(GO_SRC, "main.go")
    fn = [c for c in chunks if "func " in c.content]
    assert len(fn) >= 2, [c.content for c in chunks]
    joined = "\n".join(c.content for c in chunks)
    assert "Add" in joined and "Greet" in joined


def test_ts_chunks_by_method_and_function():
    chunks = SemanticIndexer.chunk_source_code(TS_SRC, "calc.ts")
    joined = "\n".join(c.content for c in chunks)
    assert "add" in joined and "sub" in joined and "topLevel" in joined
    # 类方法应被拆开（不与 topLevel 混在一块）
    assert len(chunks) >= 3, [c.chunk_type for c in chunks]


def test_missing_grammar_graceful_fallback():
    """grammar 加载失败 → _get_ts_parser 返回 None → 回退字符切兜底，不抛、不丢内容。"""
    with patch("swarm.knowledge.semantic_index._get_ts_parser", return_value=None):
        chunks = SemanticIndexer.chunk_source_code(JAVA_SRC, "svc/UserService.java")
    # 兜底路径仍产出 chunk（free_text/字符切），且内容未丢
    assert len(chunks) >= 1
    joined = "\n".join(c.content for c in chunks)
    assert "findById" in joined
    # 兜底不会打 tree-sitter 标记
    assert all(c.metadata.get("chunker") != "tree-sitter" for c in chunks)


def test_python_still_uses_ast_path():
    """Python 文件不走 tree-sitter（无 chunker=tree-sitter 标记），按原 def/class 切。"""
    py = "class A:\n    def m(self):\n        return 1\n\ndef top():\n    return 2\n"
    chunks = SemanticIndexer.chunk_source_code(py, "mod.py")
    assert len(chunks) >= 1
    assert all(c.metadata.get("chunker") != "tree-sitter" for c in chunks)
    joined = "\n".join(c.content for c in chunks)
    assert "def m" in joined and "def top" in joined


def test_unknown_language_fallback():
    """未知扩展名（.rb）走原兜底路径，不抛。"""
    chunks = SemanticIndexer.chunk_source_code("puts 'hi'\n", "x.rb")
    assert isinstance(chunks, list)
