using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Microsoft.Win32;
using Drawing = System.Drawing;
using Forms = System.Windows.Forms;

namespace TimeLapseNative;

public partial class MainWindow : Window
{
    public ObservableCollection<DownloadJob> DownloadJobs { get; } = [];
    public ObservableCollection<DownloadJob> DailyAutomationJobs { get; } = [];
    public ObservableCollection<string> Logs { get; } = [];

    private readonly ObservableCollection<ConnectionProfile> _profiles = [];
    private readonly List<CameraInfo> _cameras = [];
    private readonly HashSet<string> _selectedCameraIds = [];
    private readonly Dictionary<Guid, BackendProcess> _downloadProcesses = [];
    private readonly Forms.NotifyIcon _notificationIcon;
    private readonly HashSet<BackendProcess> _thumbnailProcesses = [];
    private readonly Dictionary<string, CameraThumbnail> _thumbnailCache = [];
    private readonly Dictionary<string, string> _thumbnailFailures = [];
    private readonly Dictionary<string, BackendProcess> _thumbnailRequests = [];
    private readonly HashSet<string> _reservedOutputPaths = new(StringComparer.OrdinalIgnoreCase);
    private readonly DispatcherTimer _dailyTimer = new() { Interval = TimeSpan.FromMinutes(1) };
    private readonly DispatcherTimer _startThumbnailTimer = new() { Interval = TimeSpan.FromMilliseconds(400) };
    private readonly DispatcherTimer _endThumbnailTimer = new() { Interval = TimeSpan.FromMilliseconds(400) };
    private ConnectionProfile? _selectedProfile;
    private BackendProcess? _cameraProcess;
    private string _outputDirectory;
    private int _nextGroupNumber = 1;
    private bool _loadingProfiles;
    private bool _isLoadingCameras;
    private bool _allowClose;
    private bool _adjustingFullDay;
    private bool _updatingDailyToggle;
    private string? _hoveredThumbnailBoundary;
    private string? _visibleThumbnailKey;
    private int _thumbnailGeneration;
    private DailySchedule? _dailySchedule;

    private sealed class DailySchedule
    {
        public required List<CameraInfo> Cameras { get; init; }
        public required string OutputDirectory { get; init; }
        public required ConnectionSettings Settings { get; init; }
        public required string Speed { get; init; }
        public required DownloadJob Job { get; init; }
        public DateTime? LastRunDay { get; set; }
        public DateTime? ActiveDay { get; set; }
        public HashSet<Guid> ActiveJobIds { get; } = [];
    }

    public MainWindow()
    {
        InitializeComponent();
        _notificationIcon = new Forms.NotifyIcon
        {
            Icon = Drawing.Icon.ExtractAssociatedIcon(Environment.ProcessPath ?? "") ?? Drawing.SystemIcons.Application,
            Text = "UniFi Protect Timelapse",
            Visible = true,
        };
        JobsTabControl.SelectionChanged += JobsTabControl_SelectionChanged;
        DataContext = this;
        ProfileCombo.ItemsSource = _profiles;
        var videos = Environment.GetFolderPath(Environment.SpecialFolder.MyVideos);
        _outputDirectory = Path.Combine(string.IsNullOrWhiteSpace(videos) ? Environment.GetFolderPath(Environment.SpecialFolder.UserProfile) : videos, "TimeLapse");
        CleanupStalePartialExports(_outputDirectory);
        OutputDirectoryText.Text = _outputDirectory;
        OutputDirectoryText.ToolTip = _outputDirectory;
        var end = DateTime.Now;
        var start = end.AddDays(-1);
        StartDatePicker.SelectedDate = start.Date;
        EndDatePicker.SelectedDate = end.Date;
        StartTimeText.Text = start.ToString("t", CultureInfo.CurrentCulture);
        EndTimeText.Text = end.ToString("t", CultureInfo.CurrentCulture);
        _startThumbnailTimer.Tick += (_, _) => ThumbnailTimerElapsed("start", _startThumbnailTimer);
        _endThumbnailTimer.Tick += (_, _) => ThumbnailTimerElapsed("end", _endThumbnailTimer);
        _dailyTimer.Tick += (_, _) => RunDailyScheduleIfDue();
        _dailyTimer.Start();
        UpdateDownloadsDisplay();
    }

    private void Window_Loaded(object sender, RoutedEventArgs e)
    {
        try
        {
            _loadingProfiles = true;
            var state = ProfileStore.Load();
            foreach (var profile in state.Profiles) _profiles.Add(profile);
            _selectedProfile = _profiles.FirstOrDefault(profile => profile.Id == state.SelectedProfileId) ?? _profiles.FirstOrDefault();
            ProfileCombo.SelectedItem = _selectedProfile;
            UpdateConnectionDisplay();
            AppendLog("INFO", "Application ready");
        }
        catch (Exception exception)
        {
            ShowError("Could Not Read Profiles", $"Saved connection profiles could not be read from Windows Credential Manager: {exception.Message}");
        }
        finally { _loadingProfiles = false; }

        if (_profiles.Count == 0) ShowConnectionDialog(null, firstRun: true);
    }

    private void NewProfile_Click(object sender, RoutedEventArgs e) => ShowConnectionDialog(null, firstRun: false);
    private void EditProfile_Click(object sender, RoutedEventArgs e) => ShowConnectionDialog(_selectedProfile, firstRun: false);

