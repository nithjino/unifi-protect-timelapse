using TimeLapseNative;
using Xunit;

namespace TimeLapseNative.Tests;

public sealed class BackendProtocolTests
{
    [Fact]
    public async Task SessionMultiplexesInterleavedRequestsThroughOneBackend()
    {
        if (string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("TIMELAPSE_BACKEND_PATH"))) return;

        using var first = new BackendProcess();
        using var second = new BackendProcess();
        var firstEvents = new List<BackendEvent>();
        var secondEvents = new List<BackendEvent>();
        var firstRun = first.RunAsync(
            new Dictionary<string, object> { ["id"] = "health-one", ["command"] = "health" },
            firstEvents.Add);
        var secondRun = second.RunAsync(
            new Dictionary<string, object> { ["id"] = "health-two", ["command"] = "health" },
            secondEvents.Add);

        var completions = await Task.WhenAll(firstRun, secondRun);

        Assert.All(completions, completion => Assert.Equal(0, completion.ExitCode));
        Assert.Contains(firstEvents, item => item.Id == "health-one" && item.Event == "complete");
        Assert.Contains(secondEvents, item => item.Id == "health-two" && item.Event == "complete");
        await BackendProcess.ShutdownAsync();
    }

    [Fact]
    public async Task CancelTargetsOneRequestWithoutStoppingItsSibling()
    {
        if (string.IsNullOrWhiteSpace(Environment.GetEnvironmentVariable("TIMELAPSE_BACKEND_PATH"))) return;

        using var cancelled = new BackendProcess();
        using var sibling = new BackendProcess();
        using var source = new CancellationTokenSource();
        source.Cancel();
        var cancelledRun = cancelled.RunAsync(
            new Dictionary<string, object> { ["id"] = "cancelled-health", ["command"] = "health" },
            _ => { },
            source.Token);
        var siblingRun = sibling.RunAsync(
            new Dictionary<string, object> { ["id"] = "sibling-health", ["command"] = "health" },
            _ => { });

        var siblingCompletion = await siblingRun;
        _ = await cancelledRun;

        Assert.Equal(0, siblingCompletion.ExitCode);
        await BackendProcess.ShutdownAsync();
    }
}
