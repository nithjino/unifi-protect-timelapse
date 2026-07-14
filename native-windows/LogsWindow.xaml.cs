using System.Collections.ObjectModel;
using System.Windows;

namespace TimeLapseNative;

public partial class LogsWindow : Window
{
    public ObservableCollection<string> Logs { get; }
    public LogsWindow(ObservableCollection<string> logs)
    {
        Logs = logs;
        DataContext = this;
        InitializeComponent();
        logs.CollectionChanged += (_, _) => { if (LogList.Items.Count > 0) LogList.ScrollIntoView(LogList.Items[^1]); };
    }
    private void Clear_Click(object sender, RoutedEventArgs e) => Logs.Clear();
}
