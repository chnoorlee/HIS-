using System.ComponentModel;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Windows;
using HisVoice.Core;
using Microsoft.Web.WebView2.Core;
using Microsoft.Win32;

namespace HisVoice.Desktop;

public partial class MainWindow : Window
{
    private DesktopSettings? settings;
    private EncryptedSpool? spool;
    private CaptureCoordinator? capture;
    private bool closing;
    private readonly SemaphoreSlim bridge = new(1, 1);
    public MainWindow()
    {
        InitializeComponent(); Loaded += InitializeAsync; Closing += OnClosing;
    }

    private async void InitializeAsync(object sender, RoutedEventArgs e)
    {
        try
        {
            settings = DesktopSettings.Load();
            string storage = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "HisVoice");
            Directory.CreateDirectory(storage);
            spool = new EncryptedSpool(Path.Combine(storage, "Spool"));
            string deviceId = Convert.ToHexStringLower(SHA256.HashData(Encoding.UTF8.GetBytes(Environment.MachineName + "/" + Environment.UserName)));
            capture = new CaptureCoordinator(settings, spool, deviceId, Publish, action => Dispatcher.BeginInvoke(action));
            var environment = await CoreWebView2Environment.CreateAsync(null, Path.Combine(storage, "WebView"));
            await Browser.EnsureCoreWebView2Async(environment);
            Browser.CoreWebView2.Settings.AreDevToolsEnabled = settings.Development;
            Browser.CoreWebView2.Settings.AreDefaultContextMenusEnabled = settings.Development;
            Browser.CoreWebView2.Settings.AreBrowserAcceleratorKeysEnabled = settings.Development;
            Browser.CoreWebView2.Settings.IsPasswordAutosaveEnabled = false;
            Browser.CoreWebView2.Settings.IsGeneralAutofillEnabled = false;
            Browser.CoreWebView2.NewWindowRequested += (_, args) => args.Handled = true;
            Browser.CoreWebView2.DownloadStarting += (_, args) => args.Cancel = true;
            Browser.CoreWebView2.NavigationStarting += (_, args) =>
            {
                if (!settings.IsAllowedPage(args.Uri)) args.Cancel = true;
            };
            Browser.CoreWebView2.PermissionRequested += (_, args) =>
                args.State = args.PermissionKind == CoreWebView2PermissionKind.Microphone && settings.IsAllowedPage(args.Uri)
                    ? CoreWebView2PermissionState.Default : CoreWebView2PermissionState.Deny;
            Browser.CoreWebView2.WebMessageReceived += HandleMessageAsync;
            SystemEvents.SessionSwitch += OnSessionSwitch;
            SystemEvents.PowerModeChanged += OnPowerModeChanged;
            Browser.CoreWebView2.Navigate(settings.UiOrigin);
            NativeStatus.Text = settings.Development ? "Native capture | Development environment | Separate input-device clocks" : "Native capture | Hospital environment | Separate input-device clocks";
        }
        catch (Exception ex)
        {
            NativeStatus.Text = "Native startup failed";
            MessageBox.Show(this, ex.Message, "HIS Voice startup", MessageBoxButton.OK, MessageBoxImage.Error);
        }
    }

    private async void HandleMessageAsync(object? sender, CoreWebView2WebMessageReceivedEventArgs args)
    {
        if (settings is null || capture is null || !settings.IsAllowedPage(args.Source) || args.WebMessageAsJson.Length > 32768) return;
        string? requestId = null;
        await bridge.WaitAsync();
        try
        {
            using var message = JsonDocument.Parse(args.WebMessageAsJson);
            string? type = message.RootElement.GetProperty("type").GetString();
            requestId = message.RootElement.GetProperty("request_id").GetString();
            if (requestId is null || requestId.Length > 128) throw new ArgumentException("Invalid bridge request ID.");
            object result = type switch
            {
                "devices.list" => AudioDeviceCapture.ListDevices(),
                "capture.start" => await capture.StartAsync(message.RootElement.GetProperty("payload").Deserialize<StartRequest>(Wire.Json)
                    ?? throw new ArgumentException("Missing capture request.")),
                "capture.recover" => await capture.RecoverAsync(message.RootElement.GetProperty("payload").Deserialize<StartRequest>(Wire.Json)
                    ?? throw new ArgumentException("Missing recovery request.")),
                "capture.pause" => await capture.PauseAsync(),
                "capture.resume" => await capture.ResumeAsync(),
                "capture.stop" => await capture.StopAsync(),
                "capture.status" => capture.Status(),
                "session.clear" => await capture.ClearAsync(),
                _ => throw new ArgumentException("Unknown native command.")
            };
            Post(new { type = "bridge.result", request_id = requestId, payload = result });
        }
        catch (Exception ex)
        {
            Post(new { type = "bridge.error", request_id = requestId, payload = new { code = "native_command_failed", message = ex.Message } });
        }
        finally { bridge.Release(); }
    }
    private void Publish(string type, object payload)
    {
        Dispatcher.BeginInvoke(() =>
        {
            Post(new { type, payload });
            if (type == "session.revoked" && Browser.CoreWebView2 is not null)
                _ = Browser.CoreWebView2.ExecuteScriptAsync("sessionStorage.clear(); document.body.replaceChildren(); location.replace('/');");
        });
    }
    private void Post(object message)
    {
        if (Browser.CoreWebView2 is not null && settings?.IsAllowedPage(Browser.Source?.AbsoluteUri ?? "") == true)
            Browser.CoreWebView2.PostWebMessageAsJson(JsonSerializer.Serialize(message, Wire.Json));
    }
    private void OnSessionSwitch(object sender, SessionSwitchEventArgs e)
    {
        if (e.Reason is SessionSwitchReason.SessionLock or SessionSwitchReason.SessionLogoff)
            Dispatcher.BeginInvoke(async () => { if (capture is not null) await capture.PauseAsync("workstation_locked"); });
    }
    private void OnPowerModeChanged(object sender, PowerModeChangedEventArgs e)
    {
        if (e.Mode == PowerModes.Suspend)
            Dispatcher.BeginInvoke(async () => { if (capture is not null) await capture.PauseAsync("system_suspending"); });
    }
    private async void OnClosing(object? sender, CancelEventArgs e)
    {
        if (closing) return;
        e.Cancel = true; closing = true;
        SystemEvents.SessionSwitch -= OnSessionSwitch; SystemEvents.PowerModeChanged -= OnPowerModeChanged;
        try
        {
            if (capture is not null) await capture.DisposeAsync();
            spool?.Dispose(); Browser.Dispose(); bridge.Dispose();
        }
        finally { Close(); }
    }
}
