using System.Net;
using System.Net.WebSockets;
using System.Text.Json;
using HisVoice.Core;
using HisVoice.Desktop;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Logging;
using NAudio.Wave;
using Xunit;

namespace HisVoice.Transport.Tests;

public sealed class AudioUploaderTests
{
    [Fact]
    public async Task ReceivedAckRetainsAudioUntilDurableAckArrives()
    {
        var received = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var sendDurable = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        await using var fixture = await GatewayFixture.StartAsync(async (socket, connection, ct) =>
        {
            var chunk = await GatewayFixture.ReadChunkAsync(socket, ct);
            await GatewayFixture.AckAsync(socket, chunk, "ACK_RECEIVED", ct); received.TrySetResult();
            await sendDurable.Task.WaitAsync(ct);
            await GatewayFixture.AckAsync(socket, chunk, "ACK_DURABLE", ct);
            await Task.Delay(Timeout.InfiniteTimeSpan, ct);
        });
        fixture.Uploader.Start();
        await received.Task.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Single(fixture.Spool.Pending("session-a"));
        sendDurable.TrySetResult();
        await fixture.WaitForAsync(() => fixture.Spool.Pending("session-a").Count == 0);
        Assert.Empty(fixture.Spool.Pending("session-a"));
    }

    [Fact]
    public async Task LostDurableAckReplaysSameChunkWithNewStreamTicket()
    {
        var hashes = new List<string>();
        await using var fixture = await GatewayFixture.StartAsync(async (socket, connection, ct) =>
        {
            var chunk = await GatewayFixture.ReadChunkAsync(socket, ct);
            lock (hashes) hashes.Add(chunk.Sha256);
            if (connection == 1)
                await socket.CloseAsync(WebSocketCloseStatus.EndpointUnavailable, "lost_ack_simulation", ct);
            else
            {
                await GatewayFixture.AckAsync(socket, chunk, "ACK_DURABLE", ct);
                await Task.Delay(Timeout.InfiniteTimeSpan, ct);
            }
        });
        fixture.Uploader.Start();
        await fixture.WaitForAsync(() => fixture.Spool.Pending("session-a").Count == 0);
        Assert.True(fixture.TicketCount >= 2);
        lock (hashes) { Assert.Equal(2, hashes.Count); Assert.Single(hashes.Distinct()); }
    }

    [Fact]
    public async Task QuarantineClosesUploadAndPreservesUnacknowledgedEvidence()
    {
        await using var fixture = await GatewayFixture.StartAsync(async (socket, connection, ct) =>
        {
            await GatewayFixture.ReadChunkAsync(socket, ct);
            await socket.SendAsync(JsonSerializer.SerializeToUtf8Bytes(new { type = "ERROR", code = "session_quarantined" }),
                WebSocketMessageType.Text, true, ct);
            await Task.Delay(Timeout.InfiniteTimeSpan, ct);
        });
        fixture.Uploader.Start();
        var reason = await fixture.Terminal.Task.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal("authorization_revoked", reason);
        Assert.Single(fixture.Spool.Pending("session-a"));
    }

    [Fact]
    public async Task HashConflictInManifestNeverDeletesLocalEvidence()
    {
        await using var fixture = await GatewayFixture.StartAsync((socket, connection, ct) => Task.CompletedTask, conflictManifest: true);
        fixture.Uploader.Start();
        var reason = await fixture.Terminal.Task.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal("audio_integrity_or_retention_incident", reason);
        Assert.Single(fixture.Spool.Pending("session-a"));
        Assert.Equal(0, fixture.TicketCount);
    }

    [Fact]
    public async Task ReconnectAfterResumeUsesUniqueChannelBindingsAcrossEpochs()
    {
        await using var fixture = await GatewayFixture.StartAsync(async (socket, _, ct) =>
        {
            for (int i = 0; i < 2; i++)
            {
                var chunk = await GatewayFixture.ReadChunkAsync(socket, ct);
                await GatewayFixture.AckAsync(socket, chunk, "ACK_DURABLE", ct);
            }
            await Task.Delay(Timeout.InfiniteTimeSpan, ct);
        });
        fixture.Spool.Append(AudioChunk.Create("session-a", "resumed-epoch", "doctor", 0, 0, new byte[32000], 0, 0), DateTimeOffset.UtcNow);
        fixture.Uploader.Start();
        await fixture.WaitForAsync(() => fixture.Spool.Pending("session-a").Count == 0);
        Assert.Equal(["doctor"], fixture.TicketChannels);
    }

