"""Regression tests for secret redaction gaps from Fix 8a."""

import pytest

from jev.proposals import redact_data_structure, redact_secrets_in_text


@pytest.mark.parametrize(
    "value",
    [
        "GOOGLE_API_KEY=AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
        "OPENAI_API_KEY=sk-short",
        "DB_PASSWORD=hunter2hunter2",
        "MY_SECRET=abc123def456",
        "AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature-value",
        "postgres://admin:S3cretPass@db.example.com:5432/app",
        "mysql://admin:S3cretPass@db.example.com/app",
        "mongodb://admin:S3cretPass@db.example.com/app",
        "redis://admin:S3cretPass@db.example.com/0",
        "amqp://admin:S3cretPass@mq.example.com/vhost",
        "ftp://admin:S3cretPass@files.example.com/data",
        "aws_secret_access_key = AWSsecretvalue1234567890",
        "AWS_SECRET_ACCESS_KEY=AWSsecretvalue1234567890",
    ],
)
def test_requested_secret_shapes_are_fully_redacted(value):
    redacted = redact_secrets_in_text(value)
    assert redacted != value
    for secret in (
        "AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
        "sk-short",
        "hunter2hunter2",
        "abc123def456",
        "S3cretPass",
        "AWSsecretvalue1234567890",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature-value",
    ):
        assert secret not in redacted


@pytest.mark.parametrize(
    "safe",
    [
        "def authenticate(user): return user.token_count + 1  # commit 98f3cad",
        "40-hex: 0123456789abcdef0123456789abcdef01234567",
        "max_tokens=800",
        "token_count = 5",
        "KEYBOARD_LAYOUT=us",
        "plain hashes in diffs: 0123456789abcdef0123456789abcdef",
        "ordinary prose that contains the words token or secret",
    ],
)
def test_non_secret_values_remain_unchanged(safe):
    assert redact_secrets_in_text(safe) == safe


def test_data_structure_redaction_keeps_non_secret_key_names():
    value = {
        "GOOGLE_API_KEY": "AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
        "token_count": 5,
        "KEYBOARD_LAYOUT": "us",
    }
    redacted = redact_data_structure(value)
    assert redacted["GOOGLE_API_KEY"] == "[REDACTED]"
    assert redacted["token_count"] == 5
    assert redacted["KEYBOARD_LAYOUT"] == "us"


def test_redaction_is_idempotent_and_handles_empty_none_and_very_long_text():
    assert redact_secrets_in_text(None) is None
    assert redact_secrets_in_text("") == ""
    text = "prefix OPENAI_API_KEY=sk-short " + ("ordinary text " * 10000)
    once = redact_secrets_in_text(text)
    assert redact_secrets_in_text(once) == once
    assert "sk-short" not in once
