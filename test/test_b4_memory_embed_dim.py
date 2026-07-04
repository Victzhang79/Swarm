"""B4（round22, P1）：记忆表 embed 维度硬编码 1024，换模型后无校验、静默失效。

根因：L5/L6 表 DDL 固定 vector(1024)；write_mistake/write_success 写入前不做维度校验（KB 侧
semantic_index 已有 EmbeddingDimensionMismatchError）。换 embedding 模型(768/1536)后新写入 PG
报错被上层吞成 persisted:False（交付已成功、记忆静默丢失）。

治本：写入前对齐 KB 做维度校验——不符即抛 EmbeddingDimensionMismatchError（fail-loud），
不静默丢。

行为测试：直接验证维度校验不变量 + 异常类型对齐 KB。
"""
from __future__ import annotations

import pytest

from swarm.knowledge.semantic_index import EmbeddingDimensionMismatchError
from swarm.memory.store import BGE_M3_DIMENSION, _validate_embed_dim


def test_correct_dim_passes():
    _validate_embed_dim([0.1] * BGE_M3_DIMENSION)  # 不抛即通过


def test_wrong_dim_raises_mismatch():
    with pytest.raises(EmbeddingDimensionMismatchError):
        _validate_embed_dim([0.1] * 768)  # 换成 768 维模型


def test_wrong_dim_1536_raises():
    with pytest.raises(EmbeddingDimensionMismatchError):
        _validate_embed_dim([0.1] * 1536)


def test_empty_embedding_raises():
    with pytest.raises(EmbeddingDimensionMismatchError):
        _validate_embed_dim([])


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