    [Fact]
    public async Task FinalizedStatusBlocksNewCaptureBeforeTicketIssuance()
    {
        await using var fixture = await GatewayFixture.StartAsync((_, _, _) => Task.CompletedTask);
        fixture.SessionStatus = "COMPLETE";
        await Assert.ThrowsAsync<SessionClosedException>(() => fixture.Uploader.AuthorizeAsync(CancellationToken.None, forCapture: true));
        Assert.Equal(0, fixture.TicketCount);
        Assert.Single(fixture.Spool.Pending("session-a"));
    }

    [Fact]
    public async Task IncompleteSessionAllowsQueuedRecoveryButRejectsNewCapture()
    {
        await using var fixture = await GatewayFixture.StartAsync(async (socket, _, ct) =>
        {
            var chunk = await GatewayFixture.ReadChunkAsync(socket, ct);
            await GatewayFixture.AckAsync(socket, chunk, "ACK_DURABLE", ct);
            await Task.Delay(Timeout.InfiniteTimeSpan, ct);
        });
        fixture.SessionStatus = "INCOMPLETE";
        await Assert.ThrowsAsync<SessionClosedException>(() => fixture.Uploader.AuthorizeAsync(CancellationToken.None, forCapture: true));
        fixture.Uploader.Start();
        await fixture.WaitForAsync(() => fixture.Spool.Pending("session-a").Count == 0);
    }

    [Fact]
    public async Task GapLedgerOnlyClearsExplicitDurableCoordinates()
    {
        await using var fixture = await GatewayFixture.StartAsync((_, _, _) => Task.CompletedTask);
        fixture.Spool.RecordGap(AudioChunk.Create("session-a", "epoch-a", "doctor", 1, 16000, new byte[32000], 0, 0), "authorization_expired", DateTimeOffset.UtcNow);
        fixture.GapAcknowledgment = new { status = "ACK_DURABLE", session_id = "session-a", accepted = new[] { new { channel_id = "doctor", capture_epoch = "other-epoch", seq = 1 } } };
        await Assert.ThrowsAsync<JsonException>(() => fixture.Uploader.ReconcileGapsAsync(CancellationToken.None));
        Assert.Single(fixture.Spool.Gaps("session-a"));
        fixture.GapAcknowledgment = new { status = "ACK_RECEIVED", session_id = "session-a", accepted = new[] { new { channel_id = "doctor", capture_epoch = "epoch-a", seq = 1 } } };
        await Assert.ThrowsAsync<JsonException>(() => fixture.Uploader.ReconcileGapsAsync(CancellationToken.None));
        Assert.Single(fixture.Spool.Gaps("session-a"));
        fixture.GapAcknowledgment = new { status = "ACK_DURABLE", session_id = "session-a", accepted = new[] { new { channel_id = "doctor", capture_epoch = "epoch-a", seq = 1 } } };
        await fixture.Uploader.ReconcileGapsAsync(CancellationToken.None);
        Assert.Empty(fixture.Spool.Gaps("session-a"));
    }

    [Fact]
    public async Task CompletedSessionReconcilesLostAcknowledgmentBeforeLocalCleanup()
    {
        await using var fixture = await GatewayFixture.StartAsync((_, _, _) => Task.CompletedTask);
        fixture.SessionStatus = "COMPLETE";
        fixture.Manifest = ManifestFor(fixture, "AVAILABLE");
        fixture.Uploader.Start();
        Assert.Equal("session_finalized", await fixture.Terminal.Task.WaitAsync(TimeSpan.FromSeconds(5)));
        Assert.Empty(fixture.Spool.Pending("session-a"));
        Assert.Empty(fixture.Spool.SessionIds);
        Assert.Equal(0, fixture.TicketCount);
    }

