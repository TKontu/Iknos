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

    # Hard wall-clock deadline for one guided_complete call *including* all tenacity retries
    # (G1.17 R5). The retry policy bounds the backoff *waits* (~15 s), but a hung endpoint that
    # never returns and never errors would otherwise hold its concurrency permit — starving the
    # whole batch — for as long as the socket stays open. This outer asyncio.timeout is the
    # backstop above that ceiling: a call exceeding it is cancelled and its permit released.
    # Generous so a slow-but-healthy model is never cut off; the OpenAI client's own per-request
    # timeout is the finer guard inside each attempt. Mirrors core/llm.py::DEFAULT_CALL_TIMEOUT_S.
    llm_call_timeout_s: float = Field(180.0, alias="LLM_CALL_TIMEOUT_S")

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

    # Multi-sample extraction for the consistency half of faithfulness (§3.1, G1.3). The
    # extractor is sampled llm_extract_samples times; a proposition reproduced across samples is
    # stable (high agreement), one emitted rarely is unstable → provisional. Default 1 is a strict
    # no-op (agreement always 1.0, faithfulness == the verify component) — the documented current
    # behavior. Raising it requires a temperature>0 sampling regime (the Propositionizer enforces
    # this), else the N samples are identical and carry no signal. prop_agreement_threshold is the
    # cosine cutoff at which two extractions count as the same claim (a Trial-A5 tunable).
    llm_extract_samples: int = Field(1, alias="LLM_EXTRACT_SAMPLES")
    prop_agreement_threshold: float = Field(0.86, alias="PROP_AGREEMENT_THRESHOLD")

    # Cross-document "extract once" reuse (§6.1, G1.7b). When a never-extracted span's pipeline
    # content_hash matches a prior committed extraction (the same text under the same model/prompt/
    # regime/verifier — re-segmentation, shared boilerplate, an overlapping reference corpus), its
    # propositions are replayed into the new span instead of re-running the LLM. On by default — it
    # is purely additive and sound (the hash carries the full pipeline identity); set false to fall
    # back to always re-extracting. (No production entrypoint constructs the Propositionizer yet;
    # this is the wiring seam for when one does, mirroring llm_extract_samples.)
    extract_reuse_enabled: bool = Field(True, alias="EXTRACT_REUSE_ENABLED")

    # Stage 0 document parse front-end (§1, G1.0). MinerU (AGPL-3.0) runs as a separate
    # hosted service behind this endpoint — the copyleft stops at the service edge, like
    # the LLM/verifier. An empty parser_base_url is the "no service" signal: ingest falls
    # back to the identity (null) parser — plain text in, no page geometry — which is a
    # first-class supported mode, not degradation. The real HTTP client is a later slice.
    parser_base_url: str = Field("", alias="PARSER_BASE_URL")
    parser_kind: str = Field("null", alias="PARSER_KIND")
    # Wall-clock budget for one parse request (seconds). Generous by default: a real parser
    # OCRs scanned pages, which is minutes for a large document — a short timeout would turn a
    # slow-but-healthy service into spurious ingest failures. Retries (transport/5xx only) sit
    # *inside* this budget per attempt in the MinerU client.
    parser_timeout_s: float = Field(300.0, alias="PARSER_TIMEOUT_S")


settings = Settings()
