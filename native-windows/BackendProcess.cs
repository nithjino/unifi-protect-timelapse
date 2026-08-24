using System.Collections.Concurrent;
using System.Diagnostics;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;

namespace TimeLapseNative;

public sealed record BackendCompletion(int ExitCode, bool WasCancelled, string StandardError);

public sealed class BackendProcess : IDisposable
{
    private const string EmbeddedBackendName = "TimeLapseNative.timelapse-backend.exe";
    private static readonly BackendSession SharedSession = new();
    private string? _requestId;
    private bool _cancelRequested;

    public static string ExecutablePath()
    {
        var candidates = new List<string>();
        var overridePath = Environment.GetEnvironmentVariable("TIMELAPSE_BACKEND_PATH");
        if (!string.IsNullOrWhiteSpace(overridePath)) candidates.Add(overridePath);
        var embeddedPath = ExtractEmbeddedBackend();
        if (embeddedPath is not null) return embeddedPath;
        candidates.Add(Path.Combine(AppContext.BaseDirectory, "Helpers", "timelapse-backend.exe"));
        candidates.Add(Path.Combine(AppContext.BaseDirectory, "timelapse-backend.exe"));
        var match = candidates.FirstOrDefault(File.Exists);
        return match ?? throw new FileNotFoundException(
            $"The bundled timelapse backend could not be found. Checked:{Environment.NewLine}{string.Join(Environment.NewLine, candidates)}");
    }

    public async Task<BackendCompletion> RunAsync(
        object request,
        Action<BackendEvent> onEvent,
        CancellationToken cancellationToken = default,
        string? cancellationPath = null)
    {
        _ = cancellationPath;
        _cancelRequested = false;
        var serialized = JsonSerializer.Serialize(request);
        using var document = JsonDocument.Parse(serialized);
        _requestId = document.RootElement.GetProperty("id").GetString()
            ?? throw new InvalidOperationException("Backend requests require a non-empty ID.");
        using var registration = cancellationToken.Register(Cancel);
        var completion = await SharedSession.SendAsync(serialized, _requestId, onEvent);
        _requestId = null;
        return completion with { WasCancelled = completion.WasCancelled || _cancelRequested };
    }

    public void Cancel()
    {
        _cancelRequested = true;
        if (_requestId is not null) _ = SharedSession.CancelAsync(_requestId);
    }

    public static Task ShutdownAsync() => SharedSession.ShutdownAsync();

    private static string? ExtractEmbeddedBackend()
    {
        var assembly = Assembly.GetExecutingAssembly();
        if (assembly.GetManifestResourceInfo(EmbeddedBackendName) is null) return null;

        string fingerprint;
        using (var hashStream = assembly.GetManifestResourceStream(EmbeddedBackendName)
            ?? throw new InvalidOperationException("The embedded backend resource could not be opened."))
        {
            fingerprint = Convert.ToHexString(SHA256.HashData(hashStream)).ToLowerInvariant();
        }

        var backendDirectory = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "TimeLapse",
            "Backend",
            fingerprint[..16]);
        var backendPath = Path.Combine(backendDirectory, "timelapse-backend.exe");
        Directory.CreateDirectory(backendDirectory);
        if (File.Exists(backendPath) && FileHashMatches(backendPath, fingerprint)) return backendPath;

        var temporaryPath = $"{backendPath}.{Guid.NewGuid():N}.tmp";
        try
        {
            using var resource = assembly.GetManifestResourceStream(EmbeddedBackendName)
                ?? throw new InvalidOperationException("The embedded backend resource could not be opened.");
            using (var output = new FileStream(temporaryPath, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                resource.CopyTo(output);
            try
            {
                File.Move(temporaryPath, backendPath, overwrite: true);
            }
            catch (IOException) when (File.Exists(backendPath) && FileHashMatches(backendPath, fingerprint))
            {
                File.Delete(temporaryPath);
            }
        }
        finally
        {
            if (File.Exists(temporaryPath)) File.Delete(temporaryPath);
        }
        return backendPath;
    }

    private static bool FileHashMatches(string path, string expectedHash)
    {
        using var stream = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(stream)).Equals(expectedHash, StringComparison.OrdinalIgnoreCase);
    }

    public void Dispose() => GC.SuppressFinalize(this);
}

internal sealed class BackendSession
{
    private sealed record Pending(Action<BackendEvent> OnEvent, TaskCompletionSource<BackendCompletion> Completion);

    private readonly ConcurrentDictionary<string, Pending> _pending = new();
    private readonly SemaphoreSlim _lifecycleLock = new(1, 1);
    private readonly SemaphoreSlim _writeLock = new(1, 1);
    private readonly ConcurrentDictionary<string, string> _hydrationRequests = new();
    private Process? _process;
    private StreamWriter? _input;
    private string _standardError = "";
    private DateTimeOffset _startedAt;
    private int _recentCrashes;
    private bool _shuttingDown;

    public async Task<BackendCompletion> SendAsync(string serialized, string requestId, Action<BackendEvent> onEvent)
    {
        await EnsureStartedAsync();
        using (var request = JsonDocument.Parse(serialized))
        {
            if (request.RootElement.TryGetProperty("command", out var command)
                && command.GetString() == "hydrate_credentials"
                && request.RootElement.TryGetProperty("profile_id", out var profileId)
                && profileId.GetString() is { } value)
                _hydrationRequests[value] = serialized;
        }
        var completion = new TaskCompletionSource<BackendCompletion>(TaskCreationOptions.RunContinuationsAsynchronously);
        if (!_pending.TryAdd(requestId, new Pending(onEvent, completion)))
            throw new InvalidOperationException($"The backend request ID is already active: {requestId}");
        try
        {
            await WriteAsync(serialized);
        }
        catch
        {
            _pending.TryRemove(requestId, out _);
            throw;
        }
        return await completion.Task;
    }

