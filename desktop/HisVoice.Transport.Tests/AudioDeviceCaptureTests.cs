using HisVoice.Core;
using HisVoice.Desktop;
using NAudio.Wave;
using Xunit;

namespace HisVoice.Transport.Tests;

public sealed class AudioDeviceCaptureTests
{
    [Fact]
    public async Task StopWaitsForFinalDeviceCallbackAndRetainsPartialChunk()
    {
        using var input = new TestInput(16000, 1);
        var chunks = new List<AudioChunk>();
        using var capture = new AudioDeviceCapture(input, "session-a", new("device-a", "doctor"), chunks.Add, (_, _) => { });
        capture.Start();
        input.Emit(new byte[16000]);
        var stopping = capture.StopAsync();
        Assert.True(input.StopRequested);
        Assert.False(stopping.IsCompleted);
        input.Emit(new byte[8000]);
        input.CompleteStop();
        var end = await stopping.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal(12000, Assert.Single(chunks).SampleCount);
        Assert.Equal(12000, end.SampleEnd);
        Assert.Equal(0, end.LastSeq);
        Assert.Equal(16000, chunks[0].CaptureMetadata!.InputSampleRate);
    }

    [Theory]
    [InlineData(44100, 1)]
    [InlineData(48000, 2)]
    public async Task ResamplingPreservesDurationAcrossCallbacksAndStop(int rate, int channels)
    {
        using var input = new TestInput(rate, channels);
        var chunks = new List<AudioChunk>();
        using var capture = new AudioDeviceCapture(input, "session-a", new("device-a", "doctor"), chunks.Add, (_, _) => { });
        capture.Start();
        for (int i = 0; i < 20; i++) input.Emit(new byte[rate / 20 * channels * 2]);
        var stopping = capture.StopAsync();
        input.CompleteStop();
        var end = await stopping.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.InRange(end.SampleEnd, 15999, 16001);
        Assert.Equal(end.SampleEnd, chunks.Sum(c => (long)c.SampleCount));
        Assert.All(chunks, c => Assert.Equal(rate, c.CaptureMetadata!.InputSampleRate));
        Assert.All(chunks, c => Assert.Equal(channels, c.CaptureMetadata!.InputChannels));
        Assert.All(chunks, c => c.Validate());
    }

    [Fact]
    public async Task FailedPersistenceCanRetrySameFinalChunk()
    {
        using var input = new TestInput(16000, 1);
        var chunks = new List<AudioChunk>();
        bool fail = true;
        using var capture = new AudioDeviceCapture(input, "session-a", new("device-a", "doctor"), chunk =>
        {
            if (fail) throw new IOException("Injected spool write failure.");
            chunks.Add(chunk);
        }, (_, _) => { });
        capture.Start();
        input.Emit(new byte[32000]);
        fail = false;
        var stopping = capture.StopAsync();
        input.CompleteStop();
        await stopping.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal(0, Assert.Single(chunks).Seq);
        Assert.Equal(16000, chunks[0].SampleCount);
    }

    private sealed class TestInput(int rate, int channels) : IWaveIn
    {
        public WaveFormat WaveFormat { get; set; } = new(rate, 16, channels);
        public event EventHandler<WaveInEventArgs>? DataAvailable;
        public event EventHandler<StoppedEventArgs>? RecordingStopped;
        public bool StopRequested { get; private set; }
        public void StartRecording() { }
        public void StopRecording() => StopRequested = true;
        public void Emit(byte[] data) => DataAvailable?.Invoke(this, new(data, data.Length));
        public void CompleteStop() => RecordingStopped?.Invoke(this, new());
        public void Dispose() { }
    }
}
