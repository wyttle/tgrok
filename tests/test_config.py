"""配置：备用模型继承主模型设置。"""

from tgrok import config


def test_fallback_endpoint_inherits_unset_fields_from_primary():
    config.LLM_PROTOCOL, config.LLM_BASE_URL, config.LLM_API_KEY = "responses", "https://relay.example/v1", "sk-main"
    config.LLM_FALLBACK_MODEL, config.LLM_FALLBACK_PROTOCOL = "", ""
    assert config.fallback_endpoint() is None  # 没配模型名就不启用

    config.LLM_FALLBACK_MODEL = "gpt-4.1"
    config.LLM_FALLBACK_BASE_URL, config.LLM_FALLBACK_API_KEY = "", ""
    fb = config.fallback_endpoint()
    assert (fb.protocol, fb.base_url, fb.api_key) == ("responses", "https://relay.example/v1", "sk-main")

    config.LLM_FALLBACK_PROTOCOL, config.LLM_FALLBACK_API_KEY = "claude", "sk-ant"
    fb = config.fallback_endpoint()
    assert (fb.protocol, fb.base_url, fb.api_key) == ("claude", "https://relay.example", "sk-ant")  # claude 去掉 /v1
