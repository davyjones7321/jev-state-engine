import http.client
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
import urllib.request

import jsonschema

from evals.runner import (
    DRY_RUN_SMOKE_TASKS,
    check_credential_boundary,
    create_run_record,
    execute_dry_run,
    get_sanitized_environment,
    is_sensitive_env_var,
    load_run_schema,
    validate_run_record,
)


class CredentialBoundaryTests(unittest.TestCase):
    def test_is_sensitive_env_var_detection(self):
        sensitive = [
            "JEV_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
            "DEEPSEEK_API_KEY",
            "CUSTOM_API_KEY",
            "SERVICE_APIKEY",
            "CLIENT_SECRET",
            "USER_AUTH_TOKEN",
            "MY_API_TOKEN",
        ]
        for name in sensitive:
            self.assertTrue(is_sensitive_env_var(name), f"Expected {name} to be identified as sensitive")

        benign = ["PATH", "HOME", "USER", "LANG", "PYTHONPATH", "TEMP", "TMP", "TERM"]
        for name in benign:
            self.assertFalse(is_sensitive_env_var(name), f"Expected {name} to be non-sensitive")

    def test_clean_environment_passes_probe(self):
        clean_env = {"PATH": "C:\\Windows\\system32", "LANG": "en_US.UTF-8"}
        passed, leaked = check_credential_boundary(env=clean_env)
        self.assertTrue(passed)
        self.assertEqual(leaked, [])

    def test_probe_detects_api_key_in_child_process(self):
        dirty_env = {
            "PATH": "C:\\Windows\\system32",
            "JEV_API_KEY": "secret-test-key",
            "OPENAI_API_KEY": "sk-12345",
        }
        passed, leaked = check_credential_boundary(env=dirty_env)
        self.assertFalse(passed)
        self.assertEqual(set(leaked), {"JEV_API_KEY", "OPENAI_API_KEY"})

    def test_get_sanitized_environment_strips_all_keys(self):
        base_env = {
            "PATH": "C:\\test",
            "JEV_API_KEY": "secret",
            "ANTHROPIC_API_KEY": "sk-ant-test",
            "FOO_SECRET_KEY": "xyz",
            "SAFE_VAR": "keep-me",
        }
        sanitized = get_sanitized_environment(base_env=base_env)
        self.assertIn("PATH", sanitized)
        self.assertIn("SAFE_VAR", sanitized)
        self.assertNotIn("JEV_API_KEY", sanitized)
        self.assertNotIn("ANTHROPIC_API_KEY", sanitized)
        self.assertNotIn("FOO_SECRET_KEY", sanitized)

    def test_run_record_credential_boundary_verified_field_in_schema(self):
        schema = load_run_schema()
        # Record with boundary verified
        record_pass = create_run_record(
            task_id="Mbpp/2",
            status="completed",
            credential_boundary_verified=True,
            evaluator_status="not_scored",
        )
        validate_run_record(record_pass, schema=schema)
        self.assertTrue(record_pass["execution"]["credential_boundary_verified"])

        # Record with boundary failed
        record_fail = create_run_record(
            task_id="Mbpp/2",
            status="agent_failure",
            credential_boundary_verified=False,
            evaluator_status="failed",
            failure_class="credential_boundary_violation",
        )
        validate_run_record(record_fail, schema=schema)
        self.assertFalse(record_fail["execution"]["credential_boundary_verified"])

        # Schema constraint: status="completed" requires credential_boundary_verified=True
        invalid_record = dict(record_pass)
        invalid_record["execution"] = dict(record_pass["execution"])
        invalid_record["execution"]["credential_boundary_verified"] = False
        with self.assertRaises(jsonschema.ValidationError):
            validate_run_record(invalid_record, schema=schema)


class DryRunExecutionTests(unittest.TestCase):
    def test_dry_run_executes_three_non_scored_tasks_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = Path(tmp_dir)
            result = execute_dry_run(output_dir=out_path)

            self.assertEqual(result["status"], "success")
            self.assertTrue(result["dry_run"])
            self.assertTrue(result["credential_boundary_verified"])
            self.assertEqual(result["tasks_executed"], ["Mbpp/2", "Mbpp/3", "Mbpp/4"])

            runs = result["runs"]
            self.assertEqual(len(runs), 3)

            schema = load_run_schema()
            for run_rec in runs:
                validate_run_record(run_rec, schema=schema)
                self.assertEqual(run_rec["status"], "completed")
                self.assertTrue(run_rec["execution"]["credential_boundary_verified"])
                self.assertEqual(run_rec["execution"]["network_policy"], "disabled")
                self.assertEqual(run_rec["result"]["evaluator_status"], "not_scored")
                self.assertEqual(run_rec["cost"]["amount"], 0.0)
                self.assertEqual(run_rec["cost"]["status"], "zero_additional_cost")

            # Verify files on disk
            for task_id in ["Mbpp/2", "Mbpp/3", "Mbpp/4"]:
                slug = task_id.replace("/", "_")
                task_dir = out_path / "workspaces" / "jev" / slug
                self.assertTrue((task_dir / "TASK.md").is_file())
                self.assertTrue((task_dir / "solution.py").is_file())
                run_file = out_path / "runs" / f"{slug}-run.json"
                self.assertTrue(run_file.is_file())

    def test_dry_run_makes_no_network_or_api_calls(self):
        def block_network(*args, **kwargs):
            raise AssertionError("Network connection attempted during dry run!")

        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch.object(socket.socket, "connect", side_effect=block_network):
                with patch.object(urllib.request, "urlopen", side_effect=block_network):
                    with patch.object(http.client.HTTPConnection, "request", side_effect=block_network):
                        result = execute_dry_run(output_dir=Path(tmp_dir))
                        self.assertEqual(result["status"], "success")
                        self.assertEqual(len(result["runs"]), 3)

    def test_dry_run_fails_closed_when_credential_leaked(self):
        dirty_env = {"PATH": "C:\\Windows\\system32", "JEV_API_KEY": "leaked-secret-key"}
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = Path(tmp_dir)
            result = execute_dry_run(
                output_dir=out_path,
                enforce_clean_env=False,
                injected_env=dirty_env,
            )
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["credential_boundary_verified"])
            self.assertIn("JEV_API_KEY", result["leaked_variables"])

            schema = load_run_schema()
            for run_rec in result["runs"]:
                validate_run_record(run_rec, schema=schema)
                self.assertEqual(run_rec["status"], "agent_failure")
                self.assertFalse(run_rec["execution"]["credential_boundary_verified"])
                self.assertEqual(run_rec["result"]["failure_class"], "credential_boundary_violation")


if __name__ == "__main__":
    unittest.main()