    [Fact]
    public async Task UnavailableManifestObjectDoesNotDeleteLocalAudio()
    {
        var received = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        await using var fixture = await GatewayFixture.StartAsync(async (socket, _, ct) =>
        {
            await GatewayFixture.ReadChunkAsync(socket, ct);
            received.TrySetResult();
            await Task.Delay(Timeout.InfiniteTimeSpan, ct);
        });
        fixture.Manifest = ManifestFor(fixture, "EXPIRED");
        fixture.Uploader.Start();
        await received.Task.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Single(fixture.Spool.Pending("session-a"));
    }

    [Fact]
    public async Task ManifestSampleConflictPreservesLocalEvidence()
    {
        await using var fixture = await GatewayFixture.StartAsync((_, _, _) => Task.CompletedTask);
        fixture.Manifest = ManifestFor(fixture, "AVAILABLE", sampleStart: 1);
        fixture.Uploader.Start();
        Assert.Equal("audio_integrity_or_retention_incident", await fixture.Terminal.Task.WaitAsync(TimeSpan.FromSeconds(5)));
        Assert.Single(fixture.Spool.Pending("session-a"));
        Assert.Equal(0, fixture.TicketCount);
    }

    private static object ManifestFor(GatewayFixture fixture, string status, long sampleStart = 0) => new
    {
        session_id = "session-a",
        chunks = new[] { new { channel_id = "doctor", capture_epoch = "epoch-a", seq = 0,
            sha256 = fixture.Spool.Pending("session-a")[0].Chunk.Sha256, status, sample_start = sampleStart, sample_count = 16000 } }
    };

    [Theory]
    [InlineData("PAUSED")]
    [InlineData("INCOMPLETE")]
    [InlineData("FINALIZING")]
    public async Task ExplicitRecoveryUploadsOriginalEpochWithoutCaptureDevices(string status)
    {
        var uploaded = new TaskCompletionSource<AudioChunk>(TaskCreationOptions.RunContinuationsAsynchronously);
        await using var fixture = await GatewayFixture.StartAsync(async (socket, _, ct) =>
        {
            var chunk = await GatewayFixture.ReadChunkAsync(socket, ct);
            await GatewayFixture.AckAsync(socket, chunk, "ACK_DURABLE", ct);
            uploaded.TrySetResult(chunk);
            await Task.Delay(Timeout.InfiniteTimeSpan, ct);
        });
        fixture.SessionStatus = status;
        var original = fixture.Spool.Pending("session-a")[0].Chunk;
        await using var coordinator = fixture.CreateCoordinator();
        var result = JsonSerializer.SerializeToElement(await coordinator.RecoverAsync(fixture.RecoveryRequest()), Wire.Json);
        Assert.Equal("STOPPED", result.GetProperty("state").GetString());
        Assert.Equal(original, await uploaded.Task.WaitAsync(TimeSpan.FromSeconds(5)));
        await fixture.WaitForAsync(() => fixture.Spool.Pending("session-a").Count == 0);
        var end = Assert.Single(fixture.Spool.Ends("session-a"));
        Assert.Equal("epoch-a", end.CaptureEpoch);
        Assert.Equal(16000, end.SampleEnd);
        Assert.Equal(new[] { "doctor" }, fixture.TicketChannels);
        await coordinator.PauseAsync();
        await Assert.ThrowsAsync<InvalidOperationException>(() => coordinator.ResumeAsync());
    }

    [Fact]
    public async Task ExplicitCompletedRecoveryClearsConfirmedAudioAndRetainsFinalBoundary()
    {
        await using var fixture = await GatewayFixture.StartAsync((_, _, _) => Task.CompletedTask);
        fixture.SessionStatus = "COMPLETE";
        fixture.Manifest = ManifestFor(fixture, "AVAILABLE");
        await using var coordinator = fixture.CreateCoordinator();
        var result = JsonSerializer.SerializeToElement(await coordinator.RecoverAsync(fixture.RecoveryRequest()), Wire.Json);
        Assert.Equal("STOPPED", result.GetProperty("state").GetString());
        Assert.Equal("session_finalized", result.GetProperty("reason").GetString());
        Assert.Single(result.GetProperty("channels").EnumerateArray());
        Assert.Empty(fixture.Spool.SessionIds);
        Assert.Equal(0, fixture.TicketCount);
    }

