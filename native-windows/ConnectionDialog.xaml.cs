using System.Windows;

namespace TimeLapseNative;

public partial class ConnectionDialog : Window
{
    private readonly Guid _profileId;
    public ConnectionProfile? Result { get; private set; }

    public ConnectionDialog(ConnectionProfile? profile)
    {
        InitializeComponent();
        _profileId = profile?.Id ?? Guid.NewGuid();
        if (profile is null) return;
        ProfileNameText.Text = profile.Name;
        UrlText.Text = profile.Settings.InstanceUrl;
        TokenText.Password = profile.Settings.Token;
        UsernameText.Text = profile.Settings.Username;
        PasswordText.Password = profile.Settings.Password;
        VerifySslCheck.IsChecked = profile.Settings.VerifySsl;
        TimeoutText.Text = profile.Settings.RequestTimeoutSeconds.ToString();
        MaxMiBText.Text = profile.Settings.MaxDownloadMiB.ToString();
    }

    private void Save_Click(object sender, RoutedEventArgs e)
    {
        if (!int.TryParse(TimeoutText.Text, out var timeout) || !int.TryParse(MaxMiBText.Text, out var maxMiB))
        {
            MessageBox.Show(this, "Timeout and maximum download size must be whole numbers.", "Invalid Settings", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }
        var settings = new ConnectionSettings
        {
            InstanceUrl = UrlText.Text,
            Token = TokenText.Password,
            Username = UsernameText.Text,
            Password = PasswordText.Password,
            VerifySsl = VerifySslCheck.IsChecked == true,
            RequestTimeoutSeconds = timeout,
            MaxDownloadMiB = maxMiB,
        }.Normalized();
        var error = settings.ValidationError();
        if (error is not null)
        {
            MessageBox.Show(this, error, "Invalid Settings", MessageBoxButton.OK, MessageBoxImage.Warning);
            return;
        }
        Result = new ConnectionProfile(_profileId, ProfileNameText.Text, settings).Normalized();
        DialogResult = true;
    }
}
