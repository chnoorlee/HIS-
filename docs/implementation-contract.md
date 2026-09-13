# Implementation Contract

The Chinese architecture document at the repository root is normative. This file fixes integration boundaries for the initial implementation. Local development uses synthetic data, isolated local storage, and a clearly labelled demonstration provider. Production must reject demonstration identities/providers and missing secrets.

## Layout and runtime

- `backend/`: Python 3.12, FastAPI, SQLAlchemy, Alembic, PostgreSQL production / SQLite explicitly local development, persistent jobs and event outbox, encrypted audio storage, model adapters, mock/HTTP EMR connectors.
- `frontend/`: React, TypeScript, Vite, lucide-react, doctor workspace. API base `/api/v1`, development proxy to `http://127.0.0.1:8787`.
- `desktop/`: .NET 10 WPF, WebView2 and NAudio 2.2.1, native device capture, encrypted spool, desktop bridge.
- `scripts/`, `infra/`, `tests/`, `docs/`: bootstrap, deployment, system acceptance and operations.

## HTTP contract

All IDs are strings. JSON uses snake_case. Error response: `{ "detail": { "code": "revision_conflict", "message": "..." } }` (clients also handle FastAPI validation errors). Authentication: Bearer token, obtained in development by `POST /api/v1/auth/login {username,password}`. Development users: `doctor` / `Doctor123!`, `reviewer` / `Reviewer123!`, `admin` / `Admin123!`; disable this login in production. `GET /auth/me` returns user, roles, hospital. Store web token in sessionStorage, never a model key. Swagger/OpenAPI is the definitive evolving schema.

- `GET /health`, `GET /system/status`: health / mode, provider and connector readiness.
- `GET /encounters?search=`: authorized encounter list. `GET /encounters/{id}`: details.
- `GET /encounters/{id}/sources`, `POST /encounters/{id}/sources/refresh`: versioned hospital snapshots.
- `POST /sessions {encounter_id,mode}` with mode `dictation` or `conversation`; `GET /sessions?encounter_id=`, `GET /sessions/{id}`.
- `POST /sessions/{id}/pause`, `/resume`, `/finalize`, `/quarantine` for state transitions. Finalize includes `channels: [{channel_id,capture_epoch,last_seq,sample_end}]`.
- `POST /sessions/{id}/stream-ticket {device_id,channels}` returns ticket for `WS /api/v1/audio?ticket=...`. Ticket single-use, bound to session/user/device, reconnect fences old generation.
- WS JSON chunk: `{type:"chunk",protocol_version:1,session_id,capture_epoch,channel_id,seq,sample_start,sample_count,sample_rate:16000,encoding:"pcm_s16le",sha256,data:<base64>}`. Server replies `{type:"ACK_DURABLE",session_id,sha256,channel_id,capture_epoch,seq,contiguous_seq,gaps:[]}` only after object and DB commit. Exact session/epoch/channel/sequence/hash agreement is required before client cleanup. Other message `ERROR` with code/message.
- `GET /sessions/{id}/audio-manifest`, `GET /sessions/{id}/transcripts`, `GET /sessions/{id}/facts`, `GET /sessions/{id}/events` (authenticated fetch SSE).
- `POST /sessions/{id}/transcripts {text,speaker,subject}` is explicit physician manual text; development script loading is a separate endpoint, clearly synthetic.
- `PATCH /facts/{id} {base_revision,...corrections}`; source changes invalidate dependent review.
- `POST /notes {session_id,note_type}` creates a versioned draft (`admission`, `first_progress`, `daily_progress`, `discharge`). `GET /notes?encounter_id=`, `GET /notes/{id}`.
- `PATCH /notes/{id} {base_revision,blocks:[{key,title,text,fact_ids:[]}]}`. Immutable prior revisions, physician blocks protected.
- `POST /notes/{id}/suggestions {base_revision,sections:[]}` returns persistent job. `GET /jobs/{id}` and `GET /jobs`.
- `POST /notes/{id}/review {base_revision,issue_resolutions:[]}` only exact approved version; structured blockers returned. Missing clinical details require physician input, not automatic denial/normal findings.
- `POST /notes/{id}/exports {review_id,idempotency_key}`; `GET /exports/{id}`, `POST /exports/{id}/reconcile`. Mock connector selectable failure scenarios in development admin only; timeout after remote commit stays UNKNOWN until readback.
- `GET /audit`, `GET /admin/settings`, `PATCH /admin/settings`, `GET /admin/incidents`: restricted controls. Production secrets never returned.

## Desktop bridge

WebView messages use `{type,request_id,payload}`. Web sends `devices.list`, `capture.start` (API base, bearer token, immutable session_id, selected device/channel mapping), `capture.pause`, `capture.resume`, `capture.stop`, `capture.status`, `session.clear`. Native sends same request_id with `{type:"bridge.result",payload}` or `bridge.error`; events `capture.state`, `capture.quality`, `capture.buffer`, `capture.error`. Only explicitly configured HTTP loopback development or HTTPS hospital origin can use the bridge. Web fallback microphone capture must be labelled single-channel browser capture and use the same persistence protocol, with no claim of native offline durability.

## Ownership

Backend owner controls backend files and publishes actual OpenAPI/shape changes. Frontend owner controls frontend files. Desktop owner controls desktop files. Root owns infrastructure, launch scripts, cross-layer tests and documentation; coordinate before editing another owner's files. Everyone must preserve others' edits.