    [Fact]
    public async Task ExplicitCompletedRecoveryRetainsUnconfirmedAudioForReview()
    {
        await using var fixture = await GatewayFixture.StartAsync((_, _, _) => Task.CompletedTask);
        fixture.SessionStatus = "COMPLETE";
        await using var coordinator = fixture.CreateCoordinator();
        var result = JsonSerializer.SerializeToElement(await coordinator.RecoverAsync(fixture.RecoveryRequest()), Wire.Json);
        Assert.Equal("STOPPED", result.GetProperty("state").GetString());
        Assert.Equal("recovery_requires_review", result.GetProperty("reason").GetString());
        Assert.Single(fixture.Spool.Pending("session-a"));
        Assert.Equal(0, fixture.TicketCount);
    }

    [Fact]
    public async Task ExplicitCompletedRecoveryRetainsInterruptedCaptureMarker()
    {
        await using var fixture = await GatewayFixture.StartAsync((_, _, _) => Task.CompletedTask);
        fixture.Spool.BeginCapture("session-a");
        fixture.SessionStatus = "COMPLETE";
        fixture.Manifest = ManifestFor(fixture, "AVAILABLE");
        await using var coordinator = fixture.CreateCoordinator((_, _, _) => throw new InvalidOperationException("Recovery must not create capture devices."));
        var result = JsonSerializer.SerializeToElement(await coordinator.RecoverAsync(fixture.RecoveryRequest()), Wire.Json);
        Assert.Equal("recovery_requires_review", result.GetProperty("reason").GetString());
        Assert.True(result.GetProperty("finalization_blocked").GetBoolean());
        Assert.Empty(fixture.Spool.Pending("session-a"));
        Assert.Single(fixture.Spool.SessionIds);
        await Assert.ThrowsAsync<InvalidOperationException>(coordinator.StopAsync);
        Assert.Equal(0, fixture.TicketCount);
    }

    [Fact]
    public async Task ExplicitRecoveryRechecksAuthorizationBeforeSendingAudio()
    {
        await using var fixture = await GatewayFixture.StartAsync((_, _, _) => Task.CompletedTask);
        fixture.DenyAuthorization = true;
        await using var coordinator = fixture.CreateCoordinator();
        await Assert.ThrowsAsync<UnauthorizedAccessException>(() => coordinator.RecoverAsync(fixture.RecoveryRequest()));
        Assert.Single(fixture.Spool.Pending("session-a"));
        Assert.Equal(0, fixture.TicketCount);
    }

    [Fact]
    public async Task ExplicitRecoveryRejectsSessionWithoutLocalState()
    {
        await using var fixture = await GatewayFixture.StartAsync((_, _, _) => Task.CompletedTask);
        await using var coordinator = fixture.CreateCoordinator();
        var request = fixture.RecoveryRequest(); request.SessionId = "other-session";
        await Assert.ThrowsAsync<InvalidOperationException>(() => coordinator.RecoverAsync(request));
        var result = JsonSerializer.SerializeToElement(coordinator.Status(), Wire.Json);
        Assert.Equal("IDLE", result.GetProperty("state").GetString());
        Assert.Single(fixture.Spool.Pending("session-a"));
    }

    [Fact]
    public async Task FinalFlushFailureBlocksRepeatedStopAndResume()
    {
        await using var fixture = await GatewayFixture.StartAsync((_, _, _) => Task.CompletedTask);
        using var input = new FinalFlushInput();
        await using var coordinator = fixture.CreateCoordinator((sessionId, channel, _) => new AudioDeviceCapture(
            input, sessionId, channel, _ => throw new IOException("Injected final flush failure."), (_, _) => { }));
        var request = fixture.RecoveryRequest(); request.Channels = [new("synthetic-device", "doctor")];
        await coordinator.StartAsync(request);
        input.Emit(new byte[8000]);
        await Assert.ThrowsAsync<InvalidOperationException>(() => coordinator.StopAsync());
        await Assert.ThrowsAsync<InvalidOperationException>(() => coordinator.StopAsync());
        await Assert.ThrowsAsync<InvalidOperationException>(() => coordinator.ResumeAsync());
        var result = JsonSerializer.SerializeToElement(coordinator.Status(), Wire.Json);
        Assert.True(result.GetProperty("finalization_blocked").GetBoolean());
        Assert.Equal("capture_stop_failed", result.GetProperty("reason").GetString());
    }

