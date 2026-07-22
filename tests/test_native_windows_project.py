from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).parent.parent
XAML_NAMESPACE = "{http://schemas.microsoft.com/winfx/2006/xaml/presentation}"
XAML_NAME = "{http://schemas.microsoft.com/winfx/2006/xaml}Name"


def _named_xaml_elements(path: Path) -> dict[str, ET.Element]:
    return {
        name: element
        for element in ET.parse(path).iter()  # noqa: S314 - repository-owned XAML
        if (name := element.get(XAML_NAME)) is not None
    }


def test_windows_project_is_native_wpf() -> None:
    project = ET.parse(ROOT / "native-windows" / "TimeLapseNative.csproj")  # noqa: S314 - repository-owned XML
    properties = {element.tag: element.text for element in project.findall("./PropertyGroup/*")}
    expected = {
        "TargetFramework": "net8.0-windows",
        "UseWPF": "true",
        "UseWindowsForms": "true",
        "EnableWindowsTargeting": "true",
        "ApplicationIcon": r"..\assets\icons\timelapse.ico",
    }

    assert {name: properties.get(name) for name in expected} == expected


def test_windows_native_gui_declares_core_controls_and_bindings() -> None:
    elements = _named_xaml_elements(ROOT / "native-windows" / "MainWindow.xaml")

    assert {
        "FullDayCheckBox",
        "DailyAutomaticCheckBox",
        "DownloadsGrid",
        "DailyAutomationsGrid",
        "ThumbnailPopup",
    } <= elements.keys()
    assert elements["FullDayCheckBox"].get("Checked") == "FullDay_Checked"
    assert elements["DailyAutomaticCheckBox"].get("Checked") == "DailyAutomatic_Checked"
    assert elements["DownloadsGrid"].get("ItemsSource") == "{Binding DownloadJobs}"
    assert elements["DailyAutomationsGrid"].get("ItemsSource") == "{Binding DailyAutomationJobs}"

    time_range_columns = [
        element
        for element in ET.parse(ROOT / "native-windows" / "MainWindow.xaml").iter()  # noqa: S314
        if element.tag == f"{XAML_NAMESPACE}DataGridTextColumn" and element.get("Header") == "Time Range"
    ]
    assert [column.get("Binding") for column in time_range_columns] == [
        "{Binding TimeRangeText}",
        "{Binding TimeRangeText}",
    ]


def test_windows_build_packages_native_app_and_backend() -> None:
    script = (ROOT / "build-windows.ps1").read_text(encoding="utf-8")
    project = ET.parse(ROOT / "native-windows" / "TimeLapseNative.csproj")  # noqa: S314 - repository-owned XML
    backend_process = (ROOT / "native-windows" / "BackendProcess.cs").read_text(encoding="utf-8")

    assert all(
        requirement in script
        for requirement in (
            '"--onefile"',
            '"--exclude-module", "PySide6"',
            '"id":"build-health","command":"health"',
            '$_.id -eq "build-health" -and $_.event -eq "complete"',
            "dotnet publish",
            "-p:PublishSingleFile=true",
            '"-p:BackendExecutable=$BuiltBackend"',
            "PublishedFiles.Count -ne 1",
        )
    )
    resource = project.find("./ItemGroup/EmbeddedResource")
    assert resource is not None
    assert resource.get("Include") == "$(BackendExecutable)"
    assert resource.get("LogicalName") == "TimeLapseNative.timelapse-backend.exe"
    assert 'EmbeddedBackendName = "TimeLapseNative.timelapse-backend.exe"' in backend_process
