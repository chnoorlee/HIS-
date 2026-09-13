using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using HisVoice.Core;
using Xunit;

namespace HisVoice.Core.Tests;

public sealed class CaptureProtocolTests : IDisposable
{
    private readonly string directory = Path.Combine(Path.GetTempPath(), "his-voice-tests", Guid.NewGuid().ToString("N"));
    private static AudioChunk Chunk(long seq = 0, string session = "session-a", string channel = "doctor", byte[]? pcm = null)
        => AudioChunk.Create(session, "epoch-a", channel, seq, seq * 16000, pcm ?? new byte[32000], 123456, 8.5);

    [Fact]
    public void ChunkUsesStableCoordinatesAndSha256()
    {
        var chunk = Chunk(); chunk.Validate();
        using var json = JsonDocument.Parse(JsonSerializer.Serialize(chunk, Wire.Json));
        Assert.Equal("pcm_s16le", json.RootElement.GetProperty("encoding").GetString());
        Assert.Equal(16000, json.RootElement.GetProperty("sample_count").GetInt32());
        Assert.Equal(0, json.RootElement.GetProperty("seq").GetInt64());
        Assert.False(json.RootElement.TryGetProperty("key", out _));
    }
    [Fact]
    public void RejectsMutatedAudioHash()
    {
        var chunk = Chunk() with { Data = Convert.ToBase64String(Enumerable.Repeat((byte)1, 32000).ToArray()) };
        Assert.Throws<InvalidDataException>(chunk.Validate);
    }
    [Fact]
    public void DurableAckOnlyClearsExactChannelAndEpoch()
    {
        using var spool = new EncryptedSpool(directory);
        spool.Append(Chunk(), DateTimeOffset.UtcNow);
        spool.Append(Chunk(channel: "patient"), DateTimeOffset.UtcNow);
        Assert.False(spool.Acknowledge("session-a", new("ACK_RECEIVED", "doctor", "epoch-a", 0, 0, null, null)));
        Assert.False(spool.Acknowledge("session-a", new("ACK_DURABLE", "doctor", "other-epoch", 0, 0, null, null)));
        Assert.True(spool.Acknowledge("session-a", new("ACK_DURABLE", "doctor", "epoch-a", 0, 99, null, null)));
        Assert.Single(spool.Pending("session-a"));
        Assert.Equal("patient", spool.Pending("session-a")[0].Chunk.ChannelId);
    }
    [Fact]
    public void ContiguousWatermarkNeverDeletesUnacknowledgedChunks()
    {
        using var spool = new EncryptedSpool(directory);
        spool.Append(Chunk(), DateTimeOffset.UtcNow);
        spool.Append(Chunk(2), DateTimeOffset.UtcNow);
        spool.Acknowledge("session-a", new("ACK_DURABLE", "doctor", "epoch-a", 2, 2, null, null));
        Assert.Equal(0, Assert.Single(spool.Pending("session-a")).Chunk.Seq);
    }
    [Fact]
    public void EncryptedSpoolSurvivesRestartWithoutPlaintextIdentifiers()
    {
        using (var first = new EncryptedSpool(directory)) first.Append(Chunk(), DateTimeOffset.UtcNow);
        var raw = File.ReadAllBytes(Assert.Single(Directory.GetFiles(directory, "*.spool")));
        Assert.DoesNotContain("session-a", Encoding.UTF8.GetString(raw));
        using var restored = new EncryptedSpool(directory);
        Assert.Equal(Chunk().Sha256, Assert.Single(restored.Pending("session-a")).Chunk.Sha256);
    }
    [Fact]
    public void OnlyOneProcessCanWriteTheSameSpool()
    {
        using var spool = new EncryptedSpool(directory);
        Assert.Throws<IOException>(() => new EncryptedSpool(directory));
        spool.Append(Chunk(), DateTimeOffset.UtcNow);
        Assert.Single(spool.Pending("session-a"));
    }
    [Fact]
    public void FinalizationEndsSurviveDurableAckAndRestart()
    {
        using (var first = new EncryptedSpool(directory))
        {
            first.Append(Chunk(), DateTimeOffset.UtcNow);
            first.Acknowledge("session-a", new("ACK_DURABLE", "doctor", "epoch-a", 0, 0, null, null));
        }
        using var restored = new EncryptedSpool(directory);
        Assert.Empty(restored.Pending("session-a"));
        Assert.Equal(16000, Assert.Single(restored.Ends("session-a")).SampleEnd);
    }
    [Fact]
    public void CompletedSessionCleanupRetainsAudioAppendedAfterAnEmptySnapshot()
    {
        using var spool = new EncryptedSpool(directory);
        Assert.Empty(spool.Pending("session-a"));
        Assert.Empty(spool.Gaps("session-a"));
        spool.Append(Chunk(), DateTimeOffset.UtcNow);
        Assert.False(spool.TryCompleteSession("session-a"));
        Assert.Single(spool.Pending("session-a"));
        Assert.Single(Directory.GetFiles(directory, "*.spool"));
    }
    [Fact]
    public void InterruptedCaptureStaysBlockedAfterRestartWithoutPendingChunks()
    {
        using (var spool = new EncryptedSpool(directory))
        {
            spool.BeginCapture("session-a");
            spool.Append(Chunk(), DateTimeOffset.UtcNow);
            spool.Acknowledge("session-a", new("ACK_DURABLE", "doctor", "epoch-a", 0, 0, null, null));
        }
        using var restored = new EncryptedSpool(directory);
        Assert.Empty(restored.Pending("session-a"));
        Assert.True(restored.HasUnfinishedCapture("session-a"));
        Assert.False(restored.TryCompleteSession("session-a"));
        Assert.Single(restored.SessionIds);
    }
    [Fact]
    public void CleanCaptureCanCompleteOnlyAfterEveryChunkAndGapIsAcknowledged()
    {
        using (var spool = new EncryptedSpool(directory))
        {
            spool.BeginCapture("session-a");
            spool.Append(Chunk(), DateTimeOffset.UtcNow);
            spool.RecordGap(Chunk(1), "test_gap", DateTimeOffset.UtcNow);
            spool.CompleteCapture("session-a");
            spool.Acknowledge("session-a", new("ACK_DURABLE", "doctor", "epoch-a", 0, 0, null, null));
            Assert.False(spool.TryCompleteSession("session-a"));
            spool.AcknowledgeGaps("session-a", [Assert.Single(spool.Gaps("session-a")).Key]);
            Assert.True(spool.TryCompleteSession("session-a"));
        }
        using var restored = new EncryptedSpool(directory);
        Assert.False(restored.HasUnfinishedCapture("session-a"));
        Assert.Empty(restored.SessionIds);
    }
    [Fact]
    public void ConflictingSequenceCannotOverwriteSpool()
    {
        using var spool = new EncryptedSpool(directory);
        spool.Append(Chunk(), DateTimeOffset.UtcNow);
        Assert.Throws<InvalidDataException>(() => spool.Append(Chunk(pcm: Enumerable.Repeat((byte)7, 32000).ToArray()), DateTimeOffset.UtcNow));
        Assert.Equal(Chunk().Sha256, Assert.Single(spool.Pending("session-a")).Chunk.Sha256);
    }
    [Fact]
    public void TamperedEnvelopeFailsClosed()
    {
        using (var spool = new EncryptedSpool(directory)) spool.Append(Chunk(), DateTimeOffset.UtcNow);
        var path = Assert.Single(Directory.GetFiles(directory, "*.spool"));
        var data = File.ReadAllBytes(path); data[^1] ^= 1; File.WriteAllBytes(path, data);
        Assert.Throws<AuthenticationTagMismatchException>(() => new EncryptedSpool(directory));
    }
    [Fact]
    public void ExpiredUnacknowledgedAudioIsReportedBeforeDeletion()
    {
        using var spool = new EncryptedSpool(directory);
        spool.Append(Chunk(), DateTimeOffset.UtcNow.AddHours(-25));
        Assert.Single(spool.Expired("session-a", DateTimeOffset.UtcNow));
        Assert.Single(spool.Pending("session-a"));
    }
    [Fact]
    public void ExpirationPersistsGapEvidenceAcrossRestartBeforePayloadRemoval()
    {
        using (var spool = new EncryptedSpool(directory))
        {
            spool.Append(Chunk(), DateTimeOffset.UtcNow.AddHours(-25));
            var gap = Assert.Single(spool.Expire(DateTimeOffset.UtcNow));
            Assert.Equal(16000, gap.SampleCount);
            Assert.Empty(spool.Pending("session-a"));
            Assert.Empty(Directory.GetFiles(directory, "*.spool"));
        }
        using var restored = new EncryptedSpool(directory);
        var retained = Assert.Single(restored.Gaps("session-a"));
        Assert.Equal("local_retention_expired", retained.Reason);
        Assert.Equal(16000, Assert.Single(restored.Ends("session-a")).SampleEnd);
        restored.AcknowledgeGaps("wrong-session", [retained.Key]);
        Assert.Single(restored.Gaps("session-a"));
        restored.AcknowledgeGaps("session-a", [retained.Key]);
        Assert.Empty(restored.Gaps("session-a"));
    }
    [Fact]
    public void AuthorizationExpiresAt60SecondsUsingMonotonicClock()
    {
        var clock = new TestClock(); var gate = new CaptureSafetyGate(clock);
        Assert.Equal("authorization_expired", gate.StopReason(0)); gate.Authorize();
        clock.Advance(TimeSpan.FromSeconds(59)); Assert.Null(gate.StopReason(0));
        clock.Advance(TimeSpan.FromSeconds(1)); Assert.Equal("authorization_expired", gate.StopReason(0));
    }
    [Fact]
    public void RejectedTailRecordsRecoverableGapAndFinalBoundaryWithoutPayload()
    {
        using (var spool = new EncryptedSpool(directory))
            spool.RecordGap(Chunk(), "authorization_expired", DateTimeOffset.UtcNow);
        using var restored = new EncryptedSpool(directory);
        Assert.Empty(restored.Pending("session-a"));
        Assert.Equal("session-a", Assert.Single(restored.SessionIds));
        Assert.Equal(16000, Assert.Single(restored.Gaps("session-a")).SampleCount);
        Assert.Equal(16000, Assert.Single(restored.Ends("session-a")).SampleEnd);
    }
    [Fact]
    public void BufferCapAndRevocationStopCapture()
    {
        var gate = new CaptureSafetyGate(); gate.Authorize();
        Assert.Equal("offline_buffer_full", gate.StopReason(600)); gate.Revoke();
        Assert.Equal("authorization_revoked", gate.StopReason(0));
        Assert.Throws<InvalidOperationException>(gate.Authorize);
    }
    [Fact]
    public void InputMappingRejectsDuplicatedDeviceAndChannel()
    {
        var request = new StartRequest { SessionId = "s", Token = "test", Channels = [new("d1", "doctor"), new("d1", "patient")] };
        Assert.Throws<ArgumentException>(request.Normalize);
        request.Channels = [new("d1", "doctor"), new("d2", "doctor")];
        Assert.Throws<ArgumentException>(request.Normalize);
    }
    [Fact]
    public void QualityDistinguishesSilenceAndClipping()
    {
        Assert.True(Quality.Measure(new byte[32000]).Silence);
        var clipped = Enumerable.Range(0, 16000).SelectMany(_ => BitConverter.GetBytes(short.MaxValue)).ToArray();
        Assert.Equal(1, Quality.Measure(clipped).ClippingFraction);
        Assert.False(Quality.Measure(clipped).Silence);
    }
    public void Dispose() { if (Directory.Exists(directory)) Directory.Delete(directory, true); }
    private sealed class TestClock : TimeProvider
    {
        private long ticks;
        public override long TimestampFrequency => TimeSpan.TicksPerSecond;
        public override long GetTimestamp() => ticks;
        public void Advance(TimeSpan duration) => ticks += duration.Ticks;
    }
}