    [Fact]
    public async Task CleanFinalFlushClearsPersistentMarkerWithoutReportingLiveCaptureAsFailure()
    {
        await using var fixture = await GatewayFixture.StartAsync((_, _, _) => Task.CompletedTask);
        using var input = new FinalFlushInput();
        await using var coordinator = fixture.CreateCoordinator((sessionId, channel, onChunk) =>
            new AudioDeviceCapture(input, sessionId, channel, onChunk, (_, _) => { }));
        var request = fixture.RecoveryRequest(); request.Channels = [new("synthetic-device", "doctor")];
        var started = JsonSerializer.SerializeToElement(await coordinator.StartAsync(request), Wire.Json);
        Assert.True(fixture.Spool.HasUnfinishedCapture("session-a"));
        Assert.False(started.GetProperty("finalization_blocked").GetBoolean());
        input.Emit(new byte[8000]);
        var stopped = JsonSerializer.SerializeToElement(await coordinator.StopAsync(), Wire.Json);
        Assert.False(fixture.Spool.HasUnfinishedCapture("session-a"));
        Assert.False(stopped.GetProperty("finalization_blocked").GetBoolean());
        Assert.Contains(fixture.Spool.Ends("session-a"), end => end.SampleEnd == 4000);
    }

    private sealed class FinalFlushInput : IWaveIn
    {
        public WaveFormat WaveFormat { get; set; } = new(16000, 16, 1);
        public event EventHandler<WaveInEventArgs>? DataAvailable;
        public event EventHandler<StoppedEventArgs>? RecordingStopped;
        public void StartRecording() { }
        public void StopRecording() => RecordingStopped?.Invoke(this, new());
        public void Emit(byte[] data) => DataAvailable?.Invoke(this, new(data, data.Length));
        public void Dispose() { }
    }

