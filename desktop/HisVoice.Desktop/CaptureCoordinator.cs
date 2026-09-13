using System.IO;
using HisVoice.Core;

namespace HisVoice.Desktop;

internal sealed class CaptureCoordinator : IAsyncDisposable
{
    private readonly DesktopSettings settings;
    private readonly EncryptedSpool spool;
    private readonly Action<string, object> publish;
    private readonly Action<Action> dispatch;
    private readonly string deviceId;
    private readonly List<AudioDeviceCapture> captures = [];
    private readonly Func<string, DeviceChannel, Action<AudioChunk>, AudioDeviceCapture> createCapture;
    private readonly HashSet<string> failedStops = [];
    private ChannelEnd[] knownEpochs = [];
    private readonly SemaphoreSlim operations = new(1, 1);
    private readonly CancellationTokenSource lifetime = new();
    private readonly Task monitor;
    private CaptureSafetyGate safety = new();
    private StartRequest? request;
    private AudioUploader? uploader;
    private string state = "IDLE";
    private string? reason;
    private long generation;
    private bool recoveryOnly;

    public CaptureCoordinator(DesktopSettings settings, EncryptedSpool spool, string deviceId,
        Action<string, object> publish, Action<Action> dispatch,
        Func<string, DeviceChannel, Action<AudioChunk>, AudioDeviceCapture>? createCapture = null)
    {
        this.settings = settings; this.spool = spool; this.deviceId = deviceId; this.publish = publish; this.dispatch = dispatch;
        this.createCapture = createCapture ?? ((sessionId, channel, onChunk) => new(sessionId, channel, onChunk, publish));
        spool.Expire(DateTimeOffset.UtcNow);
        monitor = MonitorAsync(lifetime.Token);
    }

    public Task<object> StartAsync(StartRequest next) => BeginAsync(next, false);
    public Task<object> RecoverAsync(StartRequest next) => BeginAsync(next, true);

    private async Task<object> BeginAsync(StartRequest next, bool recoverOnly)
    {
        next.Normalize(forCapture: !recoverOnly);
        if (string.IsNullOrWhiteSpace(next.ApiBase)) next.ApiBase = settings.ApiBase;
        settings.ValidateApi(next.ApiBase);
        await operations.WaitAsync();
        bool replacing = false;
        try
        {
            if (state is "RECORDING" or "STARTING") throw new InvalidOperationException("A capture session is already active.");
            if (recoverOnly && !spool.SessionIds.Contains(next.SessionId))
                throw new InvalidOperationException("No local audio or capture boundary exists for this session.");
            if (!recoverOnly && (failedStops.Contains(next.SessionId) || spool.HasUnfinishedCapture(next.SessionId)))
                throw new InvalidOperationException("A previous capture stop failed; review the missing final audio before recording again.");
            if (request is not null && request.SessionId != next.SessionId &&
                (spool.Pending(request.SessionId).Count > 0 || spool.Gaps(request.SessionId).Count > 0))
                throw new InvalidOperationException("Previous session audio is still queued; complete its upload before changing patients.");
            replacing = true;
            var activeGeneration = ++generation;
            if (uploader is not null) await uploader.DisposeAsync();
            request = next; knownEpochs = spool.Ends(next.SessionId).ToArray(); safety = new(); state = "STARTING"; reason = null;
            recoveryOnly = recoverOnly;
            uploader = new(next, deviceId, spool, safety, Ends, publish, failure => Terminal(failure, activeGeneration));
            if (recoverOnly)
            {
                if (await uploader.AuthorizeRecoveryAsync(lifetime.Token))
                {
                    await uploader.ReconcileGapsAsync(lifetime.Token);
                    uploader.Start();
                }
                else
                {
                    reason = spool.Pending(next.SessionId).Count == 0 && spool.Gaps(next.SessionId).Count == 0 &&
                        !spool.HasUnfinishedCapture(next.SessionId)
                        ? "session_finalized" : "recovery_requires_review";
                    await uploader.DisposeAsync(); uploader = null;
                }
                state = "STOPPED"; PublishState(); return Status();
            }
            await uploader.AuthorizeAsync(lifetime.Token, forCapture: true);
            await uploader.ReconcileGapsAsync(lifetime.Token);
            CreateCaptures();
            spool.BeginCapture(next.SessionId);
            uploader.Start();
            foreach (var capture in captures) capture.Start();
            state = "RECORDING"; PublishState(); return Status();
        }
        catch (Exception ex) when (replacing)
        {
            await StopDevicesAsync(); state = recoverOnly ? "STOPPED" : "PAUSED";
            reason = ex is UnauthorizedAccessException ? "authorization_revoked" : recoverOnly ? "recovery_failed" : "capture_start_failed";
            PublishState();
            if (uploader is not null) { await uploader.DisposeAsync(); uploader = null; }
            if (ex is UnauthorizedAccessException)
            {
                safety.Revoke();
                publish("session.revoked", new { session_id = next.SessionId, clear_content = true });
            }
            throw;
        }
        finally { operations.Release(); }
    }

