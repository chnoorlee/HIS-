using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace HisVoice.Core;

public static class Wire
{
    public static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web)
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true
    };
}

public sealed record AudioChunk(
    string SessionId, string CaptureEpoch, string ChannelId, long Seq,
    long SampleStart, int SampleCount, string Sha256, string Data,
    long MonotonicTicks, double ClockUncertaintyMs, CaptureMetadata? CaptureMetadata = null)
{
    public string Type => "chunk";
    public int ProtocolVersion => 1;
    public int SampleRate => 16000;
    public string Encoding => "pcm_s16le";
    [JsonIgnore] public string Key => $"{SessionId}/{CaptureEpoch}/{ChannelId}/{Seq}";

    public static AudioChunk Create(string sessionId, string epoch, string channelId,
        long seq, long sampleStart, byte[] pcm, long monotonicTicks, double uncertaintyMs, CaptureMetadata? captureMetadata = null)
    {
        if (string.IsNullOrWhiteSpace(sessionId) || string.IsNullOrWhiteSpace(epoch) ||
            string.IsNullOrWhiteSpace(channelId) || seq < 0 || sampleStart < 0 || pcm.Length == 0 || pcm.Length % 2 != 0)
            throw new ArgumentException("Invalid PCM chunk coordinates.");
        return new(sessionId, epoch, channelId, seq, sampleStart, pcm.Length / 2,
            Convert.ToHexStringLower(SHA256.HashData(pcm)), Convert.ToBase64String(pcm),
            monotonicTicks, uncertaintyMs, captureMetadata);
    }

    public void Validate()
    {
        var data = Convert.FromBase64String(Data);
        if (data.Length != SampleCount * 2 || Seq < 0 || SampleStart < 0 ||
            !CryptographicOperations.FixedTimeEquals(SHA256.HashData(data), Convert.FromHexString(Sha256)))
            throw new InvalidDataException("Audio chunk integrity failure.");
    }
}

public sealed record ChannelEnd(string ChannelId, string CaptureEpoch, long LastSeq, long SampleEnd);
public sealed record CaptureMetadata(int InputSampleRate, int InputChannels, string DeviceId, string Resampler, string ClientVersion);
public sealed record SpoolRecord(AudioChunk Chunk, DateTimeOffset CapturedAtUtc);
public sealed record AudioGap(string SessionId, string ChannelId, string CaptureEpoch, long Seq,
    long SampleStart, int SampleCount, string Reason, DateTimeOffset DetectedAtUtc)
{
    [JsonIgnore] public string Key => $"{SessionId}/{CaptureEpoch}/{ChannelId}/{Seq}";
}
public sealed record StreamAck(string Type, string? ChannelId, string? CaptureEpoch, long? Seq,
    long? ContiguousSeq, string? Code, string? Message);
public sealed record DeviceChannel(string DeviceId, string ChannelId);

public sealed class StartRequest
{
    public string ApiBase { get; set; } = "";
    public string Token { get; set; } = "";
    public string BearerToken { get; set; } = "";
    public string SessionId { get; set; } = "";
    public List<DeviceChannel> Channels { get; set; } = [];
    public List<DeviceChannel> DeviceChannels { get; set; } = [];
    public void Normalize() => Normalize(true);
    public void Normalize(bool forCapture)
    {
        if (string.IsNullOrEmpty(Token)) Token = BearerToken;
        if (string.IsNullOrWhiteSpace(SessionId) || string.IsNullOrWhiteSpace(Token))
            throw new ArgumentException("Select an authenticated session.");
        if (!forCapture) { Channels = []; DeviceChannels = []; return; }
        if (Channels.Count == 0) Channels = DeviceChannels;
        if (string.IsNullOrWhiteSpace(SessionId) || string.IsNullOrWhiteSpace(Token) ||
            Channels.Count is < 1 or > 2 || Channels.Select(c => c.ChannelId).Distinct().Count() != Channels.Count ||
            Channels.Select(c => c.DeviceId).Distinct().Count() != Channels.Count ||
            Channels.Any(c => string.IsNullOrWhiteSpace(c.ChannelId) || string.IsNullOrWhiteSpace(c.DeviceId)))
            throw new ArgumentException("Select one or two different devices and channels for an authenticated session.");
    }
}

public sealed class CaptureSafetyGate(TimeProvider? timeProvider = null)
{
    private readonly TimeProvider clock = timeProvider ?? TimeProvider.System;
    private readonly object sync = new();
    private long lastAuthorized;
    private bool authorized;
    private bool revoked;
    public void Authorize() { lock (sync) { if (revoked) throw new InvalidOperationException("Session authorization was revoked."); lastAuthorized = clock.GetTimestamp(); authorized = true; } }
    public void Revoke() { lock (sync) revoked = true; }
    public string? StopReason(double pendingSeconds)
    {
        lock (sync)
        {
            if (revoked) return "authorization_revoked";
            if (!authorized || clock.GetElapsedTime(lastAuthorized) >= TimeSpan.FromSeconds(60)) return "authorization_expired";
            if (pendingSeconds >= 600) return "offline_buffer_full";
            return null;
        }
    }
}

public static class Quality
{
    public static (double Rms, double ClippingFraction, bool Silence) Measure(ReadOnlySpan<byte> pcm)
    {
        if (pcm.Length == 0 || pcm.Length % 2 != 0) return (0, 0, true);
        double squares = 0; int clipped = 0;
        for (int i = 0; i < pcm.Length; i += 2)
        {
            short value = System.Buffers.Binary.BinaryPrimitives.ReadInt16LittleEndian(pcm.Slice(i, 2));
            double normalized = value / 32768d;
            squares += normalized * normalized;
            if (Math.Abs(normalized) >= .99) clipped++;
        }
        double rms = Math.Sqrt(squares / (pcm.Length / 2));
        return (rms, clipped / (pcm.Length / 2d), rms < .005);
    }
}
