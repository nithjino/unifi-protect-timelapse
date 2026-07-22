using System.Diagnostics;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;

namespace TimeLapseNative;

public sealed record BackendCompletion(int ExitCode, bool WasCancelled, string StandardError);

public sealed class BackendProcess : IDisposable
{
    private const string EmbeddedBackendName = "TimeLapseNative.timelapse-backend.exe";
    private static readonly TimeSpan CancellationGracePeriod = TimeSpan.FromSeconds(5);
    private Process? _process;
    private bool _cancelRequested;
    private string? _cancellationPath;

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

    public async Task<BackendCompletion> RunAsync(
        object request,
        Action<BackendEvent> onEvent,
        CancellationToken cancellationToken = default,
        string? cancellationPath = null)
    {
        _cancelRequested = false;
        _cancellationPath = cancellationPath;
        DeleteCancellationSentinel();
        var startInfo = new ProcessStartInfo(ExecutablePath())
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        _process = new Process { StartInfo = startInfo, EnableRaisingEvents = true };
        if (!_process.Start()) throw new InvalidOperationException("The timelapse backend could not be started.");

        try
        {
            using var registration = cancellationToken.Register(Cancel);
            await _process.StandardInput.WriteLineAsync(JsonSerializer.Serialize(request));
            _process.StandardInput.Close();
            var stderrTask = _process.StandardError.ReadToEndAsync();
            string? line;
            while ((line = await _process.StandardOutput.ReadLineAsync()) is not null)
            {
                if (string.IsNullOrWhiteSpace(line)) continue;
                try
                {
                    var backendEvent = JsonSerializer.Deserialize<BackendEvent>(line);
                    if (backendEvent is not null) onEvent(backendEvent);
                }
                catch (JsonException exception)
                {
                    onEvent(new BackendEvent { Event = "log", Level = "WARNING", Message = $"Invalid backend event: {exception.Message}" });
                }
            }
            await _process.WaitForExitAsync();
            return new BackendCompletion(_process.ExitCode, _cancelRequested, await stderrTask);
        }
        finally
        {
            DeleteCancellationSentinel();
        }
    }

    public void Cancel()
    {
        _cancelRequested = true;
        var process = _process;
        if (process is not { HasExited: false }) return;
        if (_cancellationPath is not null)
        {
            try
            {
                File.WriteAllText(_cancellationPath, "cancel");
                _ = ForceKillAfterGracePeriodAsync(process);
                return;
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
        try
        {
            process.Kill(entireProcessTree: true);
        }
        catch (InvalidOperationException) { }
    }

    private static async Task ForceKillAfterGracePeriodAsync(Process process)
    {
        await Task.Delay(CancellationGracePeriod);
        try
        {
            if (!process.HasExited) process.Kill(entireProcessTree: true);
        }
        catch (InvalidOperationException) { }
    }

    private void DeleteCancellationSentinel()
    {
        if (_cancellationPath is null) return;
        try { File.Delete(_cancellationPath); }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
    }

    public void Dispose()
    {
        _process?.Dispose();
        GC.SuppressFinalize(this);
    }
}
