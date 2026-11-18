using Himari.Workbench;

namespace Himari.Workbench.Tests;

public sealed class SettingsTests : IDisposable
{
    private readonly TempFolder temp = new();
    public void Dispose() => temp.Dispose();

    [Fact]
    public void MissingFileAsksForTheWizard()
    {
        var store = new SettingsStore(temp.Path);
        Assert.Equal(SettingsLoadKind.Missing, store.Load().Kind);
        Assert.False(File.Exists(store.PathName));
    }

    [Fact]
    public void BrokenFileIsMovedAsideNotOverwritten()
    {
        var store = new SettingsStore(temp.Path);
        File.WriteAllText(store.PathName, "{ not json");
        var load = store.Load();
        Assert.Equal(SettingsLoadKind.Broken, load.Kind);
        Assert.False(File.Exists(store.PathName));
        Assert.NotNull(load.MovedTo);
        Assert.StartsWith(store.PathName + ".broken-", load.MovedTo);
        Assert.Equal("{ not json", File.ReadAllText(load.MovedTo!));
    }

    [Fact]
    public void ANullHubArgumentMakesTheFileBroken()
    {
        var store = new SettingsStore(temp.Path);
        string Settings(string arguments) =>
            $"{{\"version\": 1, \"server\": \"https://himari.example\", \"hub\": {{\"executable\": \"C:/tools/uv.exe\", \"arguments\": {arguments}, \"working_directory\": \"C:/hub\"}}}}";
        File.WriteAllText(store.PathName, Settings("[\"run\"]"));
        Assert.Equal(SettingsLoadKind.Ok, store.Load().Kind);
        File.WriteAllText(store.PathName, Settings("[\"run\", null]"));
        Assert.Equal(SettingsLoadKind.Broken, store.Load().Kind);
    }

    [Fact]
    public void ABrokenFileNeverMovesOverAnEarlierOne()
    {
        var store = new SettingsStore(temp.Path);
        File.WriteAllText(store.PathName, "[]");
        string first = store.Load().MovedTo!;
        File.WriteAllText(store.PathName, "null");
        string second = store.Load().MovedTo!;
        Assert.NotEqual(first, second);
        Assert.True(File.Exists(first) && File.Exists(second));
    }

    [Fact]
    public void WritesAtomicallyAndReadsBack()
    {
        string folder = temp.Sub("설정 폴더");
        var store = new SettingsStore(folder);
        store.Load();
        var hub = new HubCommand(@"C:\Program Files\uv\uv.exe", ["run", "--project", @"D:\00_프로젝트\Himari v4", "himari-hub"], @"D:\00_프로젝트\Himari v4");
        store.Update(s => s with { Server = "https://himari.example", Hub = hub, HubHome = @"C:\Users\사용자 이름\.himari-hub", OwnedInstance = "7.1.ab000000000000000000000000000000" });
        store.Update(s => s with { Linked = false });
        Assert.Empty(Directory.GetFiles(folder, "*.tmp"));
        var reloaded = new SettingsStore(folder).Load();
        Assert.Equal(SettingsLoadKind.Ok, reloaded.Kind);
        var value = reloaded.Settings!;
        Assert.Equal("https://himari.example", value.Server);
        Assert.True(hub.SameAs(value.Hub));
        Assert.Equal(@"C:\Users\사용자 이름\.himari-hub", value.HubHome);
        Assert.Equal("7.1.ab000000000000000000000000000000", value.OwnedInstance);
        Assert.False(value.Linked);
        string text = File.ReadAllText(Path.Combine(folder, SettingsStore.FileName));
        Assert.Contains("\"owned_instance\"", text);
        Assert.Contains("\"working_directory\"", text);
        Assert.Contains("사용자 이름", text);
        Assert.DoesNotContain("effective_hub_home", text);
        Assert.DoesNotContain("is_complete", text);
    }

    [Fact]
    public void UpdatesFromManyThreadsAllLand()
    {
        var store = new SettingsStore(temp.Path);
        store.Load();
        Parallel.For(0, 40, i => store.Update(s => s with { Server = $"https://x{i}.example", Version = s.Version + 1 }));
        Assert.Equal(41, store.Current.Version);
        Assert.Equal(41, new SettingsStore(temp.Path).Load().Settings!.Version);
    }

    [Fact]
    public void TryUpdateCanDecline()
    {
        var store = new SettingsStore(temp.Path);
        store.Load();
        Assert.False(store.TryUpdate(_ => null));
        Assert.False(File.Exists(store.PathName));
    }

    [Fact]
    public void MigratedOnlyWhenTheSettingsFileIsGone()
    {
        var store = new SettingsStore(temp.Path);
        File.WriteAllText(store.PathName + SettingsStore.MigratedSuffix, "{}");
        Assert.True(store.IsMigrated);
        Assert.Equal(SettingsLoadKind.Migrated, store.Load().Kind);
        // §8: while both exist, workbench.json is the source (the merged app redoes the move).
        File.WriteAllText(store.PathName, "{}");
        Assert.False(store.IsMigrated);
        Assert.Equal(SettingsLoadKind.Ok, store.Load().Kind);
    }

    [Fact]
    public void DefaultsAreLinkedAndNotDeveloperMode()
    {
        var value = new WorkbenchSettings();
        Assert.True(value.Linked);
        Assert.False(value.DeveloperMode);
        Assert.False(value.IsComplete);
    }
}
