using System.IO;
using System.Text.Json;
using HisVoice.Core;

namespace HisVoice.Desktop;

public sealed class DesktopSettings
{
    public bool Development { get; set; }
    public string UiOrigin { get; set; } = "";
    public string ApiBase { get; set; } = "";
    public static DesktopSettings Load()
    {
        var settings = JsonSerializer.Deserialize<DesktopSettings>(File.ReadAllText(
            Path.Combine(AppContext.BaseDirectory, "desktop.settings.json")), Wire.Json)
            ?? throw new InvalidDataException("Missing desktop settings.");
        ValidateUri(settings.UiOrigin, settings.Development);
        ValidateUri(settings.ApiBase, settings.Development);
        if (new Uri(settings.UiOrigin).AbsolutePath != "/") throw new InvalidDataException("UI origin must not include a path.");
        return settings;
    }
    private static void ValidateUri(string input, bool development)
    {
        if (!Uri.TryCreate(input, UriKind.Absolute, out var uri) || !string.IsNullOrEmpty(uri.UserInfo) ||
            !string.IsNullOrEmpty(uri.Query) || !string.IsNullOrEmpty(uri.Fragment) ||
            (uri.Scheme != "https" && !(development && uri.Scheme == "http" && uri.IsLoopback)))
            throw new InvalidDataException("Use HTTPS hospital endpoints or explicit loopback development endpoints.");
    }
    public bool IsAllowedPage(string source) => Uri.TryCreate(source, UriKind.Absolute, out var uri) &&
        uri.GetLeftPart(UriPartial.Authority) == new Uri(UiOrigin).GetLeftPart(UriPartial.Authority);
    public void ValidateApi(string api)
    {
        if (!Uri.TryCreate(api, UriKind.Absolute, out var uri) ||
            uri.AbsoluteUri.TrimEnd('/') != new Uri(ApiBase).AbsoluteUri.TrimEnd('/'))
            throw new InvalidOperationException("The native API endpoint is fixed by hospital configuration.");
    }
}
