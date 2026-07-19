using System.ComponentModel;
using System.Globalization;
using System.Runtime.CompilerServices;
using System.Text.Json.Serialization;

namespace TimeLapseNative;

public sealed record ConnectionSettings
{
    [JsonPropertyName("instance_url")]
    public string InstanceUrl { get; init; } = "";

    [JsonPropertyName("token")]
    public string Token { get; init; } = "";

    [JsonPropertyName("username")]
    public string Username { get; init; } = "";

    [JsonPropertyName("password")]
    public string Password { get; init; } = "";

    [JsonPropertyName("verify_ssl")]
    public bool VerifySsl { get; init; } = true;

    [JsonPropertyName("request_timeout_seconds")]
    public int RequestTimeoutSeconds { get; init; }

    [JsonPropertyName("max_download_mib")]
    public int MaxDownloadMiB { get; init; } = 10 * 1024;

    public ConnectionSettings Normalized() => this with
    {
        InstanceUrl = InstanceUrl.Trim().TrimEnd('/'),
        Token = Token.Trim(),
        Username = Username.Trim(),
    };

    public string? ValidationError()
    {
        var missing = new List<string>();
        if (string.IsNullOrWhiteSpace(InstanceUrl)) missing.Add("Protect URL");
        if (string.IsNullOrWhiteSpace(Token)) missing.Add("API token");
        if (string.IsNullOrWhiteSpace(Username)) missing.Add("local username");
        if (string.IsNullOrWhiteSpace(Password)) missing.Add("local password");
        if (missing.Count > 0) return $"Please provide: {string.Join(", ", missing)}.";
        if (!Uri.TryCreate(InstanceUrl, UriKind.Absolute, out var uri) || uri.Scheme != Uri.UriSchemeHttps)
            return "The Protect URL must be a complete HTTPS URL.";
        if (!string.IsNullOrEmpty(uri.UserInfo))
            return "The Protect URL must not contain credentials. Use the dedicated fields instead.";
        if (!string.IsNullOrEmpty(uri.Query) || !string.IsNullOrEmpty(uri.Fragment))
            return "The Protect URL must not contain a query string or fragment.";
        if (RequestTimeoutSeconds < 0 || MaxDownloadMiB < 0)
            return "Timeout and maximum download size must be zero or positive whole numbers.";
        return null;
    }
}

public sealed record ConnectionProfile(Guid Id, string Name, ConnectionSettings Settings)
{
    [JsonIgnore]
    public string DisplayName => string.IsNullOrWhiteSpace(Name) ? Settings.InstanceUrl : Name.Trim();

    public ConnectionProfile Normalized() => this with
    {
        Name = DisplayName,
        Settings = Settings.Normalized(),
    };
}

public sealed record CameraInfo(
    [property: JsonPropertyName("id")] string Id,
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("state")] string? State,
    [property: JsonPropertyName("model")] string? Model);

public sealed record CameraThumbnail(byte[] Image, string Source);

public sealed record BackendEvent
{
    [JsonPropertyName("id")]
    public string? Id { get; init; }

    [JsonPropertyName("event")]
    public string Event { get; init; } = "";

    [JsonPropertyName("level")]
    public string? Level { get; init; }

    [JsonPropertyName("message")]
    public string? Message { get; init; }

    [JsonPropertyName("cameras")]
    public List<CameraInfo>? Cameras { get; init; }

    [JsonPropertyName("downloaded_bytes")]
    public long? DownloadedBytes { get; init; }

    [JsonPropertyName("total_bytes")]
    public long? TotalBytes { get; init; }

    [JsonPropertyName("bytes_per_second")]
    public double? BytesPerSecond { get; init; }

    [JsonPropertyName("elapsed_seconds")]
    public double? ElapsedSeconds { get; init; }

    [JsonPropertyName("output")]
    public string? Output { get; init; }

    [JsonPropertyName("thumbnail_base64")]
    public string? ThumbnailBase64 { get; init; }

    [JsonPropertyName("thumbnail_source")]
    public string? ThumbnailSource { get; init; }
}

public enum DownloadState
{
    Scheduled,
    Preparing,
    Downloading,
    Cancelling,
    Completed,
    Cancelled,
    Failed,
    Stopped,
}

