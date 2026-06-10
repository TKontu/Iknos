from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = Field(..., alias="DATABASE_URL")
    api_host: str = Field("0.0.0.0", alias="API_HOST")
    api_port: int = Field(8000, alias="API_PORT")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    graph_name: str = Field("iknos", alias="GRAPH_NAME")

    # vLLM OpenAI-compatible endpoint for propositionization (Phase 1 Increment 3).
    # llm_model has no usable default (it is the served model id, recorded in
    # Action.model); the LLM client enforces it is set before any call. It is not a
    # required field here so that unrelated code/tests importing the config singleton
    # do not need LLM_MODEL in their environment.
    llm_base_url: str = Field("http://192.168.0.247:8000/v1", alias="LLM_BASE_URL")
    llm_model: str = Field("", alias="LLM_MODEL")

    # Independent verifier endpoint for extract-then-verify (§3.1/§13, G1.4). A *different
    # model family* from the extractor cuts correlated error; it may be served by the same
    # vLLM on a different model id or by a separate server, so base_url and model are
    # configured separately. An empty llm_verifier_model is the "verifier off" signal —
    # the propositionizer then runs in degraded mode and faithfulness/provisional stay null
    # (the documented G1.1 state), so importing the config singleton never requires it.
    llm_verifier_base_url: str = Field(
        "http://192.168.0.247:8000/v1", alias="LLM_VERIFIER_BASE_URL"
    )
    llm_verifier_model: str = Field("", alias="LLM_VERIFIER_MODEL")


settings = Settings()
