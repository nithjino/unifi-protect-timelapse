using Microsoft.Win32;
using System.Windows;

namespace TimeLapseNative;

public partial class DailyScheduleDialog : Window
{
    public List<CameraChoice> Choices { get; }
    public List<CameraInfo> SelectedCameras => Choices.Where(choice => choice.Selected).Select(choice => choice.Camera).ToList();
    public string OutputDirectory { get; private set; }

    public DailyScheduleDialog(IEnumerable<CameraInfo> cameras, string initialDirectory)
    {
        InitializeComponent();
        Choices = cameras.Select(camera => new CameraChoice(camera, selected: false)).ToList();
        CameraList.ItemsSource = Choices;
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
