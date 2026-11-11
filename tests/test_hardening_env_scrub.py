import json
import os
import sys
import pytest

from jev.process_runner import (
    SAFE_ENV_NAMES,
    SAFE_ENV_PREFIXES,
    SENSITIVE_SUBSTRINGS,
    get_scrubbed_env,
    run_command,
)


@pytest.fixture(autouse=True)
def setup_secret_env(monkeypatch):
    """Set secret API keys and tokens in the parent process environment."""
    monkeypatch.setenv("JEV_API_KEY", "jev-secret-api-key-xyz")
    monkeypatch.setenv("GOOGLE_API_KEY", "google-secret-api-key-123")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret-api-key-456")
    monkeypatch.setenv("MY_SECRET_TOKEN", "my-super-secret-token-789")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret-access-key-abc")
    monkeypatch.setenv("UNAPPROVED_SECRET_PASSWORD", "super-secret-password")
    monkeypatch.setenv("CUSTOM_ARBITRARY_VAR", "should-be-dropped-by-allowlist")


def test_spawn_scrubs_api_keys_and_secret_tokens():
    """Verify spawned process does not inherit API keys or secret tokens by default."""
    code = "import os, json; print(json.dumps(dict(os.environ)))"
    res = run_command([sys.executable, "-c", code], timeout=10.0)
    assert res.exit_code == 0, f"Child process failed: {res.stderr}"

    child_env = json.loads(res.stdout)

    # Assert secret keys and tokens are completely absent from child process
    assert "JEV_API_KEY" not in child_env
    assert "GOOGLE_API_KEY" not in child_env
    assert "OPENAI_API_KEY" not in child_env
    assert "MY_SECRET_TOKEN" not in child_env
    assert "AWS_SECRET_ACCESS_KEY" not in child_env
    assert "UNAPPROVED_SECRET_PASSWORD" not in child_env
    assert "CUSTOM_ARBITRARY_VAR" not in child_env


def test_spawn_keeps_path_and_system_variables():
    """Verify essential system variables like PATH are retained."""
    code = "import os, json; print(json.dumps(dict(os.environ)))"
    res = run_command([sys.executable, "-c", code], timeout=10.0)
    assert res.exit_code == 0

    child_env = json.loads(res.stdout)

    # PATH must be present for standard command execution
    path_key = "PATH" if "PATH" in child_env else "Path"
    assert path_key in child_env
    assert len(child_env[path_key]) > 0


def test_spawn_with_explicitly_declared_variables():
    """Verify variables explicitly declared by proposal / caller pass through, even with sensitive names."""
    code = "import os, json; print(json.dumps(dict(os.environ)))"
    declared = {
        "MY_DECLARED_INPUT": "explicit_value_123",
        "MY_APPROVED_TOKEN": "approved_token_value_456",
    }
    res = run_command([sys.executable, "-c", code], env=declared, timeout=10.0)
    assert res.exit_code == 0

    child_env = json.loads(res.stdout)

    # Declared variables must be present
    assert child_env.get("MY_DECLARED_INPUT") == "explicit_value_123"
    assert child_env.get("MY_APPROVED_TOKEN") == "approved_token_value_456"

    # Undeclared parent secrets must remain scrubbed
    assert "JEV_API_KEY" not in child_env
    assert "GOOGLE_API_KEY" not in child_env
    assert "OPENAI_API_KEY" not in child_env
    assert "MY_SECRET_TOKEN" not in child_env


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-specific test")
def test_windows_systemroot_kept():
    """Verify SYSTEMROOT (and Windows essentials) are preserved on Windows."""
    code = "import os, json; print(json.dumps(dict(os.environ)))"
    res = run_command([sys.executable, "-c", code], timeout=10.0)
    assert res.exit_code == 0

    child_env = json.loads(res.stdout)
    assert "SYSTEMROOT" in child_env or "SystemRoot" in child_env
    root_val = child_env.get("SYSTEMROOT") or child_env.get("SystemRoot")
    assert root_val is not None
    assert root_val.lower() == os.environ.get("SystemRoot", os.environ.get("SYSTEMROOT", "")).lower()


def test_opt_out_exposes_secrets_demonstrating_test_fails_if_scrub_removed():
    """Demonstrate that if scrubbing is disabled / removed, secret keys leak to child process.

    Each test asserting absent secrets would fail if the scrub is removed.
    """
    code = "import os, json; print(json.dumps(dict(os.environ)))"

    # Test explicit opt_out=True
    res_opt_out = run_command([sys.executable, "-c", code], opt_out=True, timeout=10.0)
    assert res_opt_out.exit_code == 0
    child_env_opt_out = json.loads(res_opt_out.stdout)

    assert child_env_opt_out.get("JEV_API_KEY") == "jev-secret-api-key-xyz"
    assert child_env_opt_out.get("GOOGLE_API_KEY") == "google-secret-api-key-123"
    assert child_env_opt_out.get("OPENAI_API_KEY") == "openai-secret-api-key-456"
    assert child_env_opt_out.get("MY_SECRET_TOKEN") == "my-super-secret-token-789"

    # Test scrub_env=False
    res_scrub_false = run_command([sys.executable, "-c", code], scrub_env=False, timeout=10.0)
    assert res_scrub_false.exit_code == 0
    child_env_scrub_false = json.loads(res_scrub_false.stdout)

    assert child_env_scrub_false.get("JEV_API_KEY") == "jev-secret-api-key-xyz"
    assert child_env_scrub_false.get("GOOGLE_API_KEY") == "google-secret-api-key-123"


def test_env_var_opt_out_toggle(monkeypatch):
    """Verify JEV_DISABLE_ENV_SCRUB=1 opts out of scrubbing."""
    monkeypatch.setenv("JEV_DISABLE_ENV_SCRUB", "1")
    code = "import os, json; print(json.dumps(dict(os.environ)))"
    res = run_command([sys.executable, "-c", code], timeout=10.0)
    assert res.exit_code == 0
    child_env = json.loads(res.stdout)

    assert child_env.get("JEV_API_KEY") == "jev-secret-api-key-xyz"
    assert child_env.get("GOOGLE_API_KEY") == "google-secret-api-key-123"


def test_get_scrubbed_env_layer1_and_layer2():
    """Unit test for get_scrubbed_env allowlist and substring filtering."""
    base = {
        "PATH": "/usr/bin:/bin",
        "SYSTEMROOT": "C:\\Windows",
        "UNALLOWED_VAR": "should_be_dropped",
        "NPM_CONFIG_REGISTRY": "https://registry.npmjs.org",
        "NPM_CONFIG_AUTH_TOKEN": "secret_npm_token",  # matches prefix, but contains AUTH and TOKEN
        "MY_API_KEY": "secret_api_key",
        "CUSTOM_PASSWORD": "secret_password",
    }
    scrubbed = get_scrubbed_env(base_env=base)

    assert "PATH" in scrubbed
    assert "SYSTEMROOT" in scrubbed
    assert "UNALLOWED_VAR" not in scrubbed
    assert "NPM_CONFIG_REGISTRY" in scrubbed
    # Layer 2 must drop NPM_CONFIG_AUTH_TOKEN because it contains AUTH / TOKEN
    assert "NPM_CONFIG_AUTH_TOKEN" not in scrubbed
    assert "MY_API_KEY" not in scrubbed
    assert "CUSTOM_PASSWORD" not in scrubbed

    # Merging declared vars
    declared = {"CUSTOM_PASSWORD": "declared_and_approved"}
    with_declared = get_scrubbed_env(declared_env=declared, base_env=base)
    assert with_declared["CUSTOM_PASSWORD"] == "declared_and_approved"
