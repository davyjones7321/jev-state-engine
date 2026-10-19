import json

import httpx

from jev.advisory import (AdvisoryErrorCode, AdvisoryRequest, AdvisoryService,
                          RepairBrief)
from jev.providers import OpenAICompatibleProvider


def _provider(handler, key="secret-key"):
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    return OpenAICompatibleProvider("http://local.test/v1", "local-model", api_key=key, client=client), client


def test_success_and_schema_request():
    seen = {}

    def handler(request):
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({
            "summary": "Fix it", "cause": "A bug", "actions": ["Add a test"]})}}]})

    provider, client = _provider(handler)
    try:
        result = provider.advise(AdvisoryRequest(prompt="review", json_schema={"name": "RepairBrief"}))
    finally:
        client.close()
    assert result.ok and result.brief == RepairBrief(summary="Fix it", cause="A bug", actions=["Add a test"])
    assert seen["json"]["response_format"]["type"] == "json_schema"
    assert seen["json"]["model"] == "local-model"


def test_malformed_response_is_non_fatal():
    provider, client = _provider(lambda request: httpx.Response(200, json={"choices": []}))
    try:
        result = provider.advise(AdvisoryRequest(prompt="review"))
    finally:
        client.close()
    assert not result.ok and result.error_code is AdvisoryErrorCode.MALFORMED_RESPONSE


def test_timeout_http_and_connection_errors_are_non_fatal():
    cases = [
        (httpx.ReadTimeout("slow"), AdvisoryErrorCode.TIMEOUT),
        (httpx.ConnectError("offline"), AdvisoryErrorCode.CONNECTION),
    ]
    for error, code in cases:
        def handler(request, error=error):
            raise error
        provider, client = _provider(handler)
        try:
            result = provider.advise(AdvisoryRequest(prompt="review"))
        finally:
            client.close()
        assert not result.ok and result.error_code is code

    provider, client = _provider(lambda request: httpx.Response(503, text="temporary failure"))
    try:
        result = provider.advise(AdvisoryRequest(prompt="review"))
    finally:
        client.close()
    assert not result.ok and result.error_code is AdvisoryErrorCode.HTTP


def test_api_key_is_not_leaked_and_fake_adapter_contract():
    provider, client = _provider(lambda request: httpx.Response(401, text="Bearer secret-key"))
    try:
        result = provider.advise(AdvisoryRequest(prompt="review"))
    finally:
        client.close()
    assert "secret-key" not in (result.error_message or "")

    class FakeAdapter:
        def advise(self, request):
            assert request.prompt == "repair"
            return type(result)(ok=True, brief=RepairBrief(summary="s", cause="c", actions=["a"]))

    result = AdvisoryService(FakeAdapter()).request_repair_brief("repair")
    assert result.ok and result.brief.summary == "s"
