import json

from jev.advisory import AdvisoryService
from jev.engine import _invoke_advisory, build_role_provenance
from jev.providers import OpenAICompatibleProvider
from main import build_advisory_service


def test_omitted_advisory_is_non_fatal_and_recorded_as_unconfigured():
    telemetry = _invoke_advisory(None, {"failure_class": "test"})
    assert telemetry["status"] == "not_configured"
    assert build_advisory_service(None, None) is None


def test_configured_advisory_is_injected():
    advisory = build_advisory_service("https://api.example.test/v1", "repair-model", "secret-token")
    assert isinstance(advisory, AdvisoryService)
    assert isinstance(advisory.provider, OpenAICompatibleProvider)
    assert advisory.provider.model == "repair-model"


def test_role_metadata_has_origin_and_no_secret():
    advisory = build_advisory_service("https://user:secret-token@api.example.test/v1", "repair-model", "secret-token")
    metadata = build_role_provenance(None, None, advisory)
    encoded = json.dumps(metadata, sort_keys=True)
    assert metadata["advisory"]["base_url_origin"] == "https://api.example.test"
    assert "secret-token" not in encoded
    assert metadata["advisory"]["capabilities"] == ["repair_guidance"]
