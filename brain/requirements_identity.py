"""需求条目身份与引文字符归一化。"""

from __future__ import annotations

import hashlib
import unicodedata


def normalize_for_id(text: str) -> str:
    """去空白和 Unicode 标点并折叠大小写，生成稳定内容身份。"""
    normalized: list[str] = []
    for char in str(text):
        if char.isspace() or unicodedata.category(char).startswith("P"):
            continue
        normalized.append(char.casefold())
    return "".join(normalized)


def requirement_id(text: str) -> str:
    """稳定条目 ID；抽取顺序变化不改变身份。"""
    digest = hashlib.sha1(normalize_for_id(text).encode("utf-8")).hexdigest()
    return f"req-{digest[:8]}"


_PUNCT_FOLD_TABLE = str.maketrans({
    "，": ",", "。": ".", "．": ".", "、": ",",
    "：": ":", "；": ";", "！": "!", "？": "?",
    "（": "(", "）": ")", "【": "[", "】": "]",
    "《": "<", "》": ">", "－": "-", "～": "~",
    "“": '"', "”": '"', "「": '"', "」": '"', "『": '"', "』": '"',
    "‘": "'", "’": "'",
})


def fold_for_quote_match(text: str) -> str:
    """去空白并折叠全半角同义标点，字符内容仍保持严格。"""
    return "".join(
        char for char in str(text).translate(_PUNCT_FOLD_TABLE)
        if not char.isspace()
    )


__all__ = ["fold_for_quote_match", "normalize_for_id", "requirement_id"]
