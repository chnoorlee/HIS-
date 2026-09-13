"""DashScope duplex ASR for contiguous, already-durable PCM evidence.

Wire reference: https://help.aliyun.com/zh/model-studio/paraformer-client-events
and https://help.aliyun.com/zh/model-studio/paraformer-server-events .
Clinical subject and speaker assignment deliberately remain downstream decisions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake


class ASRError(Exception):
    def __init__(self, code: str, *, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class AudioBlock:
    channel_id: str
    capture_epoch: str
    seq: int
    sample_start: int
    sample_count: int
    pcm: bytes = field(repr=False)
    sha256: str
    sample_rate: int = 16000

    def validate(self) -> None:
        if self.sample_rate != 16000 or self.sample_count <= 0 or self.seq < 0 or self.sample_start < 0:
            raise ASRError("audio_coordinates_invalid")
        if len(self.pcm) != self.sample_count * 2 or len(self.pcm) > 320000:
            raise ASRError("audio_length_invalid")
        if hashlib.sha256(self.pcm).hexdigest() != self.sha256:
            raise ASRError("audio_integrity_failed")


@dataclass(frozen=True)
class ASRUpdate:
    run_id: str
    segment_id: str
    revision: int
    channel_id: str
    capture_epoch: str
    sample_start: int
    sample_end: int | None
    text: str
    status: str
    provider: str = "dashscope_realtime"
    model: str = ""


@dataclass(frozen=True)
class DashscopeConfig:
    endpoint: str
    api_key: str = field(repr=False)
    approved_hosts: frozenset[str]
    model: str = "paraformer-realtime-v2"
    timeout_seconds: float = 30
    allow_loopback_test: bool = False

    def validate(self) -> None:
        url = urlsplit(self.endpoint)
        loopback = self.allow_loopback_test and url.hostname in {"127.0.0.1", "localhost", "::1"}
        if url.hostname not in self.approved_hosts or url.username or url.password or url.query or url.fragment:
            raise ASRError("provider_endpoint_not_approved")
        if url.scheme != "wss" and not (loopback and url.scheme == "ws"):
            raise ASRError("provider_requires_tls")
        if not self.api_key or not self.model or not 0 < self.timeout_seconds <= 120:
            raise ASRError("provider_configuration_invalid")


class ApprovedEndpointConnect(connect):
    def process_redirect(self, exc):
        # Redirecting a bearer-authenticated medical stream to another host is forbidden.
        return exc


class DashscopeRealtime:
    def __init__(self, config: DashscopeConfig, connector: Callable = ApprovedEndpointConnect):
        config.validate()
        self.config = config
        self.connector = connector

    async def recognize(self, audio: AsyncIterable[AudioBlock]) -> AsyncIterator[ASRUpdate]:
        iterator = aiter(audio)
        first = await anext(iterator, None)
        if first is None:
            return
        first.validate()
        run_id = uuid.uuid4().hex
        sent_end = first.sample_start
        base_sample = first.sample_start
        revisions: dict[int, int] = {}
        finalized: dict[tuple[int, int], str] = {}
        sender = None
        receive = None
        try:
            async with self.connector(
                self.config.endpoint,
                additional_headers={"Authorization": f"Bearer {self.config.api_key}"},
                open_timeout=self.config.timeout_seconds,
                max_size=2**20,
                proxy=None,
                ping_interval=20,
                ping_timeout=20,
            ) as socket:
                await socket.send(json.dumps({
                    "header": {"action": "run-task", "task_id": run_id, "streaming": "duplex"},
                    "payload": {
                        "task_group": "audio", "task": "asr", "function": "recognition",
                        "model": self.config.model,
                        "parameters": {"format": "pcm", "sample_rate": 16000, "heartbeat": True,
                                       "inverse_text_normalization_enabled": False},
                        "input": {},
                    },
                }))
                started = self._parse(await asyncio.wait_for(socket.recv(), self.config.timeout_seconds), run_id)
                if started["header"].get("event") != "task-started":
                    raise ASRError("provider_start_not_acknowledged")

                async def send_audio():
                    nonlocal sent_end
                    expected_seq = first.seq
                    block = first
                    while block is not None:
                        block.validate()
                        if (block.channel_id != first.channel_id or block.capture_epoch != first.capture_epoch
                                or block.seq != expected_seq or block.sample_start != sent_end):
                            raise ASRError("audio_gap_or_binding_changed")
                        # Sent coordinates are recorded before yielding to the receiver; the caller
                        # guarantees the entire block was durable before it entered this iterator.
                        sent_end = block.sample_start + block.sample_count
                        for offset in range(0, len(block.pcm), 3200):
                            await socket.send(block.pcm[offset:offset + 3200])
                        expected_seq += 1
                        block = await anext(iterator, None)
                    await socket.send(json.dumps({
                        "header": {"action": "finish-task", "task_id": run_id, "streaming": "duplex"},
                        "payload": {"input": {}},
                    }))

                sender = asyncio.create_task(send_audio())
                while True:
                    receive = asyncio.create_task(socket.recv())
                    if not sender.done():
                        done, _ = await asyncio.wait({sender, receive}, timeout=self.config.timeout_seconds,
                                                     return_when=asyncio.FIRST_COMPLETED)
                        if not done:
                            raise ASRError("provider_timeout", retryable=True)
                        if sender in done:
                            await sender
                    message = await asyncio.wait_for(receive, self.config.timeout_seconds)
                    data = self._parse(message, run_id)
                    event = data["header"].get("event")
                    if event == "task-finished":
                        if not sender.done():
                            raise ASRError("provider_finished_before_audio")
                        await sender
                        return
                    if event != "result-generated":
                        continue
                    sentence = data.get("payload", {}).get("output", {}).get("sentence")
                    if not isinstance(sentence, dict):
                        raise ASRError("provider_result_invalid")
                    if sentence.get("heartbeat"):
                        continue
                    result_text = sentence.get("text")
                    if not isinstance(result_text, str) or not result_text.strip():
                        continue
                    begin = self._milliseconds(sentence.get("begin_time"))
                    end_value = sentence.get("end_time")
                    if end_value is None:
                        word_ends = [word.get("end_time") for word in sentence.get("words", [])
                                     if isinstance(word, dict) and word.get("end_time") is not None]
                        end_value = max(word_ends) if word_ends else None
                    final = sentence.get("sentence_end") is True
                    end = self._milliseconds(end_value) if end_value is not None else None
                    sample_start = base_sample + begin * 16
                    sample_end = base_sample + end * 16 if end is not None else None
                    if sample_start > sent_end or (sample_end is not None and not sample_start <= sample_end <= sent_end):
                        raise ASRError("provider_evidence_outside_audio")
                    if final and (sample_end is None or sample_end == sample_start):
                        raise ASRError("provider_final_without_evidence_range")
                    if final:
                        key = (sample_start, sample_end)
                        if key in finalized:
                            if finalized[key] == result_text:
                                continue
                            raise ASRError("provider_conflicting_final")
                        finalized[key] = result_text
                    revisions[begin] = revisions.get(begin, 0) + 1
                    yield ASRUpdate(run_id, f"{run_id}:{begin}", revisions[begin], first.channel_id,
                                    first.capture_epoch, sample_start, sample_end, result_text,
                                    "final" if final else "partial", model=self.config.model)
        except ASRError:
            raise
        except (TimeoutError, ConnectionClosed, OSError) as exc:
            raise ASRError("provider_connection_lost", retryable=True) from exc
        except InvalidHandshake as exc:
            raise ASRError("provider_handshake_failed") from exc
        finally:
            for task in (sender, receive):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (sender, receive) if task is not None), return_exceptions=True)
            close = getattr(iterator, "aclose", None)
            if close:
                await close()

    @staticmethod
    def _milliseconds(value) -> int:
        if type(value) is not int or value < 0:
            raise ASRError("provider_timestamp_invalid")
        return value

    @staticmethod
    def _parse(message, run_id: str) -> dict:
        try:
            data = json.loads(message)
            header = data["header"]
            if not isinstance(header, dict) or header.get("task_id") != run_id:
                raise ASRError("provider_task_mismatch")
            if header.get("event") == "task-failed":
                # Provider diagnostic text can contain submitted content or credentials.
                raise ASRError("provider_task_failed")
            return data
        except (TypeError, ValueError, KeyError) as exc:
            raise ASRError("provider_message_invalid") from exc
