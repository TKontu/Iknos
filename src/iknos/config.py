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


settings = Settings()
