using System.Collections.ObjectModel;
using System.Collections.Specialized;
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
        LogTextBox.Text = string.Join(Environment.NewLine, logs);
        LogTextBox.CaretIndex = LogTextBox.Text.Length;
        LogTextBox.ScrollToEnd();
        logs.CollectionChanged += Logs_CollectionChanged;
        Closed += (_, _) => logs.CollectionChanged -= Logs_CollectionChanged;
    }

    private void Logs_CollectionChanged(object? sender, NotifyCollectionChangedEventArgs eventArgs)
    {
        var selectionStart = LogTextBox.SelectionStart;
        var selectionLength = LogTextBox.SelectionLength;
        var wasAtEnd = selectionLength == 0 && LogTextBox.CaretIndex >= LogTextBox.Text.Length;

        if (eventArgs.Action == NotifyCollectionChangedAction.Add && eventArgs.NewItems is not null)
        {
            var addedLines = eventArgs.NewItems.Cast<string>();
            var prefix = LogTextBox.Text.Length == 0 ? "" : Environment.NewLine;
            LogTextBox.AppendText(prefix + string.Join(Environment.NewLine, addedLines));
        }
        else if (eventArgs.Action == NotifyCollectionChangedAction.Remove
                 && eventArgs.OldStartingIndex == 0
                 && eventArgs.OldItems is not null)
        {
            var removedText = string.Join(Environment.NewLine, eventArgs.OldItems.Cast<string>());
            var removedLength = removedText.Length + (Logs.Count > 0 ? Environment.NewLine.Length : 0);
            LogTextBox.Text = LogTextBox.Text[ Math.Min(removedLength, LogTextBox.Text.Length)..];
            selectionStart = Math.Max(0, selectionStart - removedLength);
        }
        else
        {
            LogTextBox.Text = string.Join(Environment.NewLine, Logs);
        }

        if (wasAtEnd)
        {
            LogTextBox.CaretIndex = LogTextBox.Text.Length;
            LogTextBox.ScrollToEnd();
            return;
        }

        var safeStart = Math.Min(selectionStart, LogTextBox.Text.Length);
        var safeLength = Math.Min(selectionLength, LogTextBox.Text.Length - safeStart);
        LogTextBox.Select(safeStart, safeLength);
    }

    private void Clear_Click(object sender, RoutedEventArgs e) => Logs.Clear();
}
