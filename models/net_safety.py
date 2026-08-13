"""出站 HTTP 安全判定（多消费者单一事实源，30 号文批3 L-2 立）。

当前只有一个谓词：is_local_or_private_host——「这个 URL 的 host 是否 localhost/私网」。
消费者：api/routers/config.py（GET /api/models 模型清单，P1-20）、models/prober.py
（探测四处 verify 判据，L-2）。与 api/notify.py:_ssrf_unsafe_reason 是【相反方向】的
判定（那个拦私网防 SSRF，这个认私网放宽 TLS）——消费契约不同，绝不合并
（纪律：复用单一事实源 ≠ 复用其消费契约）。★未来若想合并这两个函数，必须先回答
「放宽 TLS」与「拦截 SSRF」的后果是否同档——答案是否定（hunter 批3 复核钉）。

原生于 api/routers/config.py（P1-20），L-2 施治时迁到 models 层：prober（models）是第二个
消费者，models 反向 import api 是分层倒置；api 本就多、处依赖 models，正向成立。
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse


def is_local_or_private_host(url: str) -> bool:
    """判断 URL host 是否 localhost/私网（这些常用自签名证书，可跳过 TLS 校验）。

    公网 host → 返回 False → 强制校验 TLS，防对云端 provider 的 MITM。
    无法解析 → 保守 False（强校验，fail-closed）。
    注意：非 IP 字面的内网域名（如 corp 内网 DNS 名）也判 False——本谓词只认
    localhost/.local/IP 字面私网段，宁严勿宽。
    """
    try:
        host = (urlparse(url).hostname or "").strip().lower()
    except Exception:  # noqa: BLE001
        return False
    if not host:
        return False
    if host in ("localhost",) or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private
    except ValueError:
        return False  # 非 IP 的公网域名 → 强校验
