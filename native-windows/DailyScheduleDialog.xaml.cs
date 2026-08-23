using Microsoft.Win32;
using System.Windows;

namespace TimeLapseNative;

public partial class DailyScheduleDialog : Window
{
    public List<CameraChoice> Choices { get; }
    public List<CameraInfo> SelectedCameras => Choices.Where(choice => choice.Selected).Select(choice => choice.Camera).ToList();
    public string AutomationName => NameText.Text.Trim();
    public string OutputDirectory { get; private set; }

    public DailyScheduleDialog(
        IEnumerable<CameraInfo> cameras,
        string initialDirectory,
        string initialName = "",
        IReadOnlySet<string>? selectedCameraIds = null)
    {
        InitializeComponent();
        Choices = cameras.Select(camera => new CameraChoice(camera, selectedCameraIds?.Contains(camera.Id) == true)).ToList();
        CameraList.ItemsSource = Choices;
        NameText.Text = initialName;
        OutputDirectory = initialDirectory;
        UpdateOutputDisplay();
    }

    private void SelectAll_Click(object sender, RoutedEventArgs e)
    {
        foreach (var choice in Choices) choice.Selected = true;
    }

    private void Choose_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new OpenFolderDialog { Title = "Choose Daily Timelapse Folder", InitialDirectory = OutputDirectory };
        if (dialog.ShowDialog(this) != true) return;
        OutputDirectory = dialog.FolderName;
        UpdateOutputDisplay();
    }

    private void Ok_Click(object sender, RoutedEventArgs e)
    {
        if (string.IsNullOrWhiteSpace(AutomationName))
        {
            MessageBox.Show(this, "Enter a unique name for this Daily Automation.", "Name Required", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        if (SelectedCameras.Count == 0)
        {
            MessageBox.Show(this, "Select at least one camera for the daily job.", "No Cameras Selected", MessageBoxButton.OK, MessageBoxImage.Information);
            return;
        }
        if (File.Exists(OutputDirectory))
        {
            MessageBox.Show(this, "The selected output location is not a folder.", "Invalid Output Folder", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }
        DialogResult = true;
    }

    private void UpdateOutputDisplay()
    {
        OutputText.Text = OutputDirectory;
        OutputText.ToolTip = OutputDirectory;
    }
}
