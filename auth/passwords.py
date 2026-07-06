"""Password hashing (PBKDF2, no extra deps)."""

from __future__ import annotations

import hashlib
import secrets


def hash_password(password: str, *, iterations: int = 260_000) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iters, salt, hexd = encoded.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters))
        return secrets.compare_digest(digest.hex(), hexd)
    except (ValueError, TypeError):
        return False


def generate_api_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """API token 的 at-rest 哈希（F1，round28）。

    token 由 generate_api_token() 产 = secrets.token_urlsafe(32) = 256bit 高熵随机，
    暴力枚举不可行 → 用快速 SHA256（不需要 PBKDF2 那种抗爆破慢哈希，那是给低熵口令的）。
    存 hash 而非明文：DB 转储/泄露不再等同长期凭据泄露；服务端只在【铸造时】见明文一次。
    十六进制定长 64 字符，作 token_hash 列（UNIQUE）查找键。空 token → 空串（调用方须先判空）。
    """
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
