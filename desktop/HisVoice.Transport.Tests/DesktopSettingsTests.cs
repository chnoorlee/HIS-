using HisVoice.Desktop;
using Xunit;

namespace HisVoice.Transport.Tests;

public sealed class DesktopSettingsTests
{
    private readonly DesktopSettings settings = new() { UiOrigin = "https://voice.hospital.example", ApiBase = "https://voice.hospital.example/api/v1" };

    [Theory]
    [InlineData("https://voice.hospital.example.evil.invalid")]
    [InlineData("http://voice.hospital.example")]
    [InlineData("https://voice.hospital.example:8443")]
    [InlineData("https://voice.hospital.example@evil.invalid")]
    [InlineData("file:///C:/index.html")]
    public void NativeBridgeRejectsForeignOrigins(string source) => Assert.False(settings.IsAllowedPage(source));

    [Fact]
    public void NativeApiCannotBeRedirectedByBridgePayload()
    {
        settings.ValidateApi("https://voice.hospital.example/api/v1/");
        Assert.Throws<InvalidOperationException>(() => settings.ValidateApi("https://evil.invalid/api/v1"));
        Assert.Throws<InvalidOperationException>(() => settings.ValidateApi("https://voice.hospital.example/other-api"));
        Assert.True(settings.IsAllowedPage("https://voice.hospital.example/patients/selected"));
    }
}
