using System.ComponentModel;
using System.Runtime.CompilerServices;
using System.Windows;

namespace TimeLapseNative;

public partial class CameraSelectionDialog : Window
{
    public List<CameraChoice> Choices { get; }
    public HashSet<string> SelectedIds => Choices.Where(choice => choice.Selected).Select(choice => choice.Camera.Id).ToHashSet();

    public CameraSelectionDialog(IEnumerable<CameraInfo> cameras, ISet<string> selectedIds)
    {
        InitializeComponent();
        Choices = cameras.Select(camera => new CameraChoice(camera, selectedIds.Contains(camera.Id))).ToList();
        CameraList.ItemsSource = Choices;
    }

    private void SelectAll_Click(object sender, RoutedEventArgs e) { foreach (var choice in Choices) choice.Selected = true; }
    private void Clear_Click(object sender, RoutedEventArgs e) { foreach (var choice in Choices) choice.Selected = false; }
    private void Ok_Click(object sender, RoutedEventArgs e) => DialogResult = true;
}

public sealed class CameraChoice : INotifyPropertyChanged
{
    private bool _selected;
    public CameraInfo Camera { get; }
    public string Details => string.Join(" • ", new[] { Camera.State, Camera.Model }.Where(value => !string.IsNullOrWhiteSpace(value)));
    public bool Selected { get => _selected; set { _selected = value; PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(Selected))); } }
    public CameraChoice(CameraInfo camera, bool selected) { Camera = camera; _selected = selected; }
    public event PropertyChangedEventHandler? PropertyChanged;
}
