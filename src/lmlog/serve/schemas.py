"""Request and response models for the optional lmlog HTTP API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class JobCreated(BaseModel):
    job_id: str
    status: str
    filename: str
    size_bytes: int


class JobView(BaseModel):
    id: str
    filename: str
    size_bytes: int
    status: Literal["queued", "running", "completed", "failed"]
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    progress: dict = Field(default_factory=dict)
    stats: dict | None = None
    error: str | None = None


class ExplainRequest(BaseModel):
    snapshot_id: str = Field(min_length=1, max_length=64)
    signature: str = Field(min_length=1, max_length=2000)
    use_llm: bool = True
    refresh: bool = False


class ExplainResponse(BaseModel):
    signature: str
    text: str
    source: Literal["llm", "heuristic", "cache"]
    cached: bool = False
    model: str | None = None


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    snapshot_id: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=2000)
    signature: str | None = Field(default=None, max_length=2000)
    top_n: int = Field(default=8, ge=1, le=20)
    history: list[ChatMessage] = Field(default_factory=list, max_length=6)
    use_llm: bool = True

    @field_validator("question")
    @classmethod
    def question_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("question must not be blank")
        return cleaned


class Citation(BaseModel):
    signature: str
    count: int
    errors: int
    sample: str


class ChatResponse(BaseModel):
    answer: str
    source: Literal["llm", "heuristic"]
    citations: list[Citation] = Field(default_factory=list)
    model: str | None = None
    note: str | None = None


class SignatureView(BaseModel):
    signature: str
    count: int
    errors: int
    levels: dict[str, int]
    classes: dict[str, int]
    first_ts: str
    last_ts: str
    sample_message: str
    sample_raw: str
    sample_context: dict
    daily: dict[str, int]


class SignaturePage(BaseModel):
    items: list[SignatureView]
    total: int
    limit: int
    offset: int
    snapshot: dict


class ErrorClusterView(SignatureView):
    pass


class TrendView(BaseModel):
    signature: str
    kind: str
    severity: str
    count: int
    errors: int
    classes: list[str]
    sample: str
    note: str
    detail: str
    daily: dict[str, int]


class SummaryView(BaseModel):
    snapshot_id: str
    created: str | None
    period_hours: str | None
    files: int
    lines: int
    entries: int
    signatures: int
    errors: int
    levels: dict[str, int]
    top_classes: list[dict]
    daily_totals: dict[str, int]
    stats: dict | None = None
