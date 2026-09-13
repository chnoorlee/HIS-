using System.Security.Cryptography;
using System.Text;
using System.Text.Json;

namespace HisVoice.Core;

public sealed class EncryptedSpool : IDisposable
{
    private readonly string directory;
    private readonly byte[] key = [];
    private readonly FileStream lease;
    private readonly object sync = new();
    private readonly Dictionary<string, SpoolRecord> records = [];
    private readonly Dictionary<string, List<ChannelEnd>> sessionEnds = [];
    private readonly Dictionary<string, AudioGap> gaps = [];
    private readonly HashSet<string> unfinishedCaptures = [];
    private static readonly byte[] Entropy = Encoding.UTF8.GetBytes("HisVoice.Desktop.Spool.v1");

    public EncryptedSpool(string directory)
    {
        this.directory = Path.GetFullPath(directory);
        Directory.CreateDirectory(this.directory);
        lease = new FileStream(Path.Combine(this.directory, "spool.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
        try
        {
            var keyPath = Path.Combine(this.directory, "user.key");
            if (File.Exists(keyPath)) key = ProtectedData.Unprotect(File.ReadAllBytes(keyPath), Entropy, DataProtectionScope.CurrentUser);
            else
            {
                key = RandomNumberGenerator.GetBytes(32);
                AtomicWrite(keyPath, ProtectedData.Protect(key, Entropy, DataProtectionScope.CurrentUser));
            }
            var statePath = Path.Combine(this.directory, "session-state.encrypted");
            if (File.Exists(statePath))
            {
                var restored = JsonSerializer.Deserialize<Dictionary<string, List<ChannelEnd>>>(DecryptBytes(
                    File.ReadAllBytes(statePath), "session-state-v1"), Wire.Json) ?? throw new InvalidDataException("Invalid capture state.");
                foreach (var entry in restored) sessionEnds.Add(entry.Key, entry.Value);
            }
            var gapsPath = Path.Combine(this.directory, "gaps.encrypted");
            if (File.Exists(gapsPath))
            {
                var restored = JsonSerializer.Deserialize<AudioGap[]>(DecryptBytes(File.ReadAllBytes(gapsPath), "gap-ledger-v1"), Wire.Json)
                    ?? throw new InvalidDataException("Invalid audio gap ledger.");
                foreach (var gap in restored)
                {
                    gaps.Add(gap.Key, gap);
                    RememberEnd(gap.SessionId, new(gap.ChannelId, gap.CaptureEpoch, gap.Seq, gap.SampleStart + gap.SampleCount));
                }
            }
            var capturePath = Path.Combine(this.directory, "capture-state.encrypted");
            if (File.Exists(capturePath))
            {
                var restored = JsonSerializer.Deserialize<string[]>(DecryptBytes(File.ReadAllBytes(capturePath), "capture-state-v1"), Wire.Json)
                    ?? throw new InvalidDataException("Invalid unfinished capture state.");
                unfinishedCaptures.UnionWith(restored);
            }
            foreach (var path in Directory.EnumerateFiles(this.directory, "*.spool"))
            {
                var record = Decrypt(path);
                record.Chunk.Validate();
                if (Path.GetFileNameWithoutExtension(path) != FileId(record.Chunk.Key))
                    throw new InvalidDataException("Spool identity mismatch; capture is disabled until recovery.");
                records.Add(record.Chunk.Key, record);
                RememberEnd(record.Chunk);
            }
            foreach (var temporary in Directory.EnumerateFiles(this.directory, "*.tmp")) File.Delete(temporary);
        }
        catch
        {
            CryptographicOperations.ZeroMemory(key); lease.Dispose(); throw;
        }
    }

    public IReadOnlyList<SpoolRecord> Pending(string sessionId)
    {
        lock (sync) return records.Values.Where(x => x.Chunk.SessionId == sessionId)
            .OrderBy(x => x.CapturedAtUtc).ThenBy(x => x.Chunk.Seq).ToArray();
    }
    public IReadOnlyList<string> SessionIds { get { lock (sync) return sessionEnds.Keys.Concat(unfinishedCaptures).Distinct().ToArray(); } }
    public bool HasUnfinishedCapture(string sessionId) { lock (sync) return unfinishedCaptures.Contains(sessionId); }
    public void BeginCapture(string sessionId)
    {
        lock (sync)
        {
            if (!unfinishedCaptures.Add(sessionId)) throw new InvalidOperationException("An unfinished capture requires review before recording again.");
            PersistCaptures();
        }
    }
    public void CompleteCapture(string sessionId)
    {
        lock (sync)
        {
            var remaining = unfinishedCaptures.Where(id => id != sessionId).ToArray();
            PersistCaptures(remaining);
            unfinishedCaptures.Remove(sessionId);
        }
    }
    public IReadOnlyList<ChannelEnd> Ends(string sessionId) { lock (sync) return sessionEnds.TryGetValue(sessionId, out var ends) ? ends.ToArray() : []; }
    public IReadOnlyList<AudioGap> Gaps(string sessionId) { lock (sync) return gaps.Values.Where(x => x.SessionId == sessionId).ToArray(); }
    public void RecordGap(AudioChunk chunk, string reason, DateTimeOffset now)
    {
        lock (sync)
        {
            if (records.ContainsKey(chunk.Key)) return;
            gaps[chunk.Key] = new(chunk.SessionId, chunk.ChannelId, chunk.CaptureEpoch, chunk.Seq,
                chunk.SampleStart, chunk.SampleCount, reason, now);
            PersistGaps();
            RememberEnd(chunk);
            PersistEnds();
        }
    }
    public void Append(AudioChunk chunk, DateTimeOffset now)
    {
        chunk.Validate();
        lock (sync)
        {
            if (records.TryGetValue(chunk.Key, out var previous))
            {
                if (previous.Chunk.Sha256 != chunk.Sha256 || previous.Chunk.SampleStart != chunk.SampleStart ||
                    previous.Chunk.SampleCount != chunk.SampleCount) throw new InvalidDataException("Conflicting chunk sequence.");
                return;
            }
            var record = new SpoolRecord(chunk, now);
            var id = FileId(chunk.Key);
            AtomicWrite(Path.Combine(directory, id + ".spool"), Encrypt(record, id));
            records.Add(chunk.Key, record);
            RememberEnd(chunk);
            PersistEnds();
        }
    }

    public bool Acknowledge(string sessionId, StreamAck ack)
    {
        if (ack.Type != "ACK_DURABLE" || ack.Seq is null || ack.ChannelId is null || ack.CaptureEpoch is null) return false;
        lock (sync)
        {
            var coordinate = $"{sessionId}/{ack.CaptureEpoch}/{ack.ChannelId}/{ack.Seq}";
            if (!records.ContainsKey(coordinate)) return false;
            PersistEnds();
            File.Delete(Path.Combine(directory, FileId(coordinate) + ".spool"));
            records.Remove(coordinate);
            return true;
        }
    }

    public IReadOnlyList<SpoolRecord> Expired(string sessionId, DateTimeOffset now) => Pending(sessionId)
        .Where(r => now - r.CapturedAtUtc >= TimeSpan.FromHours(24)).ToArray();

    public IReadOnlyList<AudioGap> Expire(DateTimeOffset now)
    {
        lock (sync)
        {
            var expired = records.Values.Where(r => now - r.CapturedAtUtc >= TimeSpan.FromHours(24)).ToArray();
            if (expired.Length == 0) return [];
            foreach (var record in expired)
            {
                var c = record.Chunk;
                gaps[c.Key] = new(c.SessionId, c.ChannelId, c.CaptureEpoch, c.Seq, c.SampleStart, c.SampleCount, "local_retention_expired", now);
            }
            // Durable gap evidence must precede unacknowledged payload deletion, including across a crash.
            PersistGaps();
            foreach (var record in expired)
            {
                File.Delete(Path.Combine(directory, FileId(record.Chunk.Key) + ".spool"));
                records.Remove(record.Chunk.Key);
            }
            return expired.Select(r => gaps[r.Chunk.Key]).ToArray();
        }
    }
    public void AcknowledgeGaps(string sessionId, IEnumerable<string> keys)
    {
        lock (sync)
        {
            foreach (var key in keys)
                if (gaps.TryGetValue(key, out var gap) && gap.SessionId == sessionId) gaps.Remove(key);
            PersistGaps();
        }
    }

    public bool TryCompleteSession(string sessionId)
    {
        lock (sync)
        {
            if (records.Values.Any(x => x.Chunk.SessionId == sessionId) ||
                gaps.Values.Any(x => x.SessionId == sessionId) || unfinishedCaptures.Contains(sessionId)) return false;
            sessionEnds.Remove(sessionId);
            PersistEnds();
            return true;
        }
    }

    // Only call after server finalization has been checked or under an authorized deletion workflow.
    public void DeleteSession(string sessionId)
    {
        lock (sync)
        {
            foreach (var record in records.Values.Where(x => x.Chunk.SessionId == sessionId).ToArray())
            {
                File.Delete(Path.Combine(directory, FileId(record.Chunk.Key) + ".spool"));
                records.Remove(record.Chunk.Key);
            }
            sessionEnds.Remove(sessionId);
            foreach (var gap in gaps.Values.Where(x => x.SessionId == sessionId).ToArray()) gaps.Remove(gap.Key);
            PersistEnds();
            PersistGaps();
        }
    }

    private void RememberEnd(AudioChunk chunk)
        => RememberEnd(chunk.SessionId, new(chunk.ChannelId, chunk.CaptureEpoch, chunk.Seq, chunk.SampleStart + chunk.SampleCount));

    private void RememberEnd(string sessionId, ChannelEnd end)
    {
        if (!sessionEnds.TryGetValue(sessionId, out var ends)) sessionEnds[sessionId] = ends = [];
        int index = ends.FindIndex(e => e.ChannelId == end.ChannelId && e.CaptureEpoch == end.CaptureEpoch);
        if (index < 0) ends.Add(end);
        else if (ends[index].LastSeq < end.LastSeq) ends[index] = end;
    }
    private void PersistEnds() => AtomicWrite(Path.Combine(directory, "session-state.encrypted"),
        EncryptBytes(JsonSerializer.SerializeToUtf8Bytes(sessionEnds, Wire.Json), "session-state-v1"));
    private void PersistGaps() => AtomicWrite(Path.Combine(directory, "gaps.encrypted"),
        EncryptBytes(JsonSerializer.SerializeToUtf8Bytes(gaps.Values.ToArray(), Wire.Json), "gap-ledger-v1"));
    private void PersistCaptures(IEnumerable<string>? sessions = null) => AtomicWrite(Path.Combine(directory, "capture-state.encrypted"),
        EncryptBytes(JsonSerializer.SerializeToUtf8Bytes((sessions ?? unfinishedCaptures).ToArray(), Wire.Json), "capture-state-v1"));

    private static string FileId(string coordinate) => Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(coordinate)));
    private byte[] Encrypt(SpoolRecord record, string id)
    {
        var plaintext = JsonSerializer.SerializeToUtf8Bytes(record, Wire.Json);
        return EncryptBytes(plaintext, id);
    }
    private byte[] EncryptBytes(byte[] plaintext, string id)
    {
        var output = new byte[1 + 12 + 16 + plaintext.Length];
        output[0] = 1;
        RandomNumberGenerator.Fill(output.AsSpan(1, 12));
        using var aes = new AesGcm(key, 16);
        aes.Encrypt(output.AsSpan(1, 12), plaintext, output.AsSpan(29), output.AsSpan(13, 16), Encoding.UTF8.GetBytes(id));
        CryptographicOperations.ZeroMemory(plaintext);
        return output;
    }
    private SpoolRecord Decrypt(string path)
    {
        var plaintext = DecryptBytes(File.ReadAllBytes(path), Path.GetFileNameWithoutExtension(path));
        try { return JsonSerializer.Deserialize<SpoolRecord>(plaintext, Wire.Json) ?? throw new InvalidDataException("Empty spool record."); }
        finally { CryptographicOperations.ZeroMemory(plaintext); }
    }
    private byte[] DecryptBytes(byte[] content, string id)
    {
        if (content.Length < 30 || content[0] != 1) throw new InvalidDataException("Unsupported spool envelope.");
        var plaintext = new byte[content.Length - 29];
        using var aes = new AesGcm(key, 16);
        aes.Decrypt(content.AsSpan(1, 12), content.AsSpan(29), content.AsSpan(13, 16), plaintext, Encoding.UTF8.GetBytes(id));
        return plaintext;
    }
    private static void AtomicWrite(string path, byte[] content)
    {
        var temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
        using (var stream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None,
            4096, FileOptions.WriteThrough))
        {
            stream.Write(content); stream.Flush(true);
        }
        File.Move(temporary, path, true);
    }
    public void Dispose() { CryptographicOperations.ZeroMemory(key); lease.Dispose(); }
}