public sealed class DownloadJob : INotifyPropertyChanged
{
    private DownloadState _state = DownloadState.Preparing;
    private string _error = "";
    private long _downloadedBytes;
    private long? _totalBytes;
    private double _bytesPerSecond;

    public Guid Id { get; init; } = Guid.NewGuid();
    public required int GroupNumber { get; init; }
    public required CameraInfo Camera { get; init; }
    public required string OutputPath { get; init; }
    public required ConnectionSettings RequestSettings { get; init; }
    public required string RequestStart { get; init; }
    public required string RequestEnd { get; init; }
    public required string RequestSpeed { get; init; }
    public bool IsDailySchedule { get; init; }
    public DateTime LastProgressAt { get; private set; }

    public DownloadState State
    {
        get => _state;
        set { _state = value; NotifyAll(); }
    }

    public string Error
    {
        get => _error;
        set { _error = value; NotifyAll(); }
    }

    public long DownloadedBytes
    {
        get => _downloadedBytes;
        set { _downloadedBytes = value; LastProgressAt = DateTime.Now; NotifyAll(); }
    }

    public long? TotalBytes
    {
        get => _totalBytes;
        set { _totalBytes = value; NotifyAll(); }
    }

    public double BytesPerSecond
    {
        get => _bytesPerSecond;
        set { _bytesPerSecond = value; NotifyAll(); }
    }

    public bool IsTerminal => State is DownloadState.Completed or DownloadState.Cancelled or DownloadState.Failed or DownloadState.Stopped;
    public string CameraName => Camera.Name;
    public string TimeRangeText
    {
        get
        {
            if (IsDailySchedule) return "Next completed day";
            if (!DateTimeOffset.TryParse(RequestStart, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind, out var start)
                || !DateTimeOffset.TryParse(RequestEnd, CultureInfo.InvariantCulture, DateTimeStyles.RoundtripKind, out var end))
                return "—";
            var localStart = start.ToLocalTime();
            var localEnd = end.ToLocalTime();
            var endText = localStart.Date == localEnd.Date
                ? localEnd.ToString("t", CultureInfo.CurrentCulture)
                : localEnd.ToString("g", CultureInfo.CurrentCulture);
            return $"{localStart.ToString("g", CultureInfo.CurrentCulture)} → {endText}";
        }
    }
    public string OutputName => Path.GetFileName(OutputPath);
    public string StatusText => State switch
    {
        DownloadState.Scheduled => "Scheduled daily",
        DownloadState.Preparing => "Preparing export…",
        DownloadState.Downloading => "Downloading",
        DownloadState.Cancelling => "Cancelling…",
        DownloadState.Completed => "Completed",
        DownloadState.Cancelled => "Cancelled",
        DownloadState.Failed => $"Failed: {Error}",
        DownloadState.Stopped => "Stopped",
        _ => State.ToString(),
    };
    public double Progress => TotalBytes > 0 ? Math.Min((double)DownloadedBytes / TotalBytes.Value * 100, 100) : 0;
    public string DownloadedText => FormatBytes(DownloadedBytes);
    public string ExpectedText => TotalBytes.HasValue ? FormatBytes(TotalBytes.Value) : "Unknown";
    public string SpeedText => BytesPerSecond > 0 && DateTime.Now - LastProgressAt < TimeSpan.FromSeconds(2)
        ? $"{FormatBytes((long)BytesPerSecond)}/s" : "—";
    public string ActionText => State switch
    {
        DownloadState.Scheduled => "Stop",
        DownloadState.Completed => "Show",
        DownloadState.Cancelled or DownloadState.Failed => "Restart",
        DownloadState.Cancelling => "Cancelling…",
        DownloadState.Stopped => "Remove",
        _ => "Cancel",
    };

    public event PropertyChangedEventHandler? PropertyChanged;

    public void NotifyAll() => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(null));

    private static string FormatBytes(long value)
    {
        string[] units = ["bytes", "KiB", "MiB", "GiB", "TiB"];
        var amount = (double)Math.Max(value, 0);
        var unit = 0;
        while (amount >= 1024 && unit < units.Length - 1) { amount /= 1024; unit++; }
        return unit == 0 ? $"{value} bytes" : $"{amount:0.##} {units[unit]}";
    }
}
