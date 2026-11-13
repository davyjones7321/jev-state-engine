"""Token usage recorder for LangChain LLM calls."""

from typing import Any, Dict, Optional

try:
    from langchain_core.callbacks import BaseCallbackHandler
except ImportError:
    class BaseCallbackHandler:  # type: ignore
        """Fallback BaseCallbackHandler if langchain_core is missing."""
        def on_llm_end(self, *args: Any, **kwargs: Any) -> Any:
            pass


class UsageRecorder(BaseCallbackHandler):
    """LangChain callback handler recording token usage from LLM response metadata.

    Accumulates total input tokens, output tokens, cache-read tokens, and call count
    across all Gemini calls in a run (investigation, planning, worker).
    Guaranteed never to raise: all operations are wrapped in try/except.
    """

    def __init__(self) -> None:
        try:
            super().__init__()
        except Exception:
            pass
        self.llm_tokens_in: int = 0
        self.llm_tokens_out: int = 0
        self.llm_cache_read_tokens: int = 0
        self.llm_calls: int = 0

    @property
    def tokens_in(self) -> int:
        return self.llm_tokens_in

    @property
    def tokens_out(self) -> int:
        return self.llm_tokens_out

    @property
    def cache_read_tokens(self) -> int:
        return self.llm_cache_read_tokens

    @property
    def calls(self) -> int:
        return self.llm_calls

    def on_llm_end(self, response: Any, *args: Any, **kwargs: Any) -> None:
        """Callback triggered at the end of an LLM generation call.

        Reads usage_metadata from the response message (input_tokens, output_tokens,
        and cache-read tokens if present) and accumulates totals plus a call count.
        Guaranteed never to raise.
        """
        try:
            self.llm_calls += 1
            if response is None:
                return

            generations = getattr(response, "generations", None)
            found_usage = False

            if generations and isinstance(generations, list):
                for gen_list in generations:
                    if not isinstance(gen_list, list):
                        continue
                    for gen in gen_list:
                        msg = getattr(gen, "message", None)
                        usage = None
                        if msg is not None:
                            usage = getattr(msg, "usage_metadata", None)
                            if usage is None and isinstance(msg, dict):
                                usage = msg.get("usage_metadata")
                        if usage is None and hasattr(gen, "generation_info") and isinstance(gen.generation_info, dict):
                            usage = gen.generation_info.get("usage_metadata")
                        if usage is None and isinstance(gen, dict):
                            usage = gen.get("usage_metadata")

                        if usage:
                            self._accumulate_usage(usage)
                            found_usage = True
                            break

            if not found_usage:
                llm_output = getattr(response, "llm_output", None)
                if isinstance(llm_output, dict):
                    usage = llm_output.get("usage_metadata") or llm_output.get("token_usage")
                    if usage:
                        self._accumulate_usage(usage)
                        found_usage = True

            if not found_usage and hasattr(response, "usage_metadata"):
                self._accumulate_usage(response.usage_metadata)
        except Exception:
            pass

    def _accumulate_usage(self, usage: Any) -> None:
        try:
            # Input tokens
            inp = 0
            if isinstance(usage, dict):
                inp = (
                    usage.get("input_tokens")
                    or usage.get("prompt_tokens")
                    or usage.get("prompt_token_count")
                    or 0
                )
            else:
                inp = (
                    getattr(usage, "input_tokens", None)
                    or getattr(usage, "prompt_tokens", None)
                    or getattr(usage, "prompt_token_count", None)
                    or 0
                )
            self.llm_tokens_in += int(inp or 0)

            # Output tokens
            out = 0
            if isinstance(usage, dict):
                out = (
                    usage.get("output_tokens")
                    or usage.get("completion_tokens")
                    or usage.get("candidates_token_count")
                    or 0
                )
            else:
                out = (
                    getattr(usage, "output_tokens", None)
                    or getattr(usage, "completion_tokens", None)
                    or getattr(usage, "candidates_token_count", None)
                    or 0
                )
            self.llm_tokens_out += int(out or 0)

            # Cache-read tokens
            cache_read = 0
            if isinstance(usage, dict):
                for k in (
                    "cache_read_tokens",
                    "cache_read_input_tokens",
                    "cache_read",
                    "cached_content_token_count",
                    "cached_tokens",
                ):
                    if k in usage and usage[k] is not None:
                        cache_read = int(usage[k])
                        break
                else:
                    details = usage.get("input_token_details")
                    if isinstance(details, dict):
                        for k in ("cache_read", "cache_read_tokens", "cached_tokens"):
                            if k in details and details[k] is not None:
                                cache_read = int(details[k])
                                break
            else:
                details = getattr(usage, "input_token_details", None)
                if isinstance(details, dict):
                    cache_read = details.get("cache_read") or details.get("cache_read_tokens") or 0
                elif hasattr(details, "cache_read"):
                    cache_read = getattr(details, "cache_read", 0) or 0
                else:
                    for k in (
                        "cache_read_tokens",
                        "cache_read_input_tokens",
                        "cache_read",
                        "cached_content_token_count",
                        "cached_tokens",
                    ):
                        val = getattr(usage, k, None)
                        if val is not None:
                            cache_read = int(val)
                            break

            self.llm_cache_read_tokens += int(cache_read or 0)
        except Exception:
            pass


__all__ = ["UsageRecorder"]
