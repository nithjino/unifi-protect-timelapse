from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_windows_project_is_native_wpf() -> None:
    project = (ROOT / "native-windows" / "TimeLapseNative.csproj").read_text(encoding="utf-8")

    assert "<UseWPF>true</UseWPF>" in project
    assert "<UseWindowsForms>true</UseWindowsForms>" in project
    assert "<TargetFramework>net8.0-windows</TargetFramework>" in project
    assert "timelapse.ico" in project


def test_windows_native_gui_includes_full_day_and_daily_controls() -> None:
    main_xaml = (ROOT / "native-windows" / "MainWindow.xaml").read_text(encoding="utf-8")
    main_code = (ROOT / "native-windows" / "MainWindow.xaml.cs").read_text(encoding="utf-8")
    daily_dialog = (ROOT / "native-windows" / "DailyScheduleDialog.xaml").read_text(encoding="utf-8")

    assert "24-hour timelapse" in main_xaml
    assert "Daily automatic timelapses" in main_xaml
    assert 'Header="Downloads"' in main_xaml
    assert 'Header="Daily Automations"' in main_xaml
    assert 'ItemsSource="{Binding DownloadJobs}"' in main_xaml
    assert 'ItemsSource="{Binding DailyAutomationJobs}"' in main_xaml
    assert main_xaml.count('Header="Time Range" Binding="{Binding TimeRangeText}"') == 2
    assert "public string TimeRangeText" in (ROOT / "native-windows" / "Models.cs").read_text(encoding="utf-8")
    assert "RunDailyScheduleIfDue" in main_code
    assert "start:yyyy_MM_dd}_{end:yyyy_MM_dd}_{speed}_{digest}" in main_code
    assert "start:HH_mm_ss}" in main_code
    assert "{speed}__{digest}" in main_code
    assert "ShowingDailyAutomations" in main_code
    assert 'x:Name="ThumbnailPopup"' in main_xaml
    assert 'MouseEnter="ThumbnailHover_MouseEnter"' in main_xaml
    assert '["command"] = "thumbnail"' in main_code
    assert "ThumbnailBase64" in (ROOT / "native-windows" / "Models.cs").read_text(encoding="utf-8")
    assert "_thumbnailRequests.ContainsKey(cacheKey)" in main_code
    assert "_thumbnailFailures.TryGetValue(cacheKey" in main_code
    assert 'TextChanged="TimeText_TextChanged"' in main_xaml
    assert 'RefreshThumbnail("start")' in main_code
    assert 'RefreshThumbnail("end")' in main_code
    assert "Select cameras and a destination" in daily_dialog
    assert "NotifyDownloadFinished" in main_code
    assert "Download Jobs Are Still Running" in main_code
    assert "ActiveJobIds" in main_code
    assert "IsValidExport" in main_code


def test_windows_build_packages_native_app_and_backend() -> None:
    script = (ROOT / "build-windows.ps1").read_text(encoding="utf-8")
    project = (ROOT / "native-windows" / "TimeLapseNative.csproj").read_text(encoding="utf-8")
    backend_process = (ROOT / "native-windows" / "BackendProcess.cs").read_text(encoding="utf-8")

    assert "dotnet publish" in script
    assert "timelapse-backend.exe" in script
    assert "BackendExecutable" in script
    assert "PublishedFiles.Count -ne 1" in script
    assert "timelapse\\gui.py" not in script
    assert "EmbeddedResource" in project
    assert "TimeLapseNative.timelapse-backend.exe" in project
    assert "GetManifestResourceStream" in backend_process
    assert "CancellationGracePeriod" in backend_process
    assert "File.WriteAllText(_cancellationPath" in backend_process
    assert "CleanupStalePartialExports" in (ROOT / "native-windows" / "MainWindow.xaml.cs").read_text(encoding="utf-8")
    assert "PySide6" not in project
