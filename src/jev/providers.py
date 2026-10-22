"""HTTP adapters for OpenAI-compatible chat-completions providers."""

from typing import Any, Dict, Optional

import httpx

from jev.advisory import (AdvisoryErrorCode, AdvisoryRequest, AdvisoryResult,
                          AdvisoryProvider, _safe_message, parse_repair_brief)


class OpenAICompatibleProvider:
    """A minimal adapter for local or remote ``/chat/completions`` endpoints."""

    def __init__(self, base_url: str, model: str, *, api_key: Optional[str] = None,
                 timeout: float = 20.0, client: Optional[httpx.Client] = None):
        if not base_url or not model:
            raise ValueError("base_url and model are required")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self._client = client

    def advise(self, request: AdvisoryRequest) -> AdvisoryResult:
        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.append({"role": "user", "content": request.prompt})
        payload: Dict[str, Any] = {"model": self.model, "messages": messages}
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.json_schema is not None:
            payload["response_format"] = {"type": "json_schema", "json_schema": request.json_schema}
        elif request.json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        client = self._client or httpx.Client(timeout=self.timeout)
        try:
            response = client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
            if response.is_error:
                return AdvisoryResult(ok=False, error_code=AdvisoryErrorCode.HTTP,
                                      error_message=_safe_message(f"HTTP {response.status_code}: {response.text}", self.api_key),
                                      model=self.model)
            try:
                data = response.json()
                content = data["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                return AdvisoryResult(ok=False, error_code=AdvisoryErrorCode.MALFORMED_RESPONSE,
                                      error_message=_safe_message(str(exc), self.api_key), model=self.model)
            try:
                brief = parse_repair_brief(content)
            except ValueError as exc:
                return AdvisoryResult(ok=False, error_code=AdvisoryErrorCode.VALIDATION,
                                      error_message=_safe_message(str(exc), self.api_key), model=self.model)
            return AdvisoryResult(ok=True, brief=brief, model=self.model)
        except httpx.TimeoutException as exc:
            return AdvisoryResult(ok=False, error_code=AdvisoryErrorCode.TIMEOUT,
                                  error_message=_safe_message(str(exc) or "request timed out", self.api_key), model=self.model)
        except httpx.NetworkError as exc:
            return AdvisoryResult(ok=False, error_code=AdvisoryErrorCode.CONNECTION,
                                  error_message=_safe_message(str(exc) or "connection failed", self.api_key), model=self.model)
        except httpx.TransportError as exc:
            return AdvisoryResult(ok=False, error_code=AdvisoryErrorCode.CONNECTION,
                                  error_message=_safe_message(str(exc) or "transport failed", self.api_key), model=self.model)
        except Exception as exc:
            return AdvisoryResult(ok=False, error_code=AdvisoryErrorCode.UNKNOWN,
                                  error_message=_safe_message(str(exc), self.api_key), model=self.model)
        finally:
            if self._client is None:
                client.close()


__all__ = ["OpenAICompatibleProvider", "AdvisoryProvider"]
