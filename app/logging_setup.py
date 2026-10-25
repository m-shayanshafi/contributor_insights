"""Logging configuration with secret redaction."""

import logging
import re

REDACTED = "[REDACTED]"

_PATTERNS = [
    # Authorization headers, with or without a "Bearer" prefix.
    re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?)(?:bearer\s+)?[^\s\"',}]+"),
    re.compile(r"(?i)(x-api-key[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"),
    # Token formats, in case one shows up outside a header.
    re.compile(r"()github_pat_[A-Za-z0-9_]+"),
    re.compile(r"()gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"()sk-ant-[A-Za-z0-9_-]+"),
]


class RedactSecretsFilter(logging.Filter):
    """Removes credentials from log records before they are emitted."""

    def __init__(self, secret_values: list[str] | None = None) -> None:
        super().__init__()
        self._secrets = [s for s in (secret_values or []) if s]

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        for pattern in _PATTERNS:
            text = pattern.sub(rf"\1{REDACTED}", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = self.redact(message)
        # Only rewrite records that contained a secret, so formatters that read
        # record.args (e.g. uvicorn's access log) keep the shape they expect.
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def configure_logging(secret_values: list[str], level: int = logging.INFO) -> None:
    """Attach the redaction filter to the root handler and uvicorn's handlers."""
    redactor = RedactSecretsFilter(secret_values)
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)
    for name in ("", "uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            handler.addFilter(redactor)
