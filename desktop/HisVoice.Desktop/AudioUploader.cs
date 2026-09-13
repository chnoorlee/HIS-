using System.Net;
using System.IO;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Net.WebSockets;
using System.Text.Json;
using HisVoice.Core;

namespace HisVoice.Desktop;

internal sealed class SessionClosedException : InvalidOperationException
{
    public SessionClosedException() : base("The session is finalized and cannot accept more audio.") { }
}

internal sealed class AudioUploader : IAsyncDisposable
{
    private readonly HttpClient http;
    private readonly StartRequest request;
    private readonly EncryptedSpool spool;
    private readonly CaptureSafetyGate safety;
    private readonly Action<string, object> publish;
    private readonly Action<string> terminal;
    private readonly CancellationTokenSource lifetime = new();
    private readonly string deviceId;
    private readonly Func<IReadOnlyList<ChannelEnd>> channelEnds;
    private Task? run;
    private Task? heartbeat;
    private bool captureClosed;
    public bool Connected { get; private set; }

    public AudioUploader(StartRequest request, string deviceId, EncryptedSpool spool, CaptureSafetyGate safety,
        Func<IReadOnlyList<ChannelEnd>> channelEnds, Action<string, object> publish, Action<string> terminal)
    {
        this.request = request; this.deviceId = deviceId; this.spool = spool; this.safety = safety;
        this.channelEnds = channelEnds; this.publish = publish; this.terminal = terminal;
        http = new HttpClient { BaseAddress = new Uri(request.ApiBase.TrimEnd('/') + "/"), Timeout = TimeSpan.FromSeconds(10) };
        http.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", request.Token);
    }

    public async Task AuthorizeAsync(CancellationToken cancellationToken, bool forCapture = false)
    {
        using var response = await http.GetAsync($"sessions/{Uri.EscapeDataString(request.SessionId)}", cancellationToken);
        await EnsureAuthorizedAsync(response, cancellationToken);
        using var session = await JsonDocument.ParseAsync(await response.Content.ReadAsStreamAsync(cancellationToken), cancellationToken: cancellationToken);
        var state = session.RootElement.TryGetProperty("status", out var value) ? value.GetString() :
            session.RootElement.TryGetProperty("state", out value) ? value.GetString() : null;
        if (state?.ToUpperInvariant() == "QUARANTINED") throw new UnauthorizedAccessException("Session is quarantined.");
        if (state?.ToUpperInvariant() == "COMPLETE")
        {
            if (!forCapture) await ReconcileManifestAsync(cancellationToken);
            throw new SessionClosedException();
        }
        if (forCapture && state?.ToUpperInvariant() is "INCOMPLETE" or "FINALIZING") throw new SessionClosedException();
        if (state?.ToUpperInvariant() is not ("CREATED" or "RECORDING" or "PAUSED" or "INCOMPLETE" or "FINALIZING"))
            throw new JsonException("Missing or unknown session status.");
        if (!captureClosed && state?.ToUpperInvariant() is "INCOMPLETE" or "FINALIZING")
        {
            captureClosed = true;
            terminal("session_finalized");
        }
        try { safety.Authorize(); }
        catch (InvalidOperationException ex) { throw new UnauthorizedAccessException("Capture authorization was revoked during refresh.", ex); }
    }

    public void Start() { run = UploadLoopAsync(lifetime.Token); heartbeat = HeartbeatLoopAsync(lifetime.Token); }

    public async Task<bool> AuthorizeRecoveryAsync(CancellationToken ct)
    {
        try { await AuthorizeAsync(ct); return true; }
        catch (SessionClosedException) { CompleteLocalRecovery(); return false; }
    }

    public async Task ReconcileGapsAsync(CancellationToken ct)
    {
        spool.Expire(DateTimeOffset.UtcNow);
        foreach (var gaps in spool.Gaps(request.SessionId).Chunk(1000))
        {
            using var response = await http.PostAsJsonAsync($"sessions/{request.SessionId}/audio-gaps", new { gaps }, Wire.Json, ct);
            await EnsureAuthorizedAsync(response, ct);
            using var acknowledgment = await JsonDocument.ParseAsync(await response.Content.ReadAsStreamAsync(ct), cancellationToken: ct);
            var body = acknowledgment.RootElement;
            if (!body.TryGetProperty("status", out var status) || status.GetString() != "ACK_DURABLE" ||
                !body.TryGetProperty("session_id", out var session) || session.GetString() != request.SessionId ||
                !body.TryGetProperty("accepted", out var accepted) || accepted.ValueKind != JsonValueKind.Array)
                throw new JsonException("Missing durable gap acknowledgment.");
            var acknowledged = accepted.EnumerateArray().Select(item =>
                $"{request.SessionId}/{item.GetProperty("capture_epoch").GetString()}/{item.GetProperty("channel_id").GetString()}/{item.GetProperty("seq").GetInt64()}").ToHashSet();
            var confirmed = gaps.Where(g => acknowledged.Contains(g.Key)).Select(g => g.Key).ToArray();
            spool.AcknowledgeGaps(request.SessionId, confirmed);
            if (confirmed.Length > 0)
                publish("capture.error", new { code = "audio_gap_reported", message = "Missing local audio ranges were recorded on the server.", gap_count = confirmed.Length });
            if (confirmed.Length != gaps.Length) throw new JsonException("Not all missing audio ranges were durably acknowledged.");
        }
    }

