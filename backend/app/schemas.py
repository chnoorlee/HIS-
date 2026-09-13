from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginIn(Strict):
    username: str = Field(max_length=128)
    password: str = Field(max_length=1024)


class SessionIn(Strict):
    encounter_id: str
    mode: Literal["dictation", "conversation"] = "dictation"
    consent_record: str | None = Field(None, max_length=1000)


class ChannelIn(Strict):
    channel_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    device_id: str | None = None
    capture_epoch: int | str | None = None


class TicketIn(Strict):
    device_id: str = Field(min_length=1, max_length=128)
    channels: list[str | ChannelIn] = Field(min_length=1, max_length=4)


class CaptureMetadata(Strict):
    input_sample_rate: int | None = Field(None, ge=8000, le=192000)
    input_channels: int | None = Field(None, ge=1, le=8)
    device_id: str | None = Field(None, max_length=256)
    resampler: str | None = Field(None, max_length=128)
    client_version: str | None = Field(None, max_length=64)


class ChunkIn(Strict):
    type: Literal["chunk"]
    protocol_version: Literal[1]
    hospital_id: str | None = None
    session_id: str
    capture_epoch: int | str
    channel_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    seq: int = Field(ge=0, le=86400)
    sample_start: int = Field(ge=0, le=16000 * 7200)
    sample_count: int = Field(gt=0, le=160000)
    sample_rate: Literal[16000]
    encoding: Literal["pcm_s16le"]
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    data: str = Field(max_length=450000)
    monotonic_ticks: int | None = Field(None, ge=0)
    clock_uncertainty_ms: float | None = Field(None, ge=0, le=60000)
    capture_metadata: CaptureMetadata | None = None

    @field_validator("capture_epoch")
    @classmethod
    def epoch(cls, value):
        value = str(value)
        if not value or len(value) > 64 or not all(c.isalnum() or c in "-_" for c in value):
            raise ValueError("Invalid capture epoch")
        return value


class FinalChannel(Strict):
    channel_id: str
    capture_epoch: int | str
    last_seq: int = Field(ge=-1, le=86400)
    sample_end: int = Field(ge=0)


class FinalizeIn(Strict):
    channels: list[FinalChannel] = Field(default_factory=list, max_length=64)


class ReasonIn(Strict):
    reason: str = Field(min_length=3, max_length=2000)


class TranscriptIn(Strict):
    text: str = Field(min_length=1, max_length=20000)
    speaker: Literal["doctor", "patient", "family", "unknown"] = "doctor"
    subject: Literal["patient", "family", "other", "unknown"] = "patient"
    section: str = Field(default="history_present", max_length=64)


class TranscriptPatch(Strict):
    base_revision: int = Field(ge=1)
    text: str | None = Field(None, min_length=1, max_length=20000)
    speaker: Literal["doctor", "patient", "family", "unknown"] | None = None
    subject: Literal["patient", "family", "other", "unknown"] | None = None
    section: str | None = Field(None, max_length=64)


class FactPatch(Strict):
    base_revision: int = Field(ge=1)
    text: str | None = Field(None, min_length=1, max_length=20000)
    concept: str | None = Field(None, max_length=128)
    section: str | None = Field(None, max_length=64)
    subject: Literal["patient", "family", "other", "unknown"] | None = None
    speaker: Literal["doctor", "patient", "family", "unknown"] | None = None
    polarity: Literal["positive", "negative", "unknown"] | None = None
    certainty: Literal["certain", "uncertain", "suspected", "unknown"] | None = None
    elicitation: Literal["asked", "not_asked", "refused", "unverifiable", "not_applicable"] | None = None
    conflict_status: Literal["none", "unresolved", "resolved"] | None = None
    confirmation_status: Literal["unconfirmed", "confirmed", "excluded"] | None = None
    medication_status: Literal["current", "historical", "stopped", "unknown"] | None = None
    unit: str | None = Field(None, max_length=64)
    clinical_time: str | None = Field(None, max_length=128)
    resolution_reason: str | None = Field(None, min_length=3, max_length=2000)

    @field_validator("resolution_reason")
    @classmethod
    def meaningful_resolution_reason(cls, value):
        if value is None:
            return None
        if len(value.strip()) < 3:
            raise ValueError("Fact disposition requires a substantive reason")
        return value.strip()


class NoteIn(Strict):
    session_id: str
    note_type: Literal["admission", "first_progress", "daily_progress", "discharge"]


class ReferenceReviewIn(Strict):
    fact_revisions: dict[str, Annotated[int, Field(strict=True, ge=1)]] = Field(min_length=1, max_length=1000)
    reason: str = Field(min_length=3, max_length=2000)

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value):
        if len(value.strip()) < 3:
            raise ValueError("Reference review requires a substantive reason")
        return value.strip()


class BlockIn(Strict):
    key: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=128)
    text: str = Field(max_length=40000)
    fact_ids: list[str] = Field(default_factory=list, max_length=1000)
    author: str | None = None
    protected: bool | None = None
    reference_review: ReferenceReviewIn | None = None


class NotePatch(Strict):
    base_revision: int = Field(ge=1)
    blocks: list[BlockIn] = Field(min_length=1, max_length=40)


class SuggestionIn(Strict):
    base_revision: int = Field(ge=1)
    sections: list[str] = Field(default_factory=list, max_length=40)


class ResolutionIn(Strict):
    issue_id: str
    resolution: str = Field(min_length=3, max_length=2000)


class ReviewIn(Strict):
    base_revision: int = Field(ge=1)
    issue_resolutions: list[ResolutionIn] = Field(default_factory=list, max_length=2000)


class ExportIn(Strict):
    review_id: str
    idempotency_key: str = Field(min_length=8, max_length=128)


class AudioGap(Strict):
    session_id: str | None = None
    channel_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    capture_epoch: int | str
    seq: int = Field(ge=0, le=86400)
    sample_start: int = Field(ge=0)
    sample_count: int = Field(gt=0)
    reason: str = Field(min_length=3, max_length=1000)
    detected_at_utc: str | None = Field(None, max_length=64)


class AudioGapsIn(Strict):
    gaps: list[AudioGap] = Field(min_length=1, max_length=1000)
