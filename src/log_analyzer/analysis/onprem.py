"""Internal Chat Completions servers; no environment proxies or cloud fallback."""

from .nvidia_nim import NvidiaNimAnalyzer


class OnPremAnalyzer(NvidiaNimAnalyzer):
    _provider_name = "On-premises LLM"
    _requires_api_key = False
    _trust_env = False

    def __init__(self, *, base_url: str, **kwargs) -> None:
        # A local server must never inherit the NVIDIA hosted endpoint.
        super().__init__(base_url=base_url, **kwargs)
