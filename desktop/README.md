# HIS Voice Windows capture client

This client hosts the shared doctor workspace in WebView2 and captures explicitly selected Windows WASAPI input devices through NAudio 2.2.1. It does not contain ASR or language-model credentials. The development UI and API must already be running.

## Run

Prerequisites: Windows 10/11, .NET 10 SDK for building, Microsoft Edge WebView2 Evergreen Runtime, and microphone permission for desktop applications.

```powershell
dotnet run --project desktop/HisVoice.Desktop
dotnet test desktop/HisVoice.Core.Tests
dotnet test desktop/HisVoice.Transport.Tests
./desktop/publish.ps1
```

The published executable is `desktop/artifacts/win-x64/HisVoice.Desktop.exe`. The publish is self-contained for .NET; WebView2 Runtime remains an operating-system prerequisite. Edit `HisVoice.Desktop/desktop.settings.json` before deployment. Production configuration requires `development: false`, an HTTPS hospital UI origin, and an HTTPS API base. The native bridge only accepts the configured UI origin and pins API requests to the configured API base. Development defaults are UI `http://127.0.0.1:5173` and API `http://127.0.0.1:8787/api/v1`.

## Capture and recovery

- Select one or two distinct input endpoints. The application never switches automatically to the Windows default input device. A device failure pauses the whole conversation.
- NAudio WDL resampling produces 16 kHz mono PCM per endpoint. Chunks include source sample rate, channel count, device identifier, resampler and client version. Each endpoint receives an independent capture epoch; devices are not assumed to share a hardware clock. Reported clock uncertainty compares callback timing with the endpoint sample clock and is a quality signal, not a synchronized timestamp guarantee. Stop waits for the final NAudio callback before flushing a partial chunk.
- One-second chunks are hashed and AES-256-GCM encrypted before upload. The encryption key is protected with Windows DPAPI for the current user. Payload and capture-end metadata are persisted atomically under `%LOCALAPPDATA%/HisVoice/Spool`.
- Only an exact `ACK_DURABLE`, or a session-bound `AVAILABLE` manifest entry with matching hash and sample coordinates, deletes a queued chunk. Received acknowledgments and contiguous watermarks alone never delete audio. Reconnection obtains a new single-use stream ticket and replays pending chunks idempotently.
- The server is queried for session authorization at least every 15 seconds. Failure to refresh stops recording when the prior authorization becomes 60 seconds old. Revocation pauses capture and clears the displayed clinical content. Windows lock and suspend also pause capture.
- The local backlog is limited to 10 minutes per logical channel. After 24 hours, an encrypted gap ledger is flushed before expired unacknowledged payloads are deleted. The next authorized connection reports missing ranges to the server through `audio-gaps`; failure to record those gaps blocks new capture. Expiration runs while the application is open and immediately after startup; the app cannot enforce a wall-clock deletion deadline while Windows or the application is shut down.
- `capture.stop` returns expected final sample positions, including recovered epochs. The UI must wait until `pending_chunks` and `pending_gaps` reach zero before requesting successful server finalization. Stopping locally does not imply upload or finalization succeeded.
- Before opening microphones, an encrypted unfinished-capture marker is committed to disk. A clean stop clears it only after the final flush. A failed final flush or an interrupted process leaves `finalization_blocked` set across restart, including when earlier chunks already received durable acknowledgments. Recovery retains that evidence and requires review before finalization or new capture for the same session.
- Closing the app keeps pending encrypted audio. `capture.recover` reauthorizes the same original session and uploads existing queued audio without opening microphones. It accepts paused, incomplete and finalizing sessions. For a complete session it only verifies the manifest, deleting exactly confirmed audio and retaining unresolved evidence for review. Recovery requires local queue or capture-boundary metadata; it cannot reassign audio to a different session. The native status exposes recoverable session identifiers; no patient names are stored in filenames. A different Windows user cannot decrypt another user's spool.

## Bridge

Web messages are `{type,request_id,payload}`. Supported commands: `devices.list`, `capture.start`, `capture.recover`, `capture.pause`, `capture.resume`, `capture.stop`, `capture.status`, `session.clear`.

```json
{"type":"capture.start","request_id":"r1","payload":{"api_base":"http://127.0.0.1:8787/api/v1","token":"USER_ACCESS_TOKEN","session_id":"SESSION_ID","channels":[{"device_id":"WINDOWS_ENDPOINT_ID","channel_id":"doctor"}]}}
{"type":"capture.recover","request_id":"r2","payload":{"api_base":"http://127.0.0.1:8787/api/v1","token":"USER_ACCESS_TOKEN","session_id":"SESSION_ID"}}
```

Responses are `bridge.result` or `bridge.error` with the original request ID. Events are `capture.state`, `capture.quality`, `capture.buffer`, `capture.error`, and `session.revoked`. No command exposes arbitrary filesystem access, shell execution, or navigation. Device/channel mapping is independent of clinical speaker/subject assignment.

## Verification limits

Core tests cover encryption and restart recovery, tamper rejection, duplicate coordinates, exact durable acknowledgment handling, authorization timeout, backlog cap, device mapping, and acoustic metrics. Transport tests use a local Kestrel/WebSocket server for ACK loss, ticket renewal, manifest conflicts, gap acknowledgment and explicit recovery. Controlled input tests verify resampling and final callback draining. These tests do not establish microphone hardware compatibility, device hot-unplug behavior, clinical noise robustness, or hospital acceptance. Physical dual-device capture, long recordings, actual microphone removal, workstation locking, network loss, and reviewed hospital deployment require device-level acceptance runs.

### Local verification on 2026-09-09

- Release tests: 19 core tests and 33 transport tests passed. Regressions include concurrent completion cleanup, persisted unfinished capture markers, completed-session recovery with unresolved capture evidence, clean final flushing, and a locked retention ledger inside the dispatched monitor callback.
- Release build passed with zero warnings and errors. `dotnet format --verify-no-changes --no-restore` passed for all four C# projects. `publish.ps1` produced the current self-contained Windows executable.
- The published executable was launched and its actual WebView2 bridge checked through a temporary local debugging connection: `capture.status` returned `IDLE`, `devices.list` enumerated one available endpoint, recovery of an unknown local session returned `bridge.error` and stayed idle, and navigation outside the configured UI origin was blocked. No microphone was opened. The process closed cleanly afterward. A startup screenshot is at `artifacts/native-startup.png`.