    public async Task CancelAsync(string targetId)
    {
        if (_process is not { HasExited: false }) return;
        var request = JsonSerializer.Serialize(new Dictionary<string, object>
        {
            ["id"] = $"cancel-{Guid.NewGuid()}",
            ["command"] = "cancel",
            ["target_id"] = targetId,
        });
        await WriteAsync(request);
    }

    public async Task ShutdownAsync()
    {
        if (_process is not { HasExited: false }) return;
        _shuttingDown = true;
        var requestId = $"shutdown-{Guid.NewGuid()}";
        var request = JsonSerializer.Serialize(new Dictionary<string, object>
        {
            ["id"] = requestId,
            ["command"] = "shutdown",
        });
        await SendAsync(request, requestId, _ => { });
    }

    private async Task EnsureStartedAsync()
    {
        if (_process is { HasExited: false })
        {
            if (DateTimeOffset.UtcNow - _startedAt >= TimeSpan.FromMinutes(5)) _recentCrashes = 0;
            return;
        }
        await _lifecycleLock.WaitAsync();
        try
        {
            if (_process is { HasExited: false }) return;
            var startInfo = new ProcessStartInfo(BackendProcess.ExecutablePath())
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            var process = new Process { StartInfo = startInfo, EnableRaisingEvents = true };
            if (!process.Start()) throw new InvalidOperationException("The timelapse backend could not be started.");
            _process = process;
            _input = process.StandardInput;
            _standardError = "";
            _startedAt = DateTimeOffset.UtcNow;
            _shuttingDown = false;
            _ = ReadEventsAsync(process);
            _ = ReadStandardErrorAsync(process);
            process.Exited += (_, _) => ProcessExited(process);

            var handshakeId = $"handshake-{Guid.NewGuid()}";
            var handshakeCompletion = new TaskCompletionSource<BackendCompletion>(TaskCreationOptions.RunContinuationsAsynchronously);
            _pending[handshakeId] = new Pending(_ => { }, handshakeCompletion);
            var handshake = new Dictionary<string, object>
            {
                ["id"] = handshakeId,
                ["command"] = "handshake",
                ["protocol_version"] = 2,
            };
            if (_recentCrashes >= 2) handshake["recovery_mode"] = "quiescent";
            await WriteAsync(JsonSerializer.Serialize(handshake));
            var handshakeResult = await handshakeCompletion.Task.WaitAsync(TimeSpan.FromSeconds(10));
            if (handshakeResult.ExitCode != 0) throw new InvalidOperationException("Backend protocol version 2 was rejected.");
        }
        finally
        {
            _lifecycleLock.Release();
        }
    }

    private async Task WriteAsync(string serialized)
    {
        await _writeLock.WaitAsync();
        try
        {
            var input = _input ?? throw new InvalidOperationException("The backend session is not running.");
            await input.WriteLineAsync(serialized);
            await input.FlushAsync();
        }
        finally
        {
            _writeLock.Release();
        }
    }

    private async Task ReadEventsAsync(Process process)
    {
        string? line;
        while ((line = await process.StandardOutput.ReadLineAsync()) is not null)
        {
            if (string.IsNullOrWhiteSpace(line)) continue;
            BackendEvent? backendEvent;
            try
            {
                backendEvent = JsonSerializer.Deserialize<BackendEvent>(line);
            }
            catch (JsonException)
            {
                continue;
            }
            if (backendEvent?.Id is null || !_pending.TryGetValue(backendEvent.Id, out var pending)) continue;
            pending.OnEvent(backendEvent);
            if (backendEvent.Event is not ("complete" or "cancelled" or "error")) continue;
            _pending.TryRemove(backendEvent.Id, out _);
            pending.Completion.TrySetResult(new BackendCompletion(
                backendEvent.Event == "error" ? 1 : 0,
                backendEvent.Event == "cancelled",
                _standardError));
        }
    }

    private async Task ReadStandardErrorAsync(Process process)
    {
        _standardError = (await process.StandardError.ReadToEndAsync()).Trim();
    }

    private void ProcessExited(Process process)
    {
        if (!ReferenceEquals(process, _process)) return;
        _process = null;
        _input = null;
        _recentCrashes = DateTimeOffset.UtcNow - _startedAt < TimeSpan.FromMinutes(5) ? _recentCrashes + 1 : 0;
        var completion = new BackendCompletion(process.ExitCode == 0 ? 1 : process.ExitCode, false, _standardError);
        foreach (var (id, pending) in _pending.ToArray())
        {
            if (_pending.TryRemove(id, out _)) pending.Completion.TrySetResult(completion);
        }
        process.Dispose();
        if (!_shuttingDown && _recentCrashes <= 2 && _hydrationRequests.Count > 0)
            _ = RestartAfterCrashAsync();
    }

    private async Task RestartAfterCrashAsync()
    {
        await Task.Delay(TimeSpan.FromSeconds(1));
        try
        {
            await EnsureStartedAsync();
            if (_recentCrashes < 2)
                foreach (var hydration in _hydrationRequests.Values) await WriteAsync(hydration);
        }
        catch
        {
            // The next explicit request reports the unavailable supervisor.
        }
    }
}
