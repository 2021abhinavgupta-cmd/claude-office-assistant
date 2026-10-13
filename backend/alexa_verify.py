"""Checks that a request to /api/alexa really comes from Amazon.

Amazon signs every skill request: the `SignatureCertChainUrl` header points to
a certificate chain on s3.amazonaws.com/echo.api/, and `Signature-256` (or the
older `Signature`) is the request body signed with that certificate's key.
We check, as Amazon's docs require:
  1. the URL is https://s3.amazonaws.com/echo.api/... (port 443),
  2. the leaf certificate is in date and is issued for echo-api.amazon.com,
  3. the chain leads to a trusted root (certifi's bundle),
  4. the signature over the raw body is valid.
The timestamp and skill id are checked by the route itself.

ALEXA_VERIFY_SIGNATURE=0 turns this off (only for debugging; anyone could then
post fake requests, so turn it back on).
"""
from __future__ import annotations

import base64
import logging
import os
import posixpath
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

_SAN = "echo-api.amazon.com"
_CHAIN_CACHE: dict = {}          # url -> (fetched_at, [certs])
_CHAIN_TTL = 6 * 3600
_STORE = None
_LOCK = threading.Lock()


def enabled() -> bool:
    return os.getenv("ALEXA_VERIFY_SIGNATURE", "1").strip().lower() not in ("0", "false", "off", "no")


def available() -> bool:
    try:
        from cryptography.x509.verification import PolicyBuilder, Store  # noqa: F401
        return True
    except Exception:
        return False


def _url_ok(url: str) -> bool:
    try:
        u = urlparse(url or "")
    except Exception:
        return False
    if (u.scheme or "").lower() != "https" or (u.hostname or "").lower() != "s3.amazonaws.com":
        return False
    if u.port not in (None, 443):
        return False
    path = posixpath.normpath(u.path or "")
    return path.startswith("/echo.api/")


def _trust_store():
    global _STORE
    if _STORE is not None:
        return _STORE
    import certifi
    from cryptography import x509
    from cryptography.x509.verification import Store
    import warnings
    roots = []
    marker = b"-----END CERTIFICATE-----"
    with warnings.catch_warnings(), open(certifi.where(), "rb") as f:
        warnings.simplefilter("ignore")
        for part in f.read().split(marker):
            if b"-----BEGIN CERTIFICATE-----" not in part:
                continue
            try:
                roots.append(x509.load_pem_x509_certificate(part.strip() + b"\n" + marker + b"\n"))
            except Exception:
                continue   # one odd root must not break the whole store
    _STORE = Store(roots)
    return _STORE


def _chain(url: str) -> list:
    from cryptography import x509
    with _LOCK:
        hit = _CHAIN_CACHE.get(url)
        if hit and time.time() - hit[0] < _CHAIN_TTL:
            return hit[1]
    r = requests.get(url, timeout=4)
    r.raise_for_status()
    certs = x509.load_pem_x509_certificates(r.content)
    if not certs:
        raise ValueError("empty certificate chain")
    with _LOCK:
        _CHAIN_CACHE[url] = (time.time(), certs)
    return certs


def _chain_ok(certs: list) -> tuple[bool, str]:
    from cryptography import x509
    from cryptography.x509.verification import PolicyBuilder
    leaf = certs[0]
    now = datetime.now(timezone.utc)
    if not (leaf.not_valid_before_utc <= now <= leaf.not_valid_after_utc):
        return False, "certificate expired or not yet valid"
    try:
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [n.lower() for n in san.get_values_for_type(x509.DNSName)]
    except Exception:
        names = []
    if _SAN not in names:
        return False, "certificate is not for echo-api.amazon.com"
    try:
        verifier = PolicyBuilder().store(_trust_store()).build_server_verifier(x509.DNSName(_SAN))
        verifier.verify(leaf, certs[1:])
    except Exception as e:
        return False, f"certificate chain not trusted: {e}"
    return True, ""


def _sig_ok(leaf, signature_b64: str, raw: bytes, sha256: bool) -> bool:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    try:
        leaf.public_key().verify(base64.b64decode(signature_b64), raw, padding.PKCS1v15(),
                                 hashes.SHA256() if sha256 else hashes.SHA1())
        return True
    except Exception:
        return False


def verify(headers, raw: bytes) -> tuple[bool, str]:
    """(True, '') when the request is genuinely from Alexa."""
    if not enabled():
        return True, "signature check disabled"
    if not available():
        return False, "cryptography>=43 is not installed, cannot check Amazon's signature"
    url = headers.get("SignatureCertChainUrl", "")
    sig256 = headers.get("Signature-256", "")
    sig = sig256 or headers.get("Signature", "")
    if not url or not sig:
        return False, "missing signature headers"
    if not _url_ok(url):
        return False, "certificate URL is not Amazon's"
    try:
        certs = _chain(url)
    except Exception as e:
        return False, f"could not load certificate chain: {e}"
    ok, why = _chain_ok(certs)
    if not ok:
        return False, why
    if not _sig_ok(certs[0], sig, raw, sha256=bool(sig256)):
        return False, "signature does not match the request body"
    return True, ""
