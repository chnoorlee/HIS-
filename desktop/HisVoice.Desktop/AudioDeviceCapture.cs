using System.Diagnostics;
using System.IO;
using HisVoice.Core;
using NAudio.CoreAudioApi;
using NAudio.Wave;
using NAudio.Wave.SampleProviders;

namespace HisVoice.Desktop;

internal sealed class AudioDeviceCapture : IDisposable
{
    private readonly IWaveIn capture;
    private readonly MMDevice? device;
    private readonly BufferedWaveProvider input;
    private readonly IWaveProvider output;
    private readonly List<byte> accumulation = [];
    private readonly object sync = new();
    private readonly Action<AudioChunk> onChunk;
    private readonly Action<string, object> publish;
    private readonly string sessionId;
    private readonly long epochStart = Stopwatch.GetTimestamp();
    private long inputFrames;
    private long sampleStart;
    private long nextSequence;
    private volatile bool expectedStop;
    private bool started;
    private readonly TaskCompletionSource stopped = new(TaskCreationOptions.RunContinuationsAsynchronously);
    public string ChannelId { get; }
    public string CaptureEpoch { get; } = Guid.NewGuid().ToString("N");
    public string DeviceId { get; }
    public event Action<Exception>? Failed;

    public AudioDeviceCapture(string sessionId, DeviceChannel channel, Action<AudioChunk> onChunk, Action<string, object> publish)
        : this(OpenDevice(channel), sessionId, channel, onChunk, publish) { }

    private AudioDeviceCapture((IWaveIn Capture, MMDevice? Device) source, string sessionId, DeviceChannel channel,
        Action<AudioChunk> onChunk, Action<string, object> publish)
        : this(source.Capture, sessionId, channel, onChunk, publish) => device = source.Device;

    internal AudioDeviceCapture(IWaveIn capture, string sessionId, DeviceChannel channel, Action<AudioChunk> onChunk,
        Action<string, object> publish)
    {
        this.capture = capture; this.sessionId = sessionId; this.onChunk = onChunk; this.publish = publish;
        ChannelId = channel.ChannelId; DeviceId = channel.DeviceId;
        input = new BufferedWaveProvider(capture.WaveFormat) { BufferDuration = TimeSpan.FromSeconds(5), ReadFully = false, DiscardOnBufferOverflow = false };
        ISampleProvider samples = input.ToSampleProvider();
        if (samples.WaveFormat.Channels == 2) samples = new StereoToMonoSampleProvider(samples) { LeftVolume = .5f, RightVolume = .5f };
        if (samples.WaveFormat.SampleRate != 16000) samples = new WdlResamplingSampleProvider(samples, 16000);
        output = new SampleToWaveProvider16(samples);
        capture.DataAvailable += DataAvailable;
        capture.RecordingStopped += RecordingStopped;
    }

    private static (IWaveIn Capture, MMDevice? Device) OpenDevice(DeviceChannel channel)
    {
        using var enumerator = new MMDeviceEnumerator();
        var device = enumerator.GetDevice(channel.DeviceId);
        if (device.State != DeviceState.Active) { device.Dispose(); throw new InvalidOperationException("Selected input device is unavailable."); }
        var capture = new WasapiCapture(device);
        if (capture.WaveFormat.Channels is < 1 or > 2)
        {
            capture.Dispose(); device.Dispose();
            throw new InvalidOperationException("Select a mono or stereo capture endpoint; multichannel interfaces require an explicit channel mixer.");
        }
        return (capture, device);
    }

