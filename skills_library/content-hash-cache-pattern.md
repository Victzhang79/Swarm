---
id: content-hash-cache-pattern
title: 内容哈希缓存模式（SHA-256）
description: "当你在为 PDF 解析、OCR、文本抽取等昂贵处理加缓存、想用文件内容 SHA-256 而非路径当缓存键时调用，返回分块哈希、按哈希存 JSON 与损坏优雅降级的实现模板。"
applies_to_stacks: ["*"]
applies_to_intents: ["create", "modify"]
applies_to_phases: ["code"]
target: ["worker"]
priority: 40
max_chars: 1800
tags: ["caching", "pattern", "performance"]
---

# 内容哈希缓存模式（SHA-256）

用文件内容的 SHA-256 当缓存键（而非路径）缓存昂贵处理结果（PDF 解析、OCR、文本抽取）。文件改名/移动仍命中；内容变化自动失效；无需索引文件。

## 1. 内容哈希作键（大文件分块）
```python
def compute_file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
```
分块读避免整文件进内存。

## 2. 缓存项用不可变结构
```python
@dataclass(frozen=True, slots=True)
class CacheEntry:
    file_hash: str
    source_path: str
    document: Any
```

## 3. 文件存储：`{hash}.json`
- 每项存 `{hash}.json`，按哈希 O(1) 查，无需索引文件。
- 读缓存时 `JSONDecodeError/ValueError/KeyError` 一律当未命中返回 `None`——损坏优雅降级、下次重算，绝不崩。
- `cache_dir.mkdir(parents=True, exist_ok=True)` 首写时惰性建目录。

## 4. 服务层包装（保持处理函数纯净）
处理函数不感知缓存；缓存逻辑单独一层：
```python
def extract_with_cache(path, *, cache_enabled=True, cache_dir=Path(".cache")):
    if not cache_enabled:
        return extract(path)          # 纯函数
    h = compute_file_hash(path)
    if (hit := read_cache(cache_dir, h)):
        return hit.document
    doc = extract(path)
    write_cache(cache_dir, CacheEntry(h, str(path), doc))
    return doc
```

## 要点 / 反模式
- 哈希内容，别哈希路径（路径会变，内容身份不变）。
- 处理函数保持纯净，别把 `cache_enabled` 塞进去（违反单一职责）。
- 日志打截断哈希 `h[:12]` 便于排查。
- 嵌套 frozen dataclass 别用 `asdict()`，手写序列化更可控。

## 何时不用
- 必须实时新鲜的数据；结果依赖内容以外的参数（如不同抽取配置需并入键）；单项过大（改用流式）。
