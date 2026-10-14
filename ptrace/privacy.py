"""Credential redaction and release-file scanning."""
import re
from pathlib import Path

SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----[\s\S]*?-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)(?:bearer\s+)[A-Za-z0-9_.+/=-]{20,}"),
    re.compile(r"(?i)(?:[\"']?(?:api[_-]?key|access[_-]?token|secret[_-]?key)[\"']?\s*[=:]\s*[\"']?)[A-Za-z0-9_-]{24,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
LOCAL = re.compile(r"(?i)(?:(?<![A-Za-z0-9])[A-Z]:[\\/](?:[^\s\"'<>]+)|/(?:Users|home|root|workspace)/[^\s\"'<>]+)")
PRIVATE_URL = re.compile(r"https?://[^\s<>\"']*(?:api_key|access_token|signature|sig|token)=[^\s<>\"']+", re.I)


def redact_text(text):
    for pat in SECRET_PATTERNS:
        text = pat.sub("[REDACTED_SECRET]", text)
    text = PRIVATE_URL.sub("[REDACTED_SIGNED_URL]", text)
    text = EMAIL.sub("[REDACTED_EMAIL]", text)
    return LOCAL.sub("[REDACTED_LOCAL_PATH]", text)


def sanitize(obj):
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            key = str(k)
            normalized = key.lower().replace("-", "_")
            sensitive = normalized in ("api_key", "access_token", "secret_key", "client_secret", "authorization", "secret_access_key") or normalized.endswith("_api_key")
            out[redact_text(key)] = "[REDACTED_SECRET]" if sensitive and v else sanitize(v)
        return out
    return obj


def scan(root):
    findings = []
    for p in sorted(Path(root).rglob("*")):
        if not p.is_file() or "__pycache__" in p.parts:
            continue
        if p.suffix in (".zip", ".png", ".pdf"):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for i, pat in enumerate(SECRET_PATTERNS):
            if pat.search(text):
                findings.append({"file": p.relative_to(root).as_posix(), "kind": f"credential_pattern_{i}"})
        if p.name in (".env", ".git-credentials", "id_rsa", "id_ed25519"):
            findings.append({"file": p.relative_to(root).as_posix(), "kind": "forbidden_file"})
    return findings
