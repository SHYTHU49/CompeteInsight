"""全局配置：从 .env 读取，全应用单例。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    """全局应用配置，从环境变量 / .env 加载。"""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / "backend" / ".env",
        env_file_encoding="utf-8",
        env_prefix="",
        extra="ignore",
    )

    # --- LLM ---
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    qwen_api_key: str = ""
    qwen_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    ark_api_key: str = ""
    ark_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    cg_llm_provider: str = "deepseek"
    cg_llm_model: str = "deepseek-chat"
    cg_llm_temperature: float = Field(default=0.2, ge=0, le=2)
    cg_llm_timeout_seconds: int = Field(default=90, ge=10, le=300)
    cg_llm_min_interval_seconds: float = Field(default=1.2, ge=0, le=30)
    cg_llm_max_retries: int = Field(default=3, ge=0, le=8)
    cg_llm_rate_limit_cooldown_seconds: float = Field(default=12.0, ge=1, le=120)

    # --- 搜索 ---
    serper_api_key: str = ""
    tavily_api_key: str = ""
    brave_search_api_key: str = ""
    serpapi_api_key: str = ""
    searchapi_api_key: str = ""
    bing_search_api_key: str = ""
    exa_api_key: str = ""
    searxng_base_url: str = ""
    zhihu_api_key: str = ""
    cg_search_providers: str = "tavily,exa,zhihu_inner,zhihu_global"
    cg_search_use_all_providers: bool = True
    cg_search_provider_timeout_seconds: int = Field(default=12, ge=3, le=60)
    cg_search_default_results_per_provider: int = Field(default=5, ge=1, le=20)
    cg_search_max_results_per_provider: int = Field(default=12, ge=1, le=50)
    tavily_search_depth: str = "basic"
    serpapi_engine: str = "google"
    searchapi_engine: str = "google"
    bing_search_market: str = "en-US"
    exa_search_type: str = "auto"
    searxng_categories: str = "general"
    searxng_language: str = "all"

    # --- Agent 循环参数 ---
    cg_max_search_rounds: int = Field(default=3, ge=1, le=8)      # Search Agent 内层最大轮次
    cg_max_research_loops: int = Field(default=3, ge=1, le=8)     # Planning→Search→Evidence→Analysis 外层最大循环次数
    cg_min_coverage_to_stop: float = Field(default=0.85, ge=0, le=1)  # 覆盖度达到此值且无关键缺口时才提前收敛

    # --- 运行参数 ---
    cg_data_dir: str = "../data"
    cg_max_parallel_nodes: int = Field(default=4, ge=1, le=32)
    cg_evidence_llm_parallelism: int = Field(default=2, ge=1, le=16)
    cg_max_budget_cny: float = Field(default=2.0, ge=0)
    cg_llm_cache_ttl_days: int = 7
    cg_run_stale_after_seconds: int = Field(default=600, ge=60, le=3600)

    # --- HTTP ---
    cg_http_timeout_seconds: int = 30
    cg_http_proxy: str = ""
    cg_user_agent: str = "CompeteGraphBot/0.1"

    # --- 日志 ---
    cg_log_level: str = "INFO"
    cg_log_json: bool = False

    # --- CORS ---
    cg_cors_origins: str = "http://localhost:5173,http://localhost:3000"

    # --- Auth ---
    cg_auth_username: str = "change-me"
    cg_auth_password: str = "change-me"
    cg_auth_secret: str = "replace-with-a-long-random-secret"
    cg_auth_cookie_name: str = "cg_session"
    cg_auth_session_ttl_seconds: int = Field(default=60 * 60 * 12, ge=300, le=60 * 60 * 24 * 30)

    # ============================================================
    # 派生属性
    # ============================================================

    @property
    def data_dir(self) -> Path:
        """`data/` 绝对路径。"""
        p = Path(self.cg_data_dir)
        if not p.is_absolute():
            p = (REPO_ROOT / "backend" / p).resolve()
        return p

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cg_cors_origins.split(",") if o.strip()]

    @property
    def http_proxy(self) -> str:
        """Proxy value with inline-comment placeholders treated as empty."""

        value = self.cg_http_proxy.strip()
        if not value or value.startswith("#"):
            return ""
        return value.split()[0]

    @property
    def active_llm_api_key(self) -> str:
        provider = self.cg_llm_provider.lower().strip()
        provider_keys = {
            "ark": self.ark_api_key,
            "deepseek": self.deepseek_api_key,
            "qwen": self.qwen_api_key,
        }
        key = provider_keys.get(provider, self.deepseek_api_key)
        key = key.strip()
        if not key or "xxxx" in key.lower() or key.lower().startswith("sk-xxxx"):
            return ""
        return key

    @property
    def active_llm_base_url(self) -> str:
        provider = self.cg_llm_provider.lower().strip()
        provider_base_urls = {
            "ark": self.ark_base_url,
            "deepseek": self.deepseek_base_url,
            "qwen": self.qwen_base_url,
        }
        return provider_base_urls.get(provider, self.deepseek_base_url)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """单例 Settings。"""
    return Settings()