    private void ShowConnectionDialog(ConnectionProfile? profile, bool firstRun)
    {
        if (_isLoadingCameras) { ShowMessage("Loading Cameras", "Wait for the current camera refresh to finish."); return; }
        var dialog = new ConnectionDialog(profile) { Owner = this };
        if (dialog.ShowDialog() != true || dialog.Result is null)
        {
            if (firstRun) Close();
            return;
        }
        var replacement = dialog.Result;
        var index = _profiles.ToList().FindIndex(candidate => candidate.Id == replacement.Id);
        if (index >= 0) _profiles[index] = replacement; else _profiles.Add(replacement);
        _selectedProfile = replacement;
        ProfileCombo.SelectedItem = replacement;
        try { SaveProfiles(); }
        catch (Exception exception) { ShowError("Could Not Save Profile", exception.Message); return; }
        _cameras.Clear();
        _selectedCameraIds.Clear();
        ClearThumbnailPreviews();
        UpdateConnectionDisplay();
        UpdateCameraSummary();
        StatusText.Text = $"Saved {replacement.DisplayName}";
        AppendLog("INFO", StatusText.Text);
    }

    private void ProfileCombo_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_loadingProfiles || ProfileCombo.SelectedItem is not ConnectionProfile profile || profile.Id == _selectedProfile?.Id) return;
        _selectedProfile = profile;
        try { SaveProfiles(); }
        catch (Exception exception) { ShowError("Could Not Select Profile", exception.Message); return; }
        _cameras.Clear();
        _selectedCameraIds.Clear();
        ClearThumbnailPreviews();
        UpdateConnectionDisplay();
        UpdateCameraSummary();
        StatusText.Text = $"Selected {profile.DisplayName}";
        AppendLog("INFO", StatusText.Text);
    }

    private void SaveProfiles() => ProfileStore.Save(new ProfileState(_profiles.ToList(), _selectedProfile?.Id));

    private void UpdateConnectionDisplay()
    {
        EditProfileButton.IsEnabled = _selectedProfile is not null;
        ConnectionUrlText.Text = _selectedProfile?.Settings.InstanceUrl ?? "Create a profile to connect to UniFi Protect.";
        ConnectionUrlText.ToolTip = ConnectionUrlText.Text;
    }

    private void ChooseOutput_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFolderDialog { Title = "Choose Output Folder", InitialDirectory = _outputDirectory };
        if (dialog.ShowDialog(this) != true) return;
        _outputDirectory = dialog.FolderName;
        OutputDirectoryText.Text = _outputDirectory;
        OutputDirectoryText.ToolTip = _outputDirectory;
    }

    private void FullDay_Checked(object sender, RoutedEventArgs e)
    {
        StartTimeText.Visibility = Visibility.Collapsed;
        EndTimeText.Visibility = Visibility.Collapsed;
        SetFullDayFromStart(StartDatePicker.SelectedDate ?? DateTime.Today.AddDays(-1));
    }

    private void FullDay_Unchecked(object sender, RoutedEventArgs e)
    {
        StartTimeText.Visibility = Visibility.Visible;
        EndTimeText.Visibility = Visibility.Visible;
    }

    private void StartDate_SelectedDateChanged(object sender, SelectionChangedEventArgs e)
    {
        if (FullDayCheckBox.IsChecked == true && !_adjustingFullDay && StartDatePicker.SelectedDate is DateTime start)
            SetFullDayFromStart(start);
        RefreshThumbnail("start");
    }

    private void EndDate_SelectedDateChanged(object sender, SelectionChangedEventArgs e)
    {
        if (FullDayCheckBox.IsChecked == true && !_adjustingFullDay && EndDatePicker.SelectedDate is DateTime end)
        {
            _adjustingFullDay = true;
            EndDatePicker.SelectedDate = end.Date;
            StartDatePicker.SelectedDate = end.Date.AddDays(-1);
            _adjustingFullDay = false;
        }
        RefreshThumbnail("end");
    }

    private void SetFullDayFromStart(DateTime value)
    {
        _adjustingFullDay = true;
        StartDatePicker.SelectedDate = value.Date;
        EndDatePicker.SelectedDate = value.Date.AddDays(1);
        StartTimeText.Text = DateTime.Today.ToString("t", CultureInfo.CurrentCulture);
        EndTimeText.Text = DateTime.Today.ToString("t", CultureInfo.CurrentCulture);
        _adjustingFullDay = false;
    }

    private async void ThumbnailHover_MouseEnter(object sender, System.Windows.Input.MouseEventArgs e)
    {
        if (sender is not FrameworkElement { Tag: string boundary } target) return;
        _hoveredThumbnailBoundary = boundary;
        await ShowThumbnailPreviewAsync(boundary, target);
    }

    private void ThumbnailHover_MouseLeave(object sender, System.Windows.Input.MouseEventArgs e)
    {
        if (sender is FrameworkElement { Tag: string boundary } && _hoveredThumbnailBoundary == boundary)
        {
            _hoveredThumbnailBoundary = null;
            ThumbnailPopup.IsOpen = false;
        }
    }

    private void TimeText_TextChanged(object sender, TextChangedEventArgs e)
    {
        if (!IsLoaded) return;
        var timer = ReferenceEquals(sender, StartTimeText) ? _startThumbnailTimer : _endThumbnailTimer;
        timer.Stop();
        timer.Start();
    }

    private void ThumbnailTimerElapsed(string boundary, DispatcherTimer timer)
    {
        timer.Stop();
        RefreshThumbnail(boundary);
    }

    private void RefreshThumbnail(string boundary)
    {
        if (!IsLoaded) return;
        var target = boundary == "start" ? StartDateTimeHoverTarget : EndDateTimeHoverTarget;
        _ = ShowThumbnailPreviewAsync(boundary, target, display: _hoveredThumbnailBoundary == boundary);
    }

    private async Task ShowThumbnailPreviewAsync(string boundary, FrameworkElement target, bool display = true)
    {
        var generation = _thumbnailGeneration;
        var picker = boundary == "start" ? StartDatePicker : EndDatePicker;
        var timeBox = boundary == "start" ? StartTimeText : EndTimeText;
        DateTime timestamp;
        if (FullDayCheckBox.IsChecked == true && picker.SelectedDate is DateTime selectedDate)
            timestamp = selectedDate.Date;
        else if (!TryReadDateTime(picker, timeBox, out timestamp))
        {
            if (display)
            {
                _visibleThumbnailKey = null;
                PrepareThumbnailPopup(boundary, DateTime.MinValue, "");
                ShowThumbnailMessage("Enter a valid date and time to preview the recording.");
                OpenThumbnailPopup(target);
            }
            return;
        }

        var selected = _cameras.Where(camera => _selectedCameraIds.Contains(camera.Id)).ToList();
        var camera = selected.FirstOrDefault();
        var cameraText = camera is null ? "" : selected.Count > 1 ? $"{camera.Name} · first selected camera" : camera.Name;
        if (display)
        {
            PrepareThumbnailPopup(boundary, timestamp, cameraText);
            OpenThumbnailPopup(target);
        }
        if (camera is null)
        {
            if (display)
            {
                _visibleThumbnailKey = null;
                ShowThumbnailMessage("Select a camera to preview this time.");
            }
            return;
        }

        var localTimestamp = new DateTimeOffset(DateTime.SpecifyKind(timestamp, DateTimeKind.Local));
        var cacheKey = $"{_selectedProfile!.Id}|{camera.Id}|{localTimestamp.ToUnixTimeSeconds()}";
        if (display) _visibleThumbnailKey = cacheKey;
        if (_thumbnailCache.TryGetValue(cacheKey, out var cachedImage))
        {
            if (display) ShowThumbnailImage(cachedImage);
            return;
        }
        if (_thumbnailFailures.TryGetValue(cacheKey, out var cachedFailure))
        {
            if (display) ShowThumbnailMessage(cachedFailure);
            return;
        }
        if (_thumbnailRequests.ContainsKey(cacheKey))
        {
            if (display) ShowThumbnailMessage("Loading thumbnail…");
            return;
        }

        if (display) ShowThumbnailMessage("Loading thumbnail…");
        var process = new BackendProcess();
        _thumbnailProcesses.Add(process);
        _thumbnailRequests[cacheKey] = process;
        var requestId = Guid.NewGuid().ToString();
        var receivedTerminal = false;
        try
        {
            var request = new Dictionary<string, object>
            {
                ["id"] = requestId,
                ["command"] = "thumbnail",
                ["settings"] = _selectedProfile!.Settings,
                ["camera"] = camera,
                ["timestamp"] = localTimestamp.ToString("O"),
            };
            var completion = await process.RunAsync(request, backendEvent => Dispatcher.Invoke(() =>
            {
                if (backendEvent.Id is not null && backendEvent.Id != requestId) return;
                switch (backendEvent.Event)
                {
                    case "thumbnail":
                        receivedTerminal = true;
                        try
                        {
                            var image = Convert.FromBase64String(backendEvent.ThumbnailBase64 ?? "");
                            if (generation == _thumbnailGeneration)
                            {
                                var thumbnail = new CameraThumbnail(image, backendEvent.ThumbnailSource ?? "exact");
                                _thumbnailCache[cacheKey] = thumbnail;
                                if (_hoveredThumbnailBoundary == boundary && _visibleThumbnailKey == cacheKey)
                                    ShowThumbnailImage(thumbnail);
                            }
                        }
                        catch (FormatException)
                        {
                            var invalidImageMessage = "The thumbnail data could not be read.";
                            if (generation == _thumbnailGeneration)
                                _thumbnailFailures[cacheKey] = invalidImageMessage;
                            if (_hoveredThumbnailBoundary == boundary && _visibleThumbnailKey == cacheKey)
                                ShowThumbnailMessage(invalidImageMessage);
                        }
                        break;
                    case "error":
                        receivedTerminal = true;
                        var message = backendEvent.Message ?? "No thumbnail is available for this time.";
                        if (generation == _thumbnailGeneration) _thumbnailFailures[cacheKey] = message;
                        if (_hoveredThumbnailBoundary == boundary && _visibleThumbnailKey == cacheKey)
                            ShowThumbnailMessage(message);
                        break;
                    case "log": AppendLog(backendEvent.Level ?? "INFO", backendEvent.Message ?? ""); break;
                }
            }));
            if (!receivedTerminal && !completion.WasCancelled)
            {
                var message = string.IsNullOrWhiteSpace(completion.StandardError)
                    ? "No thumbnail is available for this time."
                    : completion.StandardError.Trim();
                if (generation == _thumbnailGeneration) _thumbnailFailures[cacheKey] = message;
                if (_hoveredThumbnailBoundary == boundary && _visibleThumbnailKey == cacheKey)
                    ShowThumbnailMessage(message);
            }
        }
        catch (Exception exception)
        {
            if (generation == _thumbnailGeneration) _thumbnailFailures[cacheKey] = exception.Message;
            if (_hoveredThumbnailBoundary == boundary && _visibleThumbnailKey == cacheKey)
                ShowThumbnailMessage(exception.Message);
        }
        finally
        {
            if (_thumbnailRequests.TryGetValue(cacheKey, out var active) && ReferenceEquals(active, process))
                _thumbnailRequests.Remove(cacheKey);
            _thumbnailProcesses.Remove(process);
            process.Dispose();
        }
    }

    private void PrepareThumbnailPopup(string boundary, DateTime timestamp, string cameraText)
    {
        ThumbnailTitle.Text = $"{char.ToUpperInvariant(boundary[0])}{boundary[1..]} preview";
        ThumbnailTime.Text = timestamp == DateTime.MinValue ? "" : timestamp.ToString("g", CultureInfo.CurrentCulture);
        ThumbnailCamera.Text = cameraText;
        ThumbnailSource.Text = "";
        ThumbnailImage.Source = null;
    }

    private void OpenThumbnailPopup(FrameworkElement target)
    {
        ThumbnailPopup.PlacementTarget = target;
        ThumbnailPopup.IsOpen = true;
    }

    private void ShowThumbnailMessage(string message)
    {
        ThumbnailImage.Source = null;
        ThumbnailMessage.Text = message;
        ThumbnailMessage.Visibility = Visibility.Visible;
    }

    private void ShowThumbnailImage(CameraThumbnail thumbnail)
    {
        try
        {
            using var stream = new MemoryStream(thumbnail.Image);
            var bitmap = new BitmapImage();
            bitmap.BeginInit();
            bitmap.CacheOption = BitmapCacheOption.OnLoad;
            bitmap.StreamSource = stream;
            bitmap.EndInit();
            bitmap.Freeze();
            ThumbnailImage.Source = bitmap;
            ThumbnailMessage.Text = "";
            ThumbnailMessage.Visibility = Visibility.Collapsed;
            ThumbnailSource.Text = thumbnail.Source == "live"
                ? "Live snapshot · exact selected-time frame unavailable"
                : "";
        }
        catch (Exception exception) when (exception is ArgumentException or InvalidOperationException or NotSupportedException)
        {
            ShowThumbnailMessage("The thumbnail data could not be read.");
        }
    }

    private void ClearThumbnailPreviews()
    {
        _startThumbnailTimer.Stop();
        _endThumbnailTimer.Stop();
        _thumbnailGeneration++;
        _hoveredThumbnailBoundary = null;
        _visibleThumbnailKey = null;
        ThumbnailPopup.IsOpen = false;
        _thumbnailCache.Clear();
        _thumbnailFailures.Clear();
        _thumbnailRequests.Clear();
        foreach (var process in _thumbnailProcesses.ToList()) process.Cancel();
    }

    private async void DailyAutomatic_Checked(object sender, RoutedEventArgs e)
    {
        if (_updatingDailyToggle || _dailySchedule is not null) return;
        if (_cameras.Count == 0) await LoadCamerasAsync(openSelection: false);
        if (DailyAutomaticCheckBox.IsChecked != true) return;
        if (_cameras.Count == 0) { SetDailyToggle(false); return; }
        var dialog = new DailyScheduleDialog(_cameras, _outputDirectory) { Owner = this };
        if (dialog.ShowDialog() != true) { SetDailyToggle(false); return; }
        if (!TryEnsureOutputDirectory(dialog.OutputDirectory)) { SetDailyToggle(false); return; }
        ConfigureDailySchedule(dialog.SelectedCameras, dialog.OutputDirectory);
    }

    private void DailyAutomatic_Unchecked(object sender, RoutedEventArgs e)
    {
        if (!_updatingDailyToggle) StopDailySchedule();
    }

    private void ConfigureDailySchedule(List<CameraInfo> cameras, string outputDirectory)
    {
        if (_selectedProfile is null) return;
        var group = _nextGroupNumber++;
        var scheduleCamera = new CameraInfo(
            $"daily-schedule-{Guid.NewGuid()}",
            cameras.Count == 1 ? cameras[0].Name : $"{cameras.Count} cameras",
            null,
            null);
        var job = new DownloadJob
        {
            GroupNumber = group,
            Camera = scheduleCamera,
            OutputPath = outputDirectory,
            RequestSettings = _selectedProfile.Settings,
            RequestStart = "",
            RequestEnd = "",
            RequestSpeed = (SpeedCombo.SelectedItem as ComboBoxItem)?.Content?.ToString() ?? "600x",
            IsDailySchedule = true,
            State = DownloadState.Scheduled,
        };
        DailyAutomationJobs.Add(job);
        _dailySchedule = new DailySchedule
        {
            Cameras = cameras,
            OutputDirectory = outputDirectory,
            Settings = _selectedProfile.Settings,
            Speed = job.RequestSpeed,
            Job = job,
        };
        UpdateDownloadsDisplay();
        StatusText.Text = $"Scheduled daily timelapses for {cameras.Count} cameras";
        AppendLog("INFO", StatusText.Text);
        RunDailyScheduleIfDue();
    }

    private void RunDailyScheduleIfDue()
    {
        var schedule = _dailySchedule;
        if (schedule is null) return;
        if (schedule.ActiveJobIds.Count > 0)
        {
            if (schedule.ActiveJobIds.Any(_downloadProcesses.ContainsKey)) return;
            var tracked = DownloadJobs.Where(job => schedule.ActiveJobIds.Contains(job.Id)).ToList();
            var completed = tracked.Count == schedule.ActiveJobIds.Count
                && tracked.All(job => job.State == DownloadState.Completed && IsValidExport(job.OutputPath));
            if (completed) schedule.LastRunDay = schedule.ActiveDay;
            schedule.ActiveDay = null;
            schedule.ActiveJobIds.Clear();
            if (!completed) return;
        }
        var today = DateTime.Today;
        var day = schedule.LastRunDay?.AddDays(1) ?? today.AddDays(-1);
        while (day < today)
        {
            var missing = schedule.Cameras
                .Where(camera => !IsValidExport(ExpectedOutputPath(
                    camera, day, day.AddDays(1), schedule.Speed, schedule.OutputDirectory, daily: true)))
                .ToList();
            if (missing.Count == 0)
            {
                schedule.LastRunDay = day;
                day = day.AddDays(1);
                continue;
            }
            var group = _nextGroupNumber++;
            schedule.ActiveDay = day;
            foreach (var camera in missing)
            {
                var job = StartDownload(
                    camera,
                    group,
                    day,
                    day.AddDays(1),
                    schedule.Speed,
                    schedule.OutputDirectory,
                    daily: true,
                    fullDay: true,
                    requestSettings: schedule.Settings);
                schedule.ActiveJobIds.Add(job.Id);
            }
            StatusText.Text = $"Started daily job {group} for {day:yyyy-MM-dd}";
            AppendLog("INFO", StatusText.Text);
            return;
        }
    }

    private void StopDailySchedule()
    {
        var schedule = _dailySchedule;
        if (schedule is null) return;
        _dailySchedule = null;
        schedule.Job.State = DownloadState.Stopped;
        SetDailyToggle(false);
        UpdateDownloadsDisplay();
        StatusText.Text = "Stopped daily automatic timelapses";
        AppendLog("INFO", StatusText.Text);
    }

    private void SetDailyToggle(bool value)
    {
        _updatingDailyToggle = true;
        DailyAutomaticCheckBox.IsChecked = value;
        _updatingDailyToggle = false;
    }

    private async void SelectCameras_Click(object sender, RoutedEventArgs e)
    {
        if (_cameras.Count == 0) await LoadCamerasAsync(openSelection: true);
        else ShowCameraDialog();
    }

    private async void RefreshCameras_Click(object sender, RoutedEventArgs e) => await LoadCamerasAsync(openSelection: true);

    private async Task LoadCamerasAsync(bool openSelection)
    {
        if (_isLoadingCameras) return;
        if (_selectedProfile is null) { ShowConnectionDialog(null, firstRun: false); return; }
        var validationError = _selectedProfile.Settings.ValidationError();
        if (validationError is not null) { ShowMessage("Invalid Connection", validationError); return; }

        _isLoadingCameras = true;
        SetBusyState();
        StatusText.Text = "Loading cameras…";
        AppendLog("INFO", "Loading cameras");
        var requestId = Guid.NewGuid().ToString();
        var receivedTerminal = false;
        _cameraProcess = new BackendProcess();
        try
        {
            var request = new Dictionary<string, object>
            {
                ["id"] = requestId,
                ["command"] = "list_cameras",
                ["settings"] = _selectedProfile.Settings,
            };
            var completion = await _cameraProcess.RunAsync(request, backendEvent => Dispatcher.Invoke(() =>
            {
                if (backendEvent.Id is not null && backendEvent.Id != requestId) return;
                switch (backendEvent.Event)
                {
                    case "cameras":
                        receivedTerminal = true;
                        _cameras.Clear();
                        _cameras.AddRange(backendEvent.Cameras ?? []);
                        _selectedCameraIds.IntersectWith(_cameras.Select(camera => camera.Id));
                        StatusText.Text = $"Loaded {_cameras.Count} cameras";
                        AppendLog("INFO", StatusText.Text);
                        break;
                    case "error":
                        receivedTerminal = true;
                        ShowError("Could Not Load Cameras", backendEvent.Message ?? "An unknown backend error occurred.");
                        break;
                    case "log": AppendLog(backendEvent.Level ?? "INFO", backendEvent.Message ?? ""); break;
                    case "complete" or "cancelled": receivedTerminal = true; break;
                    default: AppendLog("WARNING", $"Unknown backend event: {backendEvent.Event}"); break;
                }
            }));
            if (!receivedTerminal && !completion.WasCancelled)
                ShowError("Could Not Load Cameras", string.IsNullOrWhiteSpace(completion.StandardError)
                    ? $"The backend exited with status {completion.ExitCode} without returning a camera list."
                    : completion.StandardError.Trim());
            else if (_cameras.Count == 0 && receivedTerminal)
                ShowMessage("No Cameras", "No cameras were returned by UniFi Protect.");
            else if (openSelection && _cameras.Count > 0)
                ShowCameraDialog();
        }
        catch (Exception exception) { ShowError("Could Not Load Cameras", exception.Message); }
        finally
        {
            _cameraProcess?.Dispose();
            _cameraProcess = null;
            _isLoadingCameras = false;
            SetBusyState();
        }
    }

    private void ShowCameraDialog()
    {
        var dialog = new CameraSelectionDialog(_cameras, _selectedCameraIds) { Owner = this };
        if (dialog.ShowDialog() != true) return;
        _selectedCameraIds.Clear();
        _selectedCameraIds.UnionWith(dialog.SelectedIds);
        ClearThumbnailPreviews();
        UpdateCameraSummary();
        AppendLog("INFO", $"Selected {_selectedCameraIds.Count} cameras");
        if (_selectedCameraIds.Count > 0)
        {
            RefreshThumbnail("start");
            RefreshThumbnail("end");
        }
    }

    private void UpdateCameraSummary()
    {
        var selected = _cameras.Where(camera => _selectedCameraIds.Contains(camera.Id)).ToList();
        CameraSummaryText.Text = selected.Count switch
        {
            0 => "No cameras selected",
            1 => selected[0].Name,
            _ => $"{selected.Count} cameras selected",
        };
        CameraSummaryText.ToolTip = string.Join(", ", selected.Select(camera => camera.Name));
        StartDownloadsButton.IsEnabled = selected.Count > 0;
    }

    private void StartDownloads_Click(object sender, RoutedEventArgs e)
    {
        if (_selectedProfile is null) return;
        if (!TryReadDateTime(StartDatePicker, StartTimeText, out var start) || !TryReadDateTime(EndDatePicker, EndTimeText, out var end))
        {
            ShowMessage("Invalid Date or Time", "Enter a valid date and time, such as 9:30 AM.");
            return;
        }
        if (end <= start) { ShowMessage("Invalid Date Range", "The end date and time must be after the start."); return; }
        if (!TryEnsureOutputDirectory(_outputDirectory)) return;
        var speed = (SpeedCombo.SelectedItem as ComboBoxItem)?.Content?.ToString() ?? "600x";
        var group = _nextGroupNumber++;
        var selected = _cameras.Where(camera => _selectedCameraIds.Contains(camera.Id)).ToList();
        foreach (var camera in selected)
            StartDownload(
                camera,
                group,
                start,
                end,
                speed,
                _outputDirectory,
                daily: false,
                fullDay: FullDayCheckBox.IsChecked == true,
                requestSettings: _selectedProfile.Settings);
        StatusText.Text = $"Started job {group} with {selected.Count} downloads";
        AppendLog("INFO", StatusText.Text);
    }

    private bool TryEnsureOutputDirectory(string outputDirectory)
    {
        if (File.Exists(outputDirectory))
        {
            ShowMessage("Invalid Output Folder", "The selected output location is not a folder.");
            return false;
        }
        try
        {
            Directory.CreateDirectory(outputDirectory);
            return true;
        }
        catch (Exception exception)
        {
            ShowError(
                "Could Not Create Output Folder",
                $"Windows could not create or access the selected output folder. Choose a writable folder and try again.\n\n{exception.Message}");
            return false;
        }
    }

    private DownloadJob StartDownload(
        CameraInfo camera,
        int group,
        DateTime start,
        DateTime end,
        string speed,
        string outputDirectory,
        bool daily,
        bool fullDay,
        ConnectionSettings requestSettings)
    {
        var job = new DownloadJob
        {
            GroupNumber = group,
            Camera = camera,
            OutputPath = ReserveOutputPath(camera, start, end, speed, outputDirectory, daily, fullDay),
            RequestSettings = requestSettings,
            RequestStart = new DateTimeOffset(DateTime.SpecifyKind(start, DateTimeKind.Local)).ToString("O"),
            RequestEnd = new DateTimeOffset(DateTime.SpecifyKind(end, DateTimeKind.Local)).ToString("O"),
            RequestSpeed = speed,
        };
        DownloadJobs.Add(job);
        UpdateDownloadsDisplay();
        _ = LaunchDownloadAsync(job);
        return job;
    }

    private async Task LaunchDownloadAsync(DownloadJob job)
    {
        var process = new BackendProcess();
        _downloadProcesses[job.Id] = process;
        SetBusyState();
        var receivedTerminal = false;
        var cancellationPath = $"{job.OutputPath}.{job.Id:N}.cancel";
        AppendLog("INFO", $"Started camera download: {job.Camera.Name} -> {job.OutputPath}");
        try
        {
            var request = new Dictionary<string, object>
            {
                ["id"] = job.Id.ToString(),
                ["command"] = "download",
                ["settings"] = job.RequestSettings,
                ["camera"] = job.Camera,
                ["start"] = job.RequestStart,
                ["end"] = job.RequestEnd,
                ["speed"] = job.RequestSpeed,
                ["output"] = job.OutputPath,
                ["cancel_path"] = cancellationPath,
            };
            var completion = await process.RunAsync(request, backendEvent => Dispatcher.Invoke(() =>
            {
                if (backendEvent.Id is not null && backendEvent.Id != job.Id.ToString()) return;
                switch (backendEvent.Event)
                {
                    case "progress":
                        if (job.State != DownloadState.Cancelling) job.State = DownloadState.Downloading;
                        if (backendEvent.DownloadedBytes.HasValue) job.DownloadedBytes = backendEvent.DownloadedBytes.Value;
                        if (backendEvent.TotalBytes.HasValue) job.TotalBytes = backendEvent.TotalBytes;
                        if (backendEvent.BytesPerSecond.HasValue) job.BytesPerSecond = backendEvent.BytesPerSecond.Value;
                        break;
                    case "complete":
                        job.State = DownloadState.Completed;
                        receivedTerminal = true;
                        AppendLog("INFO", $"Completed camera download: {job.Camera.Name}");
                        NotifyDownloadFinished(job);
                        break;
                    case "cancelled":
                        job.State = DownloadState.Cancelled;
                        receivedTerminal = true;
                        AppendLog("INFO", $"Cancelled camera download: {job.Camera.Name}");
                        NotifyDownloadFinished(job);
                        break;
                    case "error":
                        job.Error = backendEvent.Message ?? "An unknown backend error occurred.";
                        job.State = DownloadState.Failed;
                        receivedTerminal = true;
                        AppendLog("ERROR", $"Download failed for {job.Camera.Name}: {job.Error}");
                        NotifyDownloadFinished(job);
                        break;
                    case "log": AppendLog(backendEvent.Level ?? "INFO", backendEvent.Message ?? ""); break;
                    default: AppendLog("WARNING", $"Unknown backend event: {backendEvent.Event}"); break;
                }
            }), cancellationPath: cancellationPath);
            if (!receivedTerminal)
            {
                if (completion.WasCancelled) job.State = DownloadState.Cancelled;
                else { job.Error = string.IsNullOrWhiteSpace(completion.StandardError) ? $"Backend exited with status {completion.ExitCode}." : completion.StandardError.Trim(); job.State = DownloadState.Failed; }
                NotifyDownloadFinished(job);
            }
        }
        catch (Exception exception)
        {
            job.Error = exception.Message;
            job.State = DownloadState.Failed;
            AppendLog("ERROR", exception.Message);
            NotifyDownloadFinished(job);
        }
        finally
        {
            job.BytesPerSecond = 0;
            _downloadProcesses.Remove(job.Id);
            _reservedOutputPaths.Remove(job.OutputPath);
            CleanupPartialFilesForOutput(job.OutputPath);
            process.Dispose();
            StatusText.Text = _downloadProcesses.Count == 0 ? "Ready" : $"{_downloadProcesses.Count} downloads active";
            UpdateDownloadsDisplay();
            SetBusyState();
        }
    }

    private void JobAction_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as FrameworkElement)?.DataContext is not DownloadJob job) return;
        switch (job.State)
        {
            case DownloadState.Scheduled: StopDailySchedule(); break;
            case DownloadState.Stopped: DailyAutomationJobs.Remove(job); UpdateDownloadsDisplay(); break;
            case DownloadState.Completed: Reveal(job); break;
            case DownloadState.Cancelled or DownloadState.Failed: Restart(job); break;
            case DownloadState.Preparing or DownloadState.Downloading: Cancel(job); break;
        }
    }

    private void Cancel(DownloadJob job)
    {
        if (job.IsDailySchedule) { StopDailySchedule(); return; }
        if (job.IsTerminal || job.State == DownloadState.Cancelling) return;
        job.State = DownloadState.Cancelling;
        if (_downloadProcesses.TryGetValue(job.Id, out var process)) process.Cancel();
        AppendLog("INFO", $"Cancellation requested for {job.Camera.Name}");
        UpdateDownloadsDisplay();
    }

    private void Restart(DownloadJob job)
    {
        if (job.IsDailySchedule) return;
        if (File.Exists(job.OutputPath)) { ShowMessage("Output Already Exists", $"Move or remove {job.OutputName} before restarting this job."); return; }
        _reservedOutputPaths.Add(job.OutputPath);
        job.Error = ""; job.DownloadedBytes = 0; job.TotalBytes = null; job.BytesPerSecond = 0; job.State = DownloadState.Preparing;
        _ = LaunchDownloadAsync(job);
        StatusText.Text = $"Restarted download for {job.Camera.Name}";
    }

    private void Reveal(DownloadJob job)
    {
        var arguments = File.Exists(job.OutputPath) ? $"/select,\"{job.OutputPath}\"" : $"\"{Path.GetDirectoryName(job.OutputPath)}\"";
        Process.Start(new ProcessStartInfo("explorer.exe", arguments) { UseShellExecute = true });
    }

    private void JobsGrid_MouseDoubleClick(object sender, MouseButtonEventArgs e)
    {
        if ((sender as DataGrid)?.SelectedItem is not DownloadJob job || job.State != DownloadState.Completed) return;
        if (File.Exists(job.OutputPath)) Process.Start(new ProcessStartInfo(job.OutputPath) { UseShellExecute = true });
    }

    private void JobsTabControl_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (ReferenceEquals(e.Source, JobsTabControl)) UpdateDownloadsDisplay();
    }

    private void ClearAll_Click(object sender, RoutedEventArgs e)
    {
        var jobs = ShowingDailyAutomations ? DailyAutomationJobs : DownloadJobs;
        foreach (var job in jobs.Where(job => job.IsTerminal).ToList()) jobs.Remove(job);
        UpdateDownloadsDisplay();
    }

    private void CancelAll_Click(object sender, RoutedEventArgs e)
    {
        if (ShowingDailyAutomations)
        {
            StopDailySchedule();
            return;
        }
        foreach (var job in DownloadJobs.Where(job => !job.IsTerminal && job.State != DownloadState.Cancelling).ToList()) Cancel(job);
        StatusText.Text = "Cancelling downloads…";
    }

    private bool ShowingDailyAutomations => JobsTabControl.SelectedIndex == 1;

    private void UpdateDownloadsDisplay()
    {
        EmptyDownloadsPanel.Visibility = DownloadJobs.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        DownloadsGrid.Visibility = DownloadJobs.Count == 0 ? Visibility.Collapsed : Visibility.Visible;
        EmptyDailyAutomationsPanel.Visibility = DailyAutomationJobs.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        DailyAutomationsGrid.Visibility = DailyAutomationJobs.Count == 0 ? Visibility.Collapsed : Visibility.Visible;
        var visibleJobs = ShowingDailyAutomations ? DailyAutomationJobs : DownloadJobs;
        ClearAllButton.IsEnabled = visibleJobs.Any(job => job.IsTerminal);
        CancelAllButton.Content = ShowingDailyAutomations ? "Stop All" : "Cancel All";
        CancelAllButton.IsEnabled = ShowingDailyAutomations
            ? _dailySchedule is not null
            : DownloadJobs.Any(job => !job.IsTerminal && job.State != DownloadState.Cancelling);
        foreach (var job in DownloadJobs.Concat(DailyAutomationJobs)) job.NotifyAll();
    }

    private void Logs_Click(object sender, RoutedEventArgs e) => new LogsWindow(Logs) { Owner = this }.Show();

    private void SetBusyState()
    {
        var busy = _isLoadingCameras || _downloadProcesses.Count > 0;
        WorkingPanel.Visibility = busy ? Visibility.Visible : Visibility.Collapsed;
        SelectCamerasButton.IsEnabled = !_isLoadingCameras;
        RefreshCamerasButton.IsEnabled = !_isLoadingCameras;
        UpdateDownloadsDisplay();
    }

    private void Window_Closing(object? sender, CancelEventArgs e)
    {
        if (_allowClose || (_cameraProcess is null && _downloadProcesses.Count == 0 && _thumbnailProcesses.Count == 0))
        {
            _dailyTimer.Stop();
            _notificationIcon.Visible = false;
            _notificationIcon.Dispose();
            return;
        }
        if (_downloadProcesses.Count > 0
            && MessageBox.Show(
                this,
                "One or more download jobs are still running. Quit and interrupt them? Any partial download files will be removed.",
                "Download Jobs Are Still Running",
                MessageBoxButton.YesNo,
                MessageBoxImage.Warning) != MessageBoxResult.Yes)
        {
            e.Cancel = true;
            return;
        }
        _allowClose = true;
        _dailyTimer.Stop();
        _cameraProcess?.Cancel();
        foreach (var process in _downloadProcesses.Values.ToList()) process.Cancel();
        foreach (var process in _thumbnailProcesses.ToList()) process.Cancel();
        _notificationIcon.Visible = false;
        _notificationIcon.Dispose();
    }

    private void NotifyDownloadFinished(DownloadJob job)
    {
        var (title, message, icon) = job.State switch
        {
            DownloadState.Completed => ("Download complete", $"{job.Camera.Name}: {job.OutputName}", Forms.ToolTipIcon.Info),
            DownloadState.Cancelled => ("Download interrupted", $"The download for {job.Camera.Name} was cancelled.", Forms.ToolTipIcon.Warning),
            _ => ("Download failed", $"{job.Camera.Name}: {job.Error}", Forms.ToolTipIcon.Error),
        };
        if (message.Length > 240) message = message[..237] + "…";
        _notificationIcon.ShowBalloonTip(10_000, title, message, icon);
    }

    private string ReserveOutputPath(
        CameraInfo camera,
        DateTime start,
        DateTime end,
        string speed,
        string outputDirectory,
        bool daily,
        bool fullDay)
    {
        CleanupStalePartialExports(outputDirectory);
        var expected = ExpectedOutputPath(camera, start, end, speed, outputDirectory, daily, fullDay);
        if (daily)
        {
            if (_reservedOutputPaths.Add(expected)) return expected;
            throw new InvalidOperationException($"An export is already writing {Path.GetFileName(expected)}.");
        }
        var baseName = Path.GetFileNameWithoutExtension(expected);
        for (var counter = 1; ; counter++)
        {
            var suffix = counter == 1 ? "" : $"_{counter}";
            var candidate = Path.Combine(outputDirectory, baseName + suffix + ".mp4");
            if (!File.Exists(candidate) && _reservedOutputPaths.Add(candidate)) return candidate;
        }
    }

    private static string ExpectedOutputPath(
        CameraInfo camera,
        DateTime start,
        DateTime end,
        string speed,
        string outputDirectory,
        bool daily,
        bool fullDay = false)
    {
        var invalid = Path.GetInvalidFileNameChars().Concat([' ', '\t', '\r', '\n']).ToHashSet();
        var safe = string.Concat(camera.Name.Select(character => invalid.Contains(character) ? '_' : character)).Trim('.', '_', '-');
        if (string.IsNullOrWhiteSpace(safe)) safe = "camera";
        if (safe.Length > 48) safe = safe[..48].Trim('.', '_', '-');
        var digest = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(camera.Id))).ToLowerInvariant()[..12];
        var filename = fullDay || daily
            ? $"timelapse_{safe}_{start:yyyy_MM_dd}_{end:yyyy_MM_dd}_{speed}_{digest}.mp4"
            : $"timelapse_{safe}_{start:yyyy_MM_dd}_{start:HH_mm_ss}_{end:yyyy_MM_dd}_{end:HH_mm_ss}_{speed}__{digest}.mp4";
        return Path.Combine(outputDirectory, filename);
    }

    private static bool IsValidExport(string path)
    {
        try { return File.Exists(path) && new FileInfo(path).Length > 0; }
        catch (IOException) { return false; }
        catch (UnauthorizedAccessException) { return false; }
    }

    private void CleanupStalePartialExports(string directory)
    {
        try
        {
            if (!Directory.Exists(directory)) return;
            var cutoff = DateTime.UtcNow - TimeSpan.FromHours(1);
            foreach (var path in Directory.EnumerateFiles(directory, ".*.part"))
            {
                if (File.GetLastWriteTimeUtc(path) >= cutoff) continue;
                File.Delete(path);
                AppendLog("INFO", $"Removed stale partial export: {path}");
            }
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
        {
            AppendLog("WARNING", $"Could not clean stale partial exports in {directory}: {exception.Message}");
        }
    }

    private void CleanupPartialFilesForOutput(string outputPath)
    {
        try
        {
            var directory = Path.GetDirectoryName(outputPath);
            if (directory is null || !Directory.Exists(directory)) return;
            foreach (var path in Directory.EnumerateFiles(directory, $".{Path.GetFileName(outputPath)}.*.part"))
                File.Delete(path);
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
        {
            AppendLog("WARNING", $"Could not remove partial export for {outputPath}: {exception.Message}");
        }
    }

    private static bool TryReadDateTime(DatePicker picker, System.Windows.Controls.TextBox timeBox, out DateTime value)
    {
        value = default;
        if (picker.SelectedDate is not DateTime date || !DateTime.TryParse(timeBox.Text, CultureInfo.CurrentCulture, DateTimeStyles.NoCurrentDateDefault, out var time)) return false;
        value = date.Date + time.TimeOfDay;
        return value.Year >= 2000;
    }

    private void AppendLog(string level, string message)
    {
        foreach (var line in message.Split(['\r', '\n'], StringSplitOptions.RemoveEmptyEntries))
            Logs.Add($"{DateTime.Now:HH:mm:ss} {level.ToUpperInvariant()} {line}");
        while (Logs.Count > 2000) Logs.RemoveAt(0);
    }

    private void ShowError(string title, string message) { StatusText.Text = title; AppendLog("ERROR", message); MessageBox.Show(this, message, title, MessageBoxButton.OK, MessageBoxImage.Error); }
    private void ShowMessage(string title, string message) => MessageBox.Show(this, message, title, MessageBoxButton.OK, MessageBoxImage.Information);
}