    private void CreateCaptures()
    {
        if (request is null) throw new InvalidOperationException("No selected session.");
        var activeGeneration = generation;
        foreach (var channel in request.Channels)
        {
            var capture = createCapture(request.SessionId, channel, chunk =>
            {
                string? stop = safety.StopReason(PendingSeconds());
                if (stop is not null)
                {
                    spool.RecordGap(chunk, stop, DateTimeOffset.UtcNow);
                    Terminal(stop, activeGeneration);
                    return;
                }
                spool.Append(chunk, DateTimeOffset.UtcNow);
            });
            capture.Failed += _ => Terminal("device_disconnected_or_capture_failed", activeGeneration);
            captures.Add(capture);
        }
        knownEpochs = knownEpochs.Concat(captures.Select(c => c.End)).ToArray();
    }

    public async Task<object> PauseAsync(string pauseReason = "physician_paused")
    {
        await operations.WaitAsync();
        try { await StopDevicesAsync(); if (request is not null) state = "PAUSED"; reason = pauseReason; PublishState(); return Status(); }
        finally { operations.Release(); }
    }
    public async Task<object> ResumeAsync()
    {
        await operations.WaitAsync();
        bool resuming = false;
        try
        {
            if (request is null || uploader is null || state != "PAUSED" || recoveryOnly) throw new InvalidOperationException("Select an authorized capture session before resuming.");
            if (failedStops.Contains(request.SessionId)) throw new InvalidOperationException("Review the failed final audio save before resuming.");
            if (reason is "authorization_revoked" or "session_quarantined" or "session_finalized" or
                "audio_integrity_or_retention_incident" or "unacknowledged_audio_expired" or "local_audio_storage_failed")
                throw new InvalidOperationException("This session requires recovery or a new authorization before capture can restart.");
            resuming = true;
            await uploader.AuthorizeAsync(lifetime.Token, forCapture: true);
            if (safety.StopReason(PendingSeconds()) is { } stop) throw new InvalidOperationException(stop);
            CreateCaptures();
            spool.BeginCapture(request.SessionId);
            foreach (var capture in captures) capture.Start();
            state = "RECORDING"; reason = null; PublishState(); return Status();
        }
        catch when (resuming) { await StopDevicesAsync(); state = "PAUSED"; throw; }
        finally { operations.Release(); }
    }
    public async Task<object> StopAsync()
    {
        await operations.WaitAsync();
        try
        {
            await StopDevicesAsync(); state = request is null ? "IDLE" : "STOPPED"; reason = null;
            if (request is not null && (failedStops.Contains(request.SessionId) || spool.HasUnfinishedCapture(request.SessionId)))
            {
                state = "PAUSED"; reason = "capture_stop_failed"; PublishState();
                throw new InvalidOperationException("Final audio could not be saved. Review the capture failure before finalizing this session.");
            }
            // Keep the uploader alive until durable ACKs arrive; stop is not a successful server finalization.
            PublishState(); return Status();
        }
        finally { operations.Release(); }
    }
    public async Task<object> ClearAsync()
    {
        await operations.WaitAsync();
        try
        {
            ++generation;
            await StopDevicesAsync(); safety.Revoke();
            if (uploader is not null) { await uploader.DisposeAsync(); uploader = null; }
            request = null; recoveryOnly = false; state = "IDLE"; reason = null; PublishState();
            return new { cleared = true, queued_sessions = spool.SessionIds.Count };
        }
        finally { operations.Release(); }
    }
    private async Task StopDevicesAsync()
    {
        bool hadCaptures = captures.Count > 0;
        foreach (var capture in captures)
        {
            try
            {
                try { await capture.StopAsync(); }
                finally { capture.Dispose(); }
            }
            catch (Exception)
            {
                if (request is not null) failedStops.Add(request.SessionId);
                publish("capture.error", new { code = "capture_stop_failed", message = "Capture stopped with a possible final audio gap; review the session manifest." });
            }
        }
        captures.Clear();
        if (hadCaptures && request is not null && !failedStops.Contains(request.SessionId))
        {
            try { spool.CompleteCapture(request.SessionId); }
            catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
            {
                failedStops.Add(request.SessionId);
                publish("capture.error", new { code = "capture_stop_failed", message = "The final capture boundary could not be committed; review the session before finalizing." });
            }
        }
    }
    private IReadOnlyList<ChannelEnd> Ends()
    {
        if (request is null) return [];
        return spool.Ends(request.SessionId).Concat(knownEpochs)
            .GroupBy(x => new { x.ChannelId, x.CaptureEpoch }).Select(g => g.MaxBy(x => x.LastSeq)!).ToArray();
    }
    private double PendingSeconds() => request is null ? 0 : spool.Pending(request.SessionId)
        .GroupBy(r => r.Chunk.ChannelId).Select(g => g.Sum(r => r.Chunk.SampleCount) / 16000d).DefaultIfEmpty().Max();
    public object Status() => new
    {
        state,
        reason,
        session_id = request?.SessionId,
        channels = Ends(),
        pending_chunks = request is null ? 0 : spool.Pending(request.SessionId).Count,
        pending_seconds = PendingSeconds(),
        connected = uploader?.Connected ?? false,
        pending_gaps = request is null ? 0 : spool.Gaps(request.SessionId).Count,
        finalization_blocked = request is not null && (failedStops.Contains(request.SessionId) ||
            (state is not ("STARTING" or "RECORDING") && spool.HasUnfinishedCapture(request.SessionId))),
        recoverable_sessions = spool.SessionIds,
        synchronized_clock = false
    };
    private void PublishState() => publish("capture.state", Status());
    private void Terminal(string failure, long activeGeneration) => dispatch(() => _ = HandleTerminalAsync(failure, activeGeneration));
    private async Task HandleTerminalAsync(string failure, long activeGeneration)
    {
        await operations.WaitAsync();
        try
        {
            if (generation != activeGeneration || request is null) return;
            await StopDevicesAsync(); state = recoveryOnly ? "STOPPED" : "PAUSED"; reason = failure; PublishState();
            if (recoveryOnly && failure == "session_finalized") return;
            publish("capture.error", new { code = failure, message = "Capture has paused. Check authorization, the selected device, and pending audio before resuming." });
            if (failure is "authorization_revoked" or "session_quarantined")
                publish("session.revoked", new { session_id = request.SessionId, clear_content = true });
        }
        finally { operations.Release(); }
    }
    private async Task MonitorAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try { await Task.Delay(1000, ct); }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { return; }
            dispatch(MonitorTick);
        }
    }
    private void MonitorTick()
    {
        try
        {
            var expired = spool.Expire(DateTimeOffset.UtcNow);
            if (expired.Count > 0) publish("capture.error", new { code = "local_retention_expired", message = "Expired queued audio was deleted after recording its missing ranges.", gap_count = expired.Count });
            if (request is null) return;
            var pending = spool.Pending(request.SessionId);
            publish("capture.buffer", new
            {
                pending_chunks = pending.Count,
                pending_seconds = PendingSeconds(),
                oldest_age_seconds = pending.Count == 0 ? 0 : (DateTimeOffset.UtcNow - pending[0].CapturedAtUtc).TotalSeconds
            });
            if (state == "RECORDING" && safety.StopReason(PendingSeconds()) is { } stop) Terminal(stop, generation);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            safety.Revoke();
            if (request is not null) Terminal("local_audio_storage_failed", generation);
            else publish("capture.error", new { code = "local_audio_storage_failed", message = "Local audio retention failed; resolve the storage problem before recording." });
        }
    }
    public async ValueTask DisposeAsync()
    {
        await ClearAsync(); lifetime.Cancel(); await monitor; lifetime.Dispose(); operations.Dispose();
    }
}
