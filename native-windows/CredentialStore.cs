using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;

namespace TimeLapseNative;

public sealed record ProfileState(List<ConnectionProfile> Profiles, Guid? SelectedProfileId);

public static class ProfileStore
{
    private const string CredentialPrefix = "TimeLapse/ConnectionProfile/";
    private static readonly string StateDirectory = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "TimeLapse");
    private static readonly string StatePath = Path.Combine(StateDirectory, "profiles.json");

    public static ProfileState Load()
    {
        if (!File.Exists(StatePath)) return new([], null);
        var index = JsonSerializer.Deserialize<ProfileIndex>(File.ReadAllText(StatePath)) ?? new([], null);
        var profiles = new List<ConnectionProfile>();
        foreach (var id in index.ProfileIds)
        {
            var json = CredentialManager.Read(CredentialPrefix + id);
            if (json is null) continue;
            var profile = JsonSerializer.Deserialize<ConnectionProfile>(json);
            if (profile is not null && profile.Id == id) profiles.Add(profile.Normalized());
        }
        var selected = profiles.Any(profile => profile.Id == index.SelectedProfileId)
            ? index.SelectedProfileId : profiles.FirstOrDefault()?.Id;
        return new(profiles, selected);
    }

    public static void Save(ProfileState state)
    {
        Directory.CreateDirectory(StateDirectory);
        var existing = File.Exists(StatePath)
            ? JsonSerializer.Deserialize<ProfileIndex>(File.ReadAllText(StatePath))?.ProfileIds ?? []
            : [];
        var normalized = state.Profiles.Select(profile => profile.Normalized()).ToList();
        foreach (var profile in normalized)
            CredentialManager.Write(CredentialPrefix + profile.Id, JsonSerializer.Serialize(profile));
        foreach (var removed in existing.Except(normalized.Select(profile => profile.Id)))
            CredentialManager.Delete(CredentialPrefix + removed);
        var index = new ProfileIndex(normalized.Select(profile => profile.Id).ToList(), state.SelectedProfileId);
        var temporary = StatePath + ".tmp";
        File.WriteAllText(temporary, JsonSerializer.Serialize(index));
        File.Move(temporary, StatePath, overwrite: true);
    }

    private sealed record ProfileIndex(List<Guid> ProfileIds, Guid? SelectedProfileId);
}

internal static class CredentialManager
{
    private const uint GenericCredential = 1;
    private const uint LocalMachinePersistence = 2;
    private const int NotFoundError = 1168;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct NativeCredential
    {
        public uint Flags;
        public uint Type;
        public string TargetName;
        public string? Comment;
        public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
        public uint CredentialBlobSize;
        public IntPtr CredentialBlob;
        public uint Persist;
        public uint AttributeCount;
        public IntPtr Attributes;
        public string? TargetAlias;
        public string UserName;
    }

    [DllImport("advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredWrite(ref NativeCredential credential, uint flags);

    [DllImport("advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredRead(string target, uint type, uint flags, out IntPtr credential);

    [DllImport("advapi32.dll", EntryPoint = "CredDeleteW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool CredDelete(string target, uint type, uint flags);

    [DllImport("advapi32.dll")]
    private static extern void CredFree(IntPtr buffer);

    public static void Write(string target, string value)
    {
        var bytes = Encoding.Unicode.GetBytes(value);
        var blob = Marshal.AllocCoTaskMem(bytes.Length);
        try
        {
            Marshal.Copy(bytes, 0, blob, bytes.Length);
            var credential = new NativeCredential
            {
                Type = GenericCredential,
                TargetName = target,
                CredentialBlobSize = (uint)bytes.Length,
                CredentialBlob = blob,
                Persist = LocalMachinePersistence,
                UserName = "TimeLapse",
            };
            if (!CredWrite(ref credential, 0)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
        }
        finally { Marshal.FreeCoTaskMem(blob); }
    }

    public static string? Read(string target)
    {
        if (!CredRead(target, GenericCredential, 0, out var pointer))
        {
            var error = Marshal.GetLastWin32Error();
            if (error == NotFoundError) return null;
            throw new System.ComponentModel.Win32Exception(error);
        }
        try
        {
            var credential = Marshal.PtrToStructure<NativeCredential>(pointer);
            if (credential.CredentialBlob == IntPtr.Zero) return "";
            return Marshal.PtrToStringUni(credential.CredentialBlob, (int)credential.CredentialBlobSize / 2);
        }
        finally { CredFree(pointer); }
    }

    public static void Delete(string target)
    {
        if (!CredDelete(target, GenericCredential, 0) && Marshal.GetLastWin32Error() != NotFoundError)
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    }
}
