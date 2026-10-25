import io
import logging

import pytest

from app.logging_setup import REDACTED, RedactSecretsFilter

SECRET = "my-configured-secret-value"


@pytest.fixture
def capture():
    """A logger whose handler applies the redaction filter, writing to a buffer."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactSecretsFilter([SECRET]))
    logger = logging.getLogger("test.redaction")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield logger, stream
    logger.removeHandler(handler)


@pytest.mark.parametrize(
    "message, leaked",
    [
        ("headers={'Authorization': 'Bearer abc123token'}", "abc123token"),
        ("Authorization: token987", "token987"),
        ('{"x-api-key": "key-xyz-789"}', "key-xyz-789"),
        ('{"x-goog-api-key": "goog-key-456"}', "goog-key-456"),
        ("GET /v1beta/models?key=query-key-123&alt=json", "query-key-123"),
        ("using github_pat_11ABCDEFG_hijklmnop", "github_pat_11ABCDEFG_hijklmnop"),
        ("legacy ghp_abcdefghijklmnopqrstuvwxyz123456", "ghp_abcdefghijklmnopqrstuvwxyz123456"),
        ("key AIzaSyA1b2C3d4E5f6G7h8I9j0KlMnOpQrStUvW", "AIzaSyA1b2C3d4E5f6G7h8I9j0KlMnOpQrStUvW"),
    ],
)
def test_credentials_are_redacted(capture, message, leaked):
    logger, stream = capture
    logger.info(message)
    output = stream.getvalue()
    assert leaked not in output
    assert REDACTED in output


def test_configured_secret_redacted_in_format_args(capture):
    logger, stream = capture
    logger.info("calling with %s", SECRET)
    assert SECRET not in stream.getvalue()


def test_secret_inside_non_string_arg_redacted(capture):
    logger, stream = capture
    logger.info("request headers: %s", {"Authorization": "Bearer abc123token"})
    assert "abc123token" not in stream.getvalue()


def test_args_shape_preserved_for_uvicorn_access_log():
    # uvicorn's access formatter unpacks exactly five args from the record.
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:5000", "GET", "/healthz", "1.1", 200), None,
    )
    RedactSecretsFilter([SECRET]).filter(record)
    assert record.args == ("127.0.0.1:5000", "GET", "/healthz", "1.1", 200)


def test_normal_messages_untouched(capture):
    logger, stream = capture
    logger.info("synced %d PRs for %s", 12, "frenck")
    assert stream.getvalue().strip() == "synced 12 PRs for frenck"
