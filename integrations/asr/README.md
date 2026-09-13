# Realtime ASR boundary

`DashscopeRealtime.recognize()` consumes contiguous, verified PCM blocks from durable storage and emits versioned partial/final results with the original channel, epoch and sample coordinates. It never chooses a clinical subject. A stream uses one provider task and one channel; resume starts a new run with an explicit replay offset. The caller must invalidate or reconcile earlier hypotheses by audio range before publishing replacement facts.

The adapter implements the official [WebSocket interaction](https://help.aliyun.com/zh/model-studio/websocket-for-paraformer-real-time-service), [client events](https://help.aliyun.com/zh/model-studio/paraformer-client-events) and [server events](https://help.aliyun.com/zh/model-studio/paraformer-server-events). The configured model and endpoint must be approved for the hospital. `paraformer-realtime-v2` is a protocol-compatible integration target, not a claim that it is the best current medical model. Selection still requires the noisy-room comparison in the trial document.

Local tests simulate the provider protocol and verify coordinates, failures, cancellation, missing chunks and duplicate final messages. They do not measure real transcription quality, cloud service availability or licensed medical terminology support. No credentials are included.