    public void Start() { expectedStop = false; capture.StartRecording(); started = true; }
    private void RecordingStopped(object? sender, StoppedEventArgs e)
    {
        stopped.TrySetResult();
        if (!expectedStop || e.Exception is not null) Failed?.Invoke(e.Exception ?? new IOException("Audio device stopped unexpectedly."));
    }
    private void DataAvailable(object? sender, WaveInEventArgs e)
    {
        try
        {
            lock (sync)
            {
                if (stopped.Task.IsCompleted) return;
                input.AddSamples(e.Buffer, 0, e.BytesRecorded);
                inputFrames += e.BytesRecorded / capture.WaveFormat.BlockAlign;
                // WDL keeps fractional phase across callbacks; all raw samples enter it even during silence.
                int maxOutputBytes = (int)Math.Ceiling(input.BufferedBytes / (double)capture.WaveFormat.BlockAlign * 16000 / capture.WaveFormat.SampleRate) * 2;
                if (maxOutputBytes == 0) return;
                var converted = new byte[maxOutputBytes];
                int count = output.Read(converted, 0, converted.Length);
                accumulation.AddRange(converted.AsSpan(0, count).ToArray());
                while (accumulation.Count >= 32000) FlushChunk(32000);
            }
        }
        catch (Exception ex) { Failed?.Invoke(ex); }
    }

    private void FlushChunk(int size)
    {
        var pcm = accumulation.GetRange(0, size).ToArray();
        double elapsedMs = Stopwatch.GetElapsedTime(epochStart).TotalMilliseconds;
        double sampleClockMs = inputFrames * 1000d / capture.WaveFormat.SampleRate;
        double uncertaintyMs = Math.Abs(elapsedMs - sampleClockMs);
        var chunk = AudioChunk.Create(sessionId, CaptureEpoch, ChannelId, nextSequence, sampleStart, pcm,
            Stopwatch.GetTimestamp(), uncertaintyMs, new(capture.WaveFormat.SampleRate, capture.WaveFormat.Channels,
                DeviceId, capture.WaveFormat.SampleRate == 16000 ? "none" : "NAudio.WdlResamplingSampleProvider/2.2.1",
                typeof(AudioDeviceCapture).Assembly.GetName().Version?.ToString() ?? "unknown"));
        onChunk(chunk);
        accumulation.RemoveRange(0, size);
        nextSequence++; sampleStart += pcm.Length / 2;
        var quality = Quality.Measure(pcm);
        publish("capture.quality", new
        {
            channel_id = ChannelId,
            rms = quality.Rms,
            clipping_fraction = quality.ClippingFraction,
            silence = quality.Silence,
            clock_uncertainty_ms = uncertaintyMs,
            synchronized_clock = false
        });
    }

    public async Task<ChannelEnd> StopAsync()
    {
        expectedStop = true;
        if (started)
        {
            capture.StopRecording();
            // NAudio signals RecordingStopped only after its final DataAvailable callback.
            await stopped.Task.WaitAsync(TimeSpan.FromSeconds(5));
        }
        lock (sync)
        {
            var tail = new byte[32000];
            int count;
            while ((count = output.Read(tail, 0, tail.Length)) > 0)
                accumulation.AddRange(tail.AsSpan(0, count).ToArray());
            while (accumulation.Count >= 32000) FlushChunk(32000);
            if (accumulation.Count >= 2) FlushChunk(accumulation.Count - accumulation.Count % 2);
            return End;
        }
    }
    public ChannelEnd End { get { lock (sync) return new(ChannelId, CaptureEpoch, nextSequence - 1, sampleStart); } }
    public void Dispose() { expectedStop = true; capture.DataAvailable -= DataAvailable; capture.RecordingStopped -= RecordingStopped; capture.Dispose(); device?.Dispose(); }

    public static object ListDevices()
    {
        using var enumerator = new MMDeviceEnumerator();
        string? defaultId = null;
        try { using var defaultDevice = enumerator.GetDefaultAudioEndpoint(DataFlow.Capture, Role.Communications); defaultId = defaultDevice.ID; }
        catch (System.Runtime.InteropServices.COMException) { }
        var list = new List<object>();
        foreach (var device in enumerator.EnumerateAudioEndPoints(DataFlow.Capture, DeviceState.Active))
        {
            using (device) list.Add(new { id = device.ID, name = device.FriendlyName, state = device.State.ToString(), is_default = device.ID == defaultId });
        }
        return new { devices = list, supports_multichannel = true };
    }
}