    private sealed class GatewayFixture : IAsyncDisposable
    {
        private readonly WebApplication app;
        private readonly string directory;
        private int ticketCount;
        public int TicketCount => Volatile.Read(ref ticketCount);
        public EncryptedSpool Spool { get; }
        public AudioUploader Uploader { get; private set; } = null!;
        public TaskCompletionSource<string> Terminal { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public string SessionStatus { get; set; } = "RECORDING";
        public string[] TicketChannels { get; private set; } = [];
        public object GapAcknowledgment { get; set; } = new { status = "ACK_RECEIVED" };
        public object? Manifest { get; set; }
        public bool DenyAuthorization { get; set; }
        public StartRequest RecoveryRequest() => new() { ApiBase = app.Urls.Single() + "/api/v1", BearerToken = "synthetic-recovery-token", SessionId = "session-a" };
        public CaptureCoordinator CreateCoordinator(Func<string, DeviceChannel, Action<AudioChunk>, AudioDeviceCapture>? createCapture = null) =>
            new(new DesktopSettings { ApiBase = RecoveryRequest().ApiBase }, Spool, "test-device", (_, _) => { }, action => action(), createCapture);
        private GatewayFixture(WebApplication app)
        {
            this.app = app;
            directory = Path.Combine(Path.GetTempPath(), "his-voice-transport-tests", Guid.NewGuid().ToString("N"));
            Spool = new EncryptedSpool(directory);
            Spool.Append(AudioChunk.Create("session-a", "epoch-a", "doctor", 0, 0, new byte[32000], 0, 0), DateTimeOffset.UtcNow);
        }

        public static async Task<GatewayFixture> StartAsync(Func<WebSocket, int, CancellationToken, Task> connectionHandler, bool conflictManifest = false)
        {
            var builder = WebApplication.CreateBuilder();
            builder.Logging.ClearProviders();
            builder.WebHost.ConfigureKestrel(options => options.Listen(IPAddress.Loopback, 0));
            var app = builder.Build(); app.UseWebSockets();
            var fixture = new GatewayFixture(app);
            app.MapGet("/api/v1/sessions/session-a", () => fixture.DenyAuthorization
                ? Results.StatusCode(StatusCodes.Status403Forbidden) : Results.Json(new { status = fixture.SessionStatus }));
            app.MapGet("/api/v1/sessions/session-a/audio-manifest", () => Results.Json(fixture.Manifest ?? new
            {
                session_id = "session-a",
                chunks = conflictManifest ? new[] { new { channel_id = "doctor", capture_epoch = "epoch-a", seq = 0, sha256 = "conflicting", status = "AVAILABLE", sample_start = 0, sample_count = 16000 } } : []
            }));
            app.MapPost("/api/v1/sessions/session-a/audio-gaps", () => Results.Json(fixture.GapAcknowledgment));
            app.MapPost("/api/v1/sessions/session-a/stream-ticket", async (HttpContext context) =>
            {
                using var ticket = await JsonDocument.ParseAsync(context.Request.Body);
                fixture.TicketChannels = ticket.RootElement.GetProperty("channels").EnumerateArray()
                    .Select(c => c.ValueKind == JsonValueKind.String ? c.GetString()! : c.GetProperty("channel_id").GetString()!).ToArray();
                return fixture.TicketChannels.Distinct().Count() == fixture.TicketChannels.Length
                    ? Results.Json(new { ticket = Interlocked.Increment(ref fixture.ticketCount).ToString() })
                    : Results.BadRequest();
            });
            app.Map("/api/v1/audio", async context =>
            {
                if (!context.WebSockets.IsWebSocketRequest) { context.Response.StatusCode = 400; return; }
                using var socket = await context.WebSockets.AcceptWebSocketAsync();
                using var cancellation = CancellationTokenSource.CreateLinkedTokenSource(context.RequestAborted, app.Lifetime.ApplicationStopping);
                try { await connectionHandler(socket, int.Parse(context.Request.Query["ticket"].ToString()), cancellation.Token); }
                catch (OperationCanceledException) { }
                catch (WebSocketException) { }
            });
            await app.StartAsync();
            var request = new StartRequest { ApiBase = app.Urls.Single() + "/api/v1", Token = "synthetic-test-token", SessionId = "session-a", Channels = [new("device-a", "doctor")] };
            fixture.Uploader = new(request, "test-device", fixture.Spool, new CaptureSafetyGate(),
                () => fixture.Spool.Ends("session-a"), (_, _) => { }, reason => fixture.Terminal.TrySetResult(reason));
            return fixture;
        }

        public static async Task<AudioChunk> ReadChunkAsync(WebSocket socket, CancellationToken ct)
        {
            var buffer = new byte[65536];
            using var body = new MemoryStream();
            WebSocketReceiveResult result;
            do
            {
                result = await socket.ReceiveAsync(new ArraySegment<byte>(buffer), ct);
                body.Write(buffer, 0, result.Count);
            } while (!result.EndOfMessage);
            return JsonSerializer.Deserialize<AudioChunk>(body.ToArray(), Wire.Json) ?? throw new InvalidDataException("Missing test chunk.");
        }
        public static Task AckAsync(WebSocket socket, AudioChunk chunk, string type, CancellationToken ct) =>
            socket.SendAsync(JsonSerializer.SerializeToUtf8Bytes(new { type, channel_id = chunk.ChannelId, capture_epoch = chunk.CaptureEpoch, seq = chunk.Seq, contiguous_seq = chunk.Seq, gaps = Array.Empty<int>() }),
                WebSocketMessageType.Text, true, ct);
        public async Task WaitForAsync(Func<bool> predicate)
        {
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            while (!predicate()) await Task.Delay(25, timeout.Token);
        }
        public async ValueTask DisposeAsync()
        {
            await Uploader.DisposeAsync();
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            await app.StopAsync(timeout.Token); await app.DisposeAsync();
            Spool.Dispose(); Directory.Delete(directory, true);
        }
    }
}
