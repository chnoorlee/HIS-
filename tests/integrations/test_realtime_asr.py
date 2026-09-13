import asyncio
import hashlib
import json
import sys
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from integrations.asr import ASRError, AudioBlock, DashscopeConfig, DashscopeRealtime


def block(seq=0, start=0, channel="doctor", epoch="epoch-1"):
    pcm = b"\x01\x00" * 16000
    return AudioBlock(channel, epoch, seq, start, 16000, pcm, hashlib.sha256(pcm).hexdigest())


async def source(*blocks):
    for value in blocks:
        yield value


async def run_provider(blocks, sentences, *, failure=None):
    received = []

    async def handler(ws):
        begin = json.loads(await ws.recv())
        task = begin["header"]["task_id"]
        assert begin["payload"]["parameters"]["inverse_text_normalization_enabled"] is False
        if failure:
            await ws.send(json.dumps({"header": {"event": "task-failed", "task_id": task,
                                                "error_message": "sensitive provider diagnostic"}}))
            return
        await ws.send(json.dumps({"header": {"event": "task-started", "task_id": task}}))
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    received.append(message)
                    continue
                assert json.loads(message)["header"]["action"] == "finish-task"
                for sentence in sentences:
                    await ws.send(json.dumps({"header": {"event": "result-generated", "task_id": task},
                                              "payload": {"output": {"sentence": sentence}}}))
                await ws.send(json.dumps({"header": {"event": "task-finished", "task_id": task}}))
                return
        except Exception as exc:
            from websockets.exceptions import ConnectionClosed
            if not isinstance(exc, ConnectionClosed):
                raise

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        adapter = DashscopeRealtime(DashscopeConfig(f"ws://127.0.0.1:{port}", "test-only",
                                                     frozenset({"127.0.0.1"}), allow_loopback_test=True))
        output = [item async for item in adapter.recognize(source(*blocks))]
    return output, b"".join(received)


def test_partial_and_final_keep_replay_audio_coordinates():
    sentences = [{"begin_time": 100, "end_time": None, "text": "patient reports", "sentence_end": False},
                 {"begin_time": 100, "end_time": 700, "text": "patient reports pain", "sentence_end": True}]
    output, audio = asyncio.run(run_provider([block(seq=7, start=112000)], sentences))
    assert len(audio) == 32000
    assert [item.status for item in output] == ["partial", "final"]
    assert output[1].sample_start == 113600
    assert output[1].sample_end == 123200
    assert output[1].revision == 2
    assert output[0].segment_id == output[1].segment_id
    assert output[1].channel_id == "doctor"


def test_duplicate_final_is_deduplicated_by_range_not_text():
    sentence = {"begin_time": 0, "end_time": 100, "text": "same", "sentence_end": True}
    output, _ = asyncio.run(run_provider([block()], [sentence, sentence, {**sentence, "begin_time": 200, "end_time": 300}]))
    assert len(output) == 2


@pytest.mark.parametrize("second", [block(2, 16000), block(1, 16001), block(1, 16000, "patient"), block(1, 16000, epoch="epoch-2")])
def test_gap_or_changed_binding_aborts(second):
    with pytest.raises(ASRError, match="audio_gap_or_binding_changed"):
        asyncio.run(run_provider([block(), second], []))


def test_final_cannot_cite_audio_that_was_not_supplied():
    with pytest.raises(ASRError, match="provider_evidence_outside_audio"):
        asyncio.run(run_provider([block()], [{"begin_time": 0, "end_time": 1001, "text": "text", "sentence_end": True}]))


def test_provider_error_does_not_expose_diagnostics():
    with pytest.raises(ASRError) as error:
        asyncio.run(run_provider([block()], [], failure=True))
    assert str(error.value) == "provider_task_failed"
    assert "sensitive" not in repr(error.value)


def test_unapproved_or_insecure_provider_is_rejected_before_connection():
    for endpoint in ["wss://unapproved.example/", "ws://approved.example/", "wss://approved.example/?key=bad"]:
        with pytest.raises(ASRError):
            DashscopeRealtime(DashscopeConfig(endpoint, "secret", frozenset({"approved.example"})))


def test_tampered_source_pcm_is_rejected():
    value = block()
    bad = AudioBlock(value.channel_id, value.capture_epoch, value.seq, value.sample_start,
                     value.sample_count, b"\x00" * 32000, value.sha256)
    with pytest.raises(ASRError, match="audio_integrity_failed"):
        asyncio.run(run_provider([bad], []))
