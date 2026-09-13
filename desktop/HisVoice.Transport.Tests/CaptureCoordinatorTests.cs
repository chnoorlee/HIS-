using System.Text.Json;
using HisVoice.Core;
using HisVoice.Desktop;
using Xunit;

namespace HisVoice.Transport.Tests;

public sealed class CaptureCoordinatorTests
{
    [Fact]
    public async Task InvalidResumePreservesIdleState()
    {
        var directory = Path.Combine(Path.GetTempPath(), "his-voice-coordinator-tests", Guid.NewGuid().ToString("N"));
        try
        {
            using var spool = new EncryptedSpool(directory);
            await using var coordinator = new CaptureCoordinator(new DesktopSettings(), spool, "device-a", (_, _) => { }, action => action());
            await Assert.ThrowsAsync<InvalidOperationException>(coordinator.ResumeAsync);
            using var status = JsonDocument.Parse(JsonSerializer.Serialize(coordinator.Status(), Wire.Json));
            Assert.Equal("IDLE", status.RootElement.GetProperty("state").GetString());
        }
        finally { Directory.Delete(directory, true); }
    }
    [Fact]
    public async Task RetentionStorageFailureIsHandledInsideDispatchedMonitorTick()
    {
        var directory = Path.Combine(Path.GetTempPath(), "his-voice-coordinator-tests", Guid.NewGuid().ToString("N"));
        try
        {
            using var spool = new EncryptedSpool(directory);
            var tick = new TaskCompletionSource<Action>(TaskCreationOptions.RunContinuationsAsynchronously);
            var events = new List<string>();
            await using var coordinator = new CaptureCoordinator(new DesktopSettings(), spool, "device-a",
                (type, _) => events.Add(type), action => tick.TrySetResult(action));
            spool.Append(AudioChunk.Create("session-a", "epoch-a", "doctor", 0, 0, new byte[32000], 0, 0), DateTimeOffset.UtcNow.AddHours(-25));
            using var blockedLedger = new FileStream(Path.Combine(directory, "gaps.encrypted"), FileMode.Create, FileAccess.Write, FileShare.None);
            var dispatched = await tick.Task.WaitAsync(TimeSpan.FromSeconds(5));
            Assert.Null(Record.Exception(dispatched));
            Assert.Contains("capture.error", events);
            Assert.Single(spool.Pending("session-a"));
        }
        finally { Directory.Delete(directory, true); }
    }
}