    private async Task HeartbeatLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try { await Task.Delay(TimeSpan.FromSeconds(15), ct); await AuthorizeAsync(ct); }
            catch (UnauthorizedAccessException) { safety.Revoke(); terminal("authorization_revoked"); lifetime.Cancel(); }
            catch (SessionClosedException) { CompleteLocalRecovery(); terminal("session_finalized"); lifetime.Cancel(); }
            catch (InvalidDataException) { terminal("audio_integrity_or_retention_incident"); lifetime.Cancel(); }
            catch (IOException) { terminal("local_audio_storage_failed"); lifetime.Cancel(); }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { return; }
            catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or JsonException)
            { publish("capture.error", new { code = "authorization_refresh_unavailable", message = "Authorization refresh failed; capture will pause after its existing grace period." }); }
        }
    }

    private async Task UploadLoopAsync(CancellationToken ct)
    {
        int failures = 0;
        while (!ct.IsCancellationRequested)
        {
            try
            {
                await AuthorizeAsync(ct);
                await ReconcileGapsAsync(ct);
                await ReconcileManifestAsync(ct);
                var channels = request.Channels.Select(x => x.ChannelId).Concat(channelEnds().Select(x => x.ChannelId)).Distinct().ToArray();
                using var ticketResponse = await http.PostAsJsonAsync($"sessions/{request.SessionId}/stream-ticket",
                    new { device_id = deviceId, channels }, Wire.Json, ct);
                await EnsureAuthorizedAsync(ticketResponse, ct);
                using var ticketBody = await JsonDocument.ParseAsync(await ticketResponse.Content.ReadAsStreamAsync(ct), cancellationToken: ct);
                string ticket = ticketBody.RootElement.GetProperty("ticket").GetString() ?? throw new JsonException("Missing stream ticket.");
                var uri = new UriBuilder(new Uri(http.BaseAddress!, "audio")) { Scheme = http.BaseAddress!.Scheme == "https" ? "wss" : "ws", Query = "ticket=" + Uri.EscapeDataString(ticket) };
                using var socket = new ClientWebSocket();
                socket.Options.KeepAliveInterval = TimeSpan.FromSeconds(15);
                await socket.ConnectAsync(uri.Uri, ct);
                Connected = true; failures = 0;
                using var connection = CancellationTokenSource.CreateLinkedTokenSource(ct);
                var receiver = ReceiveAsync(socket, connection.Token);
                try
                {
                    var sent = new Dictionary<string, DateTimeOffset>();
                    while (socket.State == WebSocketState.Open && !receiver.IsCompleted)
                    {
                        if (spool.Gaps(request.SessionId).Count > 0) await ReconcileGapsAsync(connection.Token);
                        var pending = spool.Pending(request.SessionId);
                        if (pending.Any(r => sent.TryGetValue(r.Chunk.Key, out var sentAt) && DateTimeOffset.UtcNow - sentAt > TimeSpan.FromSeconds(30)))
                            throw new WebSocketException("Durable acknowledgment timed out; reconnecting for exact reconciliation.");
                        foreach (var record in pending.Where(r => !sent.ContainsKey(r.Chunk.Key)))
                        {
                            if (DateTimeOffset.UtcNow - record.CapturedAtUtc >= TimeSpan.FromHours(24))
                            { terminal("unacknowledged_audio_expired"); throw new InvalidDataException("Expired audio requires explicit gap reconciliation."); }
                            var data = JsonSerializer.SerializeToUtf8Bytes(record.Chunk, Wire.Json);
                            using var sendTimeout = CancellationTokenSource.CreateLinkedTokenSource(connection.Token);
                            sendTimeout.CancelAfter(TimeSpan.FromSeconds(15));
                            await socket.SendAsync(data.AsMemory(), WebSocketMessageType.Text, true, sendTimeout.Token);
                            sent.Add(record.Chunk.Key, DateTimeOffset.UtcNow);
                        }
                        await Task.Delay(100, connection.Token);
                    }
                    await receiver;
                }
                finally
                {
                    connection.Cancel(); socket.Abort();
                    try { await receiver; } catch (OperationCanceledException) { } catch (WebSocketException) { }
                }
            }
            catch (UnauthorizedAccessException) { safety.Revoke(); terminal("authorization_revoked"); lifetime.Cancel(); }
            catch (SessionClosedException) { CompleteLocalRecovery(); terminal("session_finalized"); lifetime.Cancel(); }
            catch (InvalidDataException) { terminal("audio_integrity_or_retention_incident"); lifetime.Cancel(); }
            catch (IOException) { terminal("local_audio_storage_failed"); lifetime.Cancel(); }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { return; }
            catch (Exception ex) when (ex is HttpRequestException or WebSocketException or OperationCanceledException or JsonException)
            {
                failures++;
                publish("capture.error", new { code = "upload_disconnected", message = "Encrypted audio remains queued; reconnecting.", retry_attempt = failures });
            }
            finally { Connected = false; }
            try { await Task.Delay(TimeSpan.FromSeconds(Math.Min(15, Math.Pow(2, Math.Min(failures, 4)))), ct); }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { return; }
        }
    }

    private async Task ReconcileManifestAsync(CancellationToken ct)
    {
        using var response = await http.GetAsync($"sessions/{request.SessionId}/audio-manifest", ct);
        await EnsureAuthorizedAsync(response, ct);
        using var manifest = await JsonDocument.ParseAsync(await response.Content.ReadAsStreamAsync(ct), cancellationToken: ct);
        // A watermark without hash proof is insufficient for deletion; replaying exact chunks is idempotent.
        if (!manifest.RootElement.TryGetProperty("session_id", out var session) || session.GetString() != request.SessionId ||
            !manifest.RootElement.TryGetProperty("chunks", out var chunks) || chunks.ValueKind != JsonValueKind.Array)
            throw new JsonException("Missing session-bound audio manifest.");
        var pending = spool.Pending(request.SessionId).ToDictionary(x => x.Chunk.Key);
        foreach (var chunk in chunks.EnumerateArray())
        {
            if (!chunk.TryGetProperty("sha256", out var hash) || !chunk.TryGetProperty("channel_id", out var channel) ||
                !chunk.TryGetProperty("capture_epoch", out var epoch) || !chunk.TryGetProperty("seq", out var seq)) continue;
            string coordinate = $"{request.SessionId}/{epoch.GetString()}/{channel.GetString()}/{seq.GetInt64()}";
            if (!pending.TryGetValue(coordinate, out var local)) continue;
            if (hash.GetString() != local.Chunk.Sha256) throw new InvalidDataException("Server and local chunk hashes conflict.");
            if (!chunk.TryGetProperty("status", out var status) || status.GetString() != "AVAILABLE") continue;
            if (!chunk.TryGetProperty("sample_start", out var start) || start.GetInt64() != local.Chunk.SampleStart ||
                !chunk.TryGetProperty("sample_count", out var count) || count.GetInt32() != local.Chunk.SampleCount)
                throw new InvalidDataException("Server and local chunk sample coordinates conflict.");
            spool.Acknowledge(request.SessionId, new("ACK_DURABLE", channel.GetString(), epoch.GetString(), seq.GetInt64(), null, null, null));
        }
    }

    private async Task ReceiveAsync(ClientWebSocket socket, CancellationToken ct)
    {
        var buffer = new byte[8192];
        while (socket.State == WebSocketState.Open)
        {
            using var message = new MemoryStream();
            WebSocketReceiveResult result;
            do
            {
                result = await socket.ReceiveAsync(new ArraySegment<byte>(buffer), ct);
                if (result.MessageType == WebSocketMessageType.Close)
                {
                    if ((int?)result.CloseStatus is 4401 or 4403) throw new UnauthorizedAccessException("Server revoked the capture stream.");
                    return;
                }
                if (message.Length + result.Count > 65536) throw new InvalidDataException("Oversized audio acknowledgment.");
                message.Write(buffer, 0, result.Count);
            } while (!result.EndOfMessage);
            var ack = JsonSerializer.Deserialize<StreamAck>(message.ToArray(), Wire.Json) ?? throw new JsonException("Empty acknowledgment.");
            if (ack.Type == "ACK_DURABLE") spool.Acknowledge(request.SessionId, ack);
            else if (ack.Type is "ERROR" or "error")
            {
                if (ack.Code is "session_quarantined" or "patient_access_denied" or "authorization_revoked" or "session_not_writable")
                    throw new UnauthorizedAccessException("Server denied further capture.");
                if (ack.Code is "chunk_conflict" or "checksum_mismatch") throw new InvalidDataException("Audio content conflict.");
                throw new WebSocketException("Audio gateway rejected a chunk; reconnect and reconcile.");
            }
        }
    }

    private static async Task EnsureAuthorizedAsync(HttpResponseMessage response, CancellationToken ct)
    {
        if (response.StatusCode is HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden or HttpStatusCode.Gone)
            throw new UnauthorizedAccessException("Capture authorization denied.");
        if (response.StatusCode == HttpStatusCode.Conflict)
        {
            var body = await response.Content.ReadAsStringAsync(ct);
            if (body.Contains("quarantin", StringComparison.OrdinalIgnoreCase)) throw new UnauthorizedAccessException("Session quarantined.");
        }
        response.EnsureSuccessStatusCode();
    }
    private void CompleteLocalRecovery()
    {
        spool.TryCompleteSession(request.SessionId);
    }
    public async ValueTask DisposeAsync()
    {
        lifetime.Cancel();
        try { await Task.WhenAll(run ?? Task.CompletedTask, heartbeat ?? Task.CompletedTask); }
        catch (OperationCanceledException) { }
        http.Dispose(); lifetime.Dispose();
    }
}
