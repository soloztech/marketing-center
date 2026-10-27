"""Compact public codes and recognition of references already issued."""

import re
import secrets

REFERENCE_LENGTH = 4
REFERENCE_ALPHABET = "23456789BCDFGHJKLMNPQRSTVWXYZ"
SHORT_CODE_RE = re.compile(r"[A-Z0-9]{4}")
LEGACY_REFERENCE_RE = re.compile(
    r"(?<![A-Za-z0-9-])([A-Z0-9]{2,8}-[A-Z0-9]{12})(?![A-Za-z0-9-])"
)
LEGACY_TOKEN_HINT_RE = re.compile(r"\b[A-Za-z0-9]{2,8}[-‐‑‒–—][A-Za-z0-9]{6,32}\b")
SHORT_REFERENCE_RE = re.compile(
    r"(?i:\bmeu\s+c[oó]digo\s+[eé])\s+([A-Z0-9]{4})(?![\w-])"
)
SHORT_HINT_RE = re.compile(r"\bmeu\s+c[oó]digo\s+[eé]\b", re.IGNORECASE)
REFERENCE_HINT_RE = re.compile(r"refer[eê]ncia\s*:", re.IGNORECASE)


def new_reference():
    return "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(REFERENCE_LENGTH))


def reference_message(message, reference):
    """Keep the old format on retries of clicks created before this change."""
    if SHORT_CODE_RE.fullmatch(reference):
        line = "Meu código é " + reference
        separator = "\n"
    elif LEGACY_REFERENCE_RE.fullmatch(reference):
        line = "Referência: " + reference
        separator = "\n\n"
    else:
        raise ValueError("Invalid WhatsApp reference")
    return (message + separator if message else "") + line


def extract_reference(text):
    """Accept one exact reference; short codes need their explicit message label."""
    legacy = LEGACY_REFERENCE_RE.findall(text)
    short = SHORT_REFERENCE_RE.findall(text)
    references = legacy + short
    if (
        len(references) != 1
        or LEGACY_TOKEN_HINT_RE.findall(text) != legacy
        or len(SHORT_HINT_RE.findall(text)) != len(short)
        or len(REFERENCE_HINT_RE.findall(text)) > len(legacy)
    ):
        return ""
    return references[0]


def has_reference_hint(text):
    return bool(
        REFERENCE_HINT_RE.search(text)
        or LEGACY_TOKEN_HINT_RE.search(text)
        or SHORT_HINT_RE.search(text)
    )
