// Lectern's tray app for Windows. It runs Lectern as its child process
// (<install folder>\app\venv\Scripts\lectern.exe serve), shows whether it's up, opens it in the
// browser, keeps its output in %LOCALAPPDATA%\Lectern\Logs\lectern.log (another install than
// %USERPROFILE%\.lectern: Logs\<its key>\lectern.log, Program.LogPath), and on Quit stops it,
// its models with it, and leaves.
// One file, WinForms only, compiled at install time by the csc.exe that ships with Windows
// (.NET Framework 4: C# 5, so no $"" strings and no ?.): packaging\windows\build_tray.ps1.
//
// Stopping is setting a named event, "-stop" (serve --stop-event): Windows has no Ctrl+C to
// send a process with no console. Lectern then stops its model runners itself. (Not closing its
// standard input, as before: a thread of Lectern's waiting to read it made the first model load
// wait for good, src/lectern/core/winevent.py. Its standard input is left unset.)
// Every process Lectern starts is in a job object that ends with the app, so neither a
// Lectern that won't stop nor the app being killed leaves a model in memory.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Text;
using System.IO;
using System.Net;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        Look.BeDpiAware();
        string dir = InstallFolder();
        bool background = Array.IndexOf(args, "--background") >= 0;
        // One app per install folder. Opened again (Start menu, a second click), it asks the one
        // already running to open Lectern in the browser, and leaves. The installer and the
        // uninstaller ask it to quit through "-quit" (install.ps1 names them the same way).
        string name = "Lectern-" + Key(dir);
        bool first;
        using (var one = new Mutex(true, "Local\\" + name, out first))
        using (var reopen = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\" + name + "-open"))
        using (var quit = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\" + name + "-quit"))
        using (var stop = new EventWaitHandle(false, EventResetMode.ManualReset, "Local\\" + name + "-stop"))
        {
            if (!first)
            {
                if (!background) reopen.Set();
                return 0;
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            using (var app = new TrayApp(dir, !background, reopen, quit, stop, "Local\\" + name + "-stop"))
            {
                Application.Run();
            }
            GC.KeepAlive(one);
        }
        return 0;
    }

    // This program is <install folder>\app\tray\Lectern.exe.
    private static string InstallFolder()
    {
        string here = Path.GetDirectoryName(Application.ExecutablePath);
        return Path.GetFullPath(Path.Combine(here, "..", ".."));
    }

    // The install folder as a name: its mutex and events, and the log folder of an install
    // other than the default one (install_paths.ps1 names them the same way).
    internal static string Key(string dir)
    {
        // Without a trailing \, as the scripts have it: a drive's root (D:\) has one here.
        return dir.TrimEnd('\\').ToLowerInvariant().Replace('\\', '_').Replace(':', '_');
    }

    // The default install (%USERPROFILE%\.lectern) logs to Lectern\Logs\lectern.log, another
    // one to Lectern\Logs\<key>\lectern.log: two running at once don't write one file.
    internal static string LogPath(string dir)
    {
        string logs = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Lectern", "Logs");
        string home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        string standard = Path.GetFullPath(Path.Combine(home, ".lectern")).TrimEnd('\\');
        if (string.Equals(dir.TrimEnd('\\'), standard, StringComparison.OrdinalIgnoreCase))
            return Path.Combine(logs, "lectern.log");
        return Path.Combine(logs, Key(dir), "lectern.log");
    }
}

internal enum State { Stopped, Starting, Running, Stopping, Failed }

internal sealed class TrayApp : IDisposable
{
    private const int InterruptPatienceMs = 30000;  // as the Mac app and the Linux unit give it
    // While it runs, /health is asked every so often: a Lectern that stops answering though its
    // process runs (Windows' event loop could stop accepting: src/lectern/core/winloop.py) is
    // said so in the menu, not restarted: a busy one would lose what it was doing.
    private const int WatchIntervalMs = 30000;
    private const int WatchTimeoutMs = 5000;
    private const int WatchMisses = 3;
    private const long LogLimit = 5000000;  // bytes; past it the file becomes lectern.log.1
    private const string TokenMark = "#token=";

    private readonly string dir;
    private readonly string version;
    private readonly string logPath;
    private readonly NotifyIcon icon = new NotifyIcon();
    private readonly ContextMenuStrip menu = new ContextMenuStrip();
    private readonly ToolStripMenuItem statusLine = new ToolStripMenuItem();
    private readonly ToolStripMenuItem openItem = new ToolStripMenuItem("Open Lectern");
    private readonly ToolStripMenuItem logItem = new ToolStripMenuItem("Show Log");
    private readonly ToolStripMenuItem startStopItem = new ToolStripMenuItem();
    private readonly ToolStripMenuItem restartItem = new ToolStripMenuItem("Restart");
    private readonly ToolStripMenuItem quitItem = new ToolStripMenuItem("Quit Lectern");
    private readonly MenuLook look;
    private readonly TrayGlyph glyph = new TrayGlyph();
    private readonly UserPreferenceChangedEventHandler themeChanged;
    private readonly System.Windows.Forms.Timer healthTimer = new System.Windows.Forms.Timer();
    private readonly System.Windows.Forms.Timer stopTimer = new System.Windows.Forms.Timer();
    private readonly System.Windows.Forms.Timer watchTimer = new System.Windows.Forms.Timer();
    private readonly EventWaitHandle stopEvent;  // set: Lectern stops (serve --stop-event)
    private readonly string stopEventName;
    private readonly SynchronizationContext ui;
    private readonly JobObject job = new JobObject();
    private readonly object logLock = new object();

    private Process child;
    private StreamWriter log;
    private State state = State.Stopped;
    private string detail = "";
    private bool openWhenReady;
    private Action afterStop;
    private bool quitting;
    private string link;  // the access link, asked for once each time Lectern starts
    private int runs;  // which start a link that comes back belongs to
    private int misses;  // /health asks in a row that got no answer, while running
    private bool answering = true;
    private bool asking;
    private bool openWhenAnswered;

    // NotifyIcon's own way of showing its menu, which a right click uses: it brings the app
    // forward first, so that a click anywhere else closes the menu. It isn't public.
    private static readonly MethodInfo ShowContextMenu = typeof(NotifyIcon).GetMethod(
        "ShowContextMenu", BindingFlags.Instance | BindingFlags.NonPublic);

    public TrayApp(string dir, bool openWhenReady, EventWaitHandle reopen, EventWaitHandle quit,
        EventWaitHandle stopEvent, string stopEventName)
    {
        this.dir = dir;
        this.openWhenReady = openWhenReady;
        this.stopEvent = stopEvent;
        this.stopEventName = stopEventName;
        ui = new WindowsFormsSynchronizationContext();
        SynchronizationContext.SetSynchronizationContext(ui);
        string here = Path.GetDirectoryName(Application.ExecutablePath);
        string versionFile = Path.Combine(here, "Lectern.version");
        version = File.Exists(versionFile) ? File.ReadAllText(versionFile).Trim() : "";
        logPath = Program.LogPath(dir);

        statusLine.Enabled = false;
        openItem.Click += delegate { OpenLectern(); };
        logItem.Click += delegate { ShowLog(); };
        startStopItem.Click += delegate { if (child == null) Start(); else Stop(null); };
        restartItem.Click += delegate { Stop(Start); };
        quitItem.Click += delegate { Quit(); };
        menu.Items.AddRange(new ToolStripItem[] {
            statusLine, new ToolStripSeparator(), openItem, logItem, new ToolStripSeparator(),
            startStopItem, restartItem, new ToolStripSeparator(), quitItem,
        });
        menu.Opening += delegate { Refresh(); };
        look = MenuLook.Apply(menu);
        icon.ContextMenuStrip = menu;
        ShowLook();
        // A left click shows the menu too, as Windows' own tray icons do.
        icon.MouseClick += delegate(object sender, MouseEventArgs e)
        {
            if (e.Button == MouseButtons.Left) ShowMenu();
        };
        // The taskbar switched between light and dark: the icon and the menu follow at once.
        themeChanged = delegate { ui.Post(delegate { ShowLook(); }, null); };
        SystemEvents.UserPreferenceChanged += themeChanged;
        icon.Visible = true;

        healthTimer.Interval = 1000;
        healthTimer.Tick += delegate { AskHealth(); };
        stopTimer.Tick += delegate { StopTookTooLong(); };
        watchTimer.Interval = WatchIntervalMs;
        watchTimer.Tick += delegate { Watch(); };

        // Signing out, or Windows shutting down: Lectern is stopped first (the job object
        // would end it anyway; this lets it stop its models itself).
        SystemEvents.SessionEnding += delegate { AskToStop(); };
        // Opened again: Lectern in the browser, now or once it answers.
        ThreadPool.RegisterWaitForSingleObject(reopen, delegate
        {
            ui.Post(delegate { Reopened(); }, null);
        }, null, Timeout.Infinite, false);
        // Asked to quit (an update, an uninstall): as Quit in the menu, Lectern stopped first.
        ThreadPool.RegisterWaitForSingleObject(quit, delegate
        {
            ui.Post(delegate { Quit(); }, null);
        }, null, Timeout.Infinite, true);

        Refresh();
        Start();
    }

    // --- Lectern, the child process ------------------------------------------------------

    private void Start()
    {
        if (child != null) return;
        OpenLog();
        string lectern = Path.Combine(dir, "app", "venv", "Scripts", "lectern.exe");
        var info = new ProcessStartInfo(lectern,
            "serve --no-browser --port " + Port().ToString() + " --stop-event \"" + stopEventName + "\"");
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.RedirectStandardOutput = true;
        info.RedirectStandardError = true;
        info.StandardOutputEncoding = Encoding.UTF8;
        info.StandardErrorEncoding = Encoding.UTF8;
        info.WorkingDirectory = dir;
        foreach (var setting in Settings())
        {
            info.EnvironmentVariables[setting.Key] = setting.Value;
        }
        info.EnvironmentVariables["PYTHONUTF8"] = "1";
        var process = new Process();
        process.StartInfo = info;
        process.EnableRaisingEvents = true;
        process.OutputDataReceived += (sender, e) => WriteLog(e.Data);
        process.ErrorDataReceived += (sender, e) => WriteLog(e.Data);
        process.Exited += delegate { ui.Post(delegate { ChildEnded(process); }, null); };
        stopEvent.Reset();  // set by the last stop; this Lectern waits for the next
        try
        {
            process.Start();
        }
        catch (Win32Exception error)
        {
            detail = error.Message;
            state = State.Failed;
            Refresh();
            return;
        }
        if (!job.Add(process))
        {
            // Not in the job: quitting or closing the app leaves this Lectern and its models
            // running, and nothing ends them. Not expected on Windows 10 and 11, which nest jobs.
            WriteLog("---- couldn't put Lectern in the app's job (error " + Marshal.GetLastWin32Error().ToString()
                + "): if the app is closed, Lectern and its models may keep running ----");
        }
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();
        child = process;
        WriteLog("---- Lectern " + version + " started " + DateTime.UtcNow.ToString("s") + "Z ----");
        detail = "";
        link = null;  // the settings may give this start another port
        runs++;
        state = State.Starting;
        healthTimer.Start();
        Refresh();
    }

    private void Stop(Action then)
    {
        if (child == null)
        {
            if (then != null) then();
            return;
        }
        afterStop = then;
        healthTimer.Stop();
        watchTimer.Stop();
        detail = "";
        state = State.Stopping;
        Refresh();
        AskToStop();
        stopTimer.Interval = InterruptPatienceMs;
        stopTimer.Start();
    }

    private void AskToStop()
    {
        if (child != null) stopEvent.Set();
    }

    // It didn't stop by itself: it, and everything it started, is ended now.
    private void StopTookTooLong()
    {
        stopTimer.Stop();
        if (child == null) return;
        WriteLog("---- Lectern didn't stop in time; ended it and what it had started ----");
        job.EndAll();
    }

    private void ChildEnded(Process ended)
    {
        if (ended != child) return;
        child = null;
        stopTimer.Stop();
        healthTimer.Stop();
        watchTimer.Stop();
        // What a Lectern cut short left behind (a model runner) goes with it.
        job.EndAll();
        int status = ended.ExitCode;
        ended.Dispose();
        if (state == State.Stopping)
        {
            state = State.Stopped;
            detail = "";
            Action next = afterStop;
            afterStop = null;
            Refresh();
            if (next != null) next();
        }
        else
        {
            state = State.Failed;
            detail = "see the log (exit " + status.ToString() + ")";
            Refresh();
        }
    }

    private void Quit()
    {
        if (quitting) return;
        quitting = true;
        Stop(delegate
        {
            icon.Visible = false;
            Application.ExitThread();
        });
    }

    // --- Lectern answering -----------------------------------------------------------------

    // Lectern counts as running once /health answers on its port. A child that ends is "failed"
    // whatever answers there, so another Lectern holding the port isn't taken for ours.
    private void AskHealth()
    {
        if (state != State.Starting) return;
        int port = Port();
        ThreadPool.QueueUserWorkItem(delegate
        {
            if (!Answers(port, 2000)) return;
            ui.Post(delegate
            {
                if (state != State.Starting) return;
                healthTimer.Stop();
                state = State.Running;
                misses = 0;
                answering = true;
                watchTimer.Start();
                Refresh();
                AskLink();  // now, so Open Lectern needn't wait for it
                if (openWhenReady)
                {
                    openWhenReady = false;
                    OpenLectern();
                }
            }, null);
        });
    }

    // Running: still answering? Said in the menu and the log when it stops, and when it's back.
    private void Watch()
    {
        if (state != State.Running) return;
        int port = Port();
        ThreadPool.QueueUserWorkItem(delegate
        {
            bool up = Answers(port, WatchTimeoutMs);
            ui.Post(delegate
            {
                if (state != State.Running) return;
                if (up)
                {
                    misses = 0;
                    if (answering) return;
                    answering = true;
                    WriteLog("---- Lectern answers again ----");
                }
                else
                {
                    misses++;
                    if (misses != WatchMisses || !answering) return;
                    answering = false;
                    WriteLog("---- Lectern hasn't answered on port " + port.ToString() + " for "
                        + (WatchMisses * WatchIntervalMs / 1000).ToString()
                        + " s, though it's still running: Restart, in this menu ----");
                }
                Refresh();
            }, null);
        });
    }

    private static bool Answers(int port, int timeoutMs)
    {
        try
        {
            var request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port.ToString() + "/health");
            request.Proxy = null;  // 127.0.0.1 is never asked through a proxy
            request.Timeout = timeoutMs;
            using (var response = (HttpWebResponse)request.GetResponse())
            {
                return response.StatusCode == HttpStatusCode.OK;
            }
        }
        catch (WebException)
        {
            return false;
        }
    }

    private void Reopened()
    {
        if (state == State.Running) OpenLectern();
        else if (state == State.Starting) openWhenReady = true;
        else if (child == null)
        {
            openWhenReady = true;
            Start();
        }
    }

    // --- the menu --------------------------------------------------------------------------

    // The link comes from bin\lectern-url.cmd (the settings loaded, the token from its file) and
    // goes to the browser and nowhere else.
    private void ShowMenu()
    {
        if (ShowContextMenu != null) ShowContextMenu.Invoke(icon, null);
        else menu.Show(Cursor.Position);  // not .NET Framework's NotifyIcon
    }

    private void OpenLectern()
    {
        if (state != State.Running) return;
        if (link != null)
        {
            Process.Start(link);
            return;
        }
        openWhenAnswered = true;
        AskLink();
    }

    // lectern-url.cmd starts Python, which takes a second or more: it's asked off the menu's
    // thread, so the tray doesn't freeze, and once each time Lectern starts.
    private void AskLink()
    {
        if (asking) return;
        asking = true;
        int run = runs;
        string script = Path.Combine(dir, "bin", "lectern-url.cmd");
        ThreadPool.QueueUserWorkItem(delegate
        {
            string answer = ReadLink(script);
            ui.Post(delegate { LinkAnswered(run, answer); }, null);
        });
    }

    private static string ReadLink(string script)
    {
        try
        {
            var info = new ProcessStartInfo(script);
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.RedirectStandardOutput = true;
            using (var ask = Process.Start(info))
            {
                string answer = ask.StandardOutput.ReadToEnd().Trim();
                ask.WaitForExit(10000);
                return answer;
            }
        }
        catch (Win32Exception)
        {
            return "";
        }
    }

    private void LinkAnswered(int run, string answer)
    {
        asking = false;
        if (run != runs)
        {
            // Lectern restarted meanwhile, perhaps on another port: ask again for this start.
            if (state == State.Running) AskLink();
            return;
        }
        bool open = openWhenAnswered;
        openWhenAnswered = false;
        if (state != State.Running) return;  // stopped meanwhile
        if (!answer.StartsWith("http://") && !answer.StartsWith("https://"))
        {
            detail = "couldn't get the link (bin\\lectern-url.cmd)";
            Refresh();
            return;
        }
        link = answer;
        if (open) Process.Start(link);
    }

    private void ShowLog()
    {
        if (File.Exists(logPath)) Process.Start("notepad.exe", "\"" + logPath + "\"");
    }

    private void Refresh()
    {
        string title;
        switch (state)
        {
            case State.Starting: title = "Starting…"; break;
            case State.Running:
                if (!answering) title = "Lectern isn't answering · Restart it";
                else title = version.Length == 0 ? "Lectern is running" : "Lectern is running · " + version;
                break;
            case State.Stopping: title = "Stopping…"; break;
            case State.Failed: title = "Stopped unexpectedly"; break;
            default: title = "Stopped"; break;
        }
        if (detail.Length > 0) title += ": " + detail;
        statusLine.Text = title;
        openItem.Enabled = state == State.Running;
        logItem.Enabled = File.Exists(logPath);
        startStopItem.Text = child == null ? "Start" : "Stop";
        startStopItem.Enabled = state != State.Stopping && !quitting;
        restartItem.Enabled = (state == State.Starting || state == State.Running) && !quitting;
        string tip = "Lectern: " + title;
        icon.Text = tip.Length > 63 ? tip.Substring(0, 62) + "…" : tip;  // its limit
        ShowLook();
    }

    // The book open while Lectern runs, closed otherwise; both in the taskbar's light or dark.
    private void ShowLook()
    {
        bool light = Look.LightTaskbar();
        look.Follow(menu, light);
        glyph.Show(icon, state == State.Running && answering, light);
    }

    // --- the settings ----------------------------------------------------------------------

    // lectern.env: KEY=VALUE lines, # comments, as the Mac and Linux installs read it.
    private Dictionary<string, string> Settings()
    {
        var found = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        string path = Path.Combine(dir, "lectern.env");
        if (!File.Exists(path)) return found;
        foreach (string raw in File.ReadAllLines(path))
        {
            string line = raw.Trim();
            if (line.Length == 0 || line.StartsWith("#")) continue;
            int equals = line.IndexOf('=');
            if (equals <= 0) continue;
            found[line.Substring(0, equals).Trim()] = line.Substring(equals + 1).Trim();
        }
        return found;
    }

    // LECTERN_PORT in the settings, else 8765.
    private int Port()
    {
        string value;
        int port;
        if (Settings().TryGetValue("LECTERN_PORT", out value) && int.TryParse(value, out port)) return port;
        return 8765;
    }

    // --- the log ---------------------------------------------------------------------------

    // Opened afresh with each start, rotated past its limit.
    private void OpenLog()
    {
        lock (logLock)
        {
            if (log != null) log.Dispose();
            log = null;
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(logPath));
                if (File.Exists(logPath) && new FileInfo(logPath).Length > LogLimit)
                {
                    string older = logPath + ".1";
                    if (File.Exists(older)) File.Delete(older);
                    File.Move(logPath, older);
                }
                log = new StreamWriter(new FileStream(logPath, FileMode.Append, FileAccess.Write, FileShare.ReadWrite), new UTF8Encoding(false));
                log.AutoFlush = true;
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
    }

    // The link `serve` prints carries the access token: that line never reaches the file.
    private void WriteLog(string line)
    {
        if (line == null || line.Contains(TokenMark)) return;
        lock (logLock)
        {
            if (log == null) return;
            try { log.WriteLine(line); }
            catch (IOException) { }
        }
    }

    public void Dispose()
    {
        SystemEvents.UserPreferenceChanged -= themeChanged;  // a static event: it would keep us
        icon.Dispose();
        glyph.Dispose();
        menu.Dispose();
        healthTimer.Dispose();
        stopTimer.Dispose();
        watchTimer.Dispose();
        job.Dispose();
        lock (logLock)
        {
            if (log != null) log.Dispose();
        }
    }
}

// --- how it looks ------------------------------------------------------------------------

// The tray icon and its menu follow the taskbar's mode (Settings > Personalization > Colors,
// "Choose your Windows mode"), as Windows' own tray menus do, not the apps' mode. Windows 10
// before 1903 doesn't set it: its taskbar was always dark.
internal static class Look
{
    private const string Personalize = @"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize";

    // Before the first window. Otherwise Windows draws the app at 96 dpi and stretches it, which
    // blurs the menu and the tray icon on any scale above 100%. System-aware, which WinForms on
    // .NET Framework handles: on a second screen with another scale it's stretched there only.
    public static void BeDpiAware()
    {
        SetProcessDPIAware();
    }

    public static bool LightTaskbar()
    {
        using (RegistryKey key = Registry.CurrentUser.OpenSubKey(Personalize))
        {
            object value = key == null ? null : key.GetValue("SystemUsesLightTheme");
            return value is int && (int)value != 0;
        }
    }

    // How much larger than at 96 dpi: 1 at 100%, 1.5 at 150%.
    public static float Scale()
    {
        using (Graphics screen = Graphics.FromHwnd(IntPtr.Zero))
        {
            return screen.DpiX / 96f;
        }
    }

    [DllImport("user32.dll")]
    private static extern bool SetProcessDPIAware();
}

// The tray's book, as the Mac's menu bar has it: open while Lectern runs, closed while it
// doesn't. A glyph of Windows' own icon font (Segoe Fluent Icons on 11, Segoe MDL2 Assets on
// 10), as Windows' own tray icons are, drawn at the size the tray shows it so it stays sharp.
internal sealed class TrayGlyph : IDisposable
{
    private const string OpenBook = "";  // ReadingMode
    private const string ClosedBook = "";  // Dictionary
    private static string fontName;
    private Icon shown;
    private IntPtr handle = IntPtr.Zero;
    private string drawn = "";

    public void Show(NotifyIcon tray, bool open, bool light)
    {
        int size = SystemInformation.SmallIconSize.Width;
        string wanted = (open ? "open " : "closed ") + (light ? "light " : "dark ") + size.ToString();
        if (wanted == drawn) return;
        Icon before = shown;
        IntPtr beforeHandle = handle;
        using (Bitmap bitmap = Draw(open, light, size))
        {
            handle = bitmap.GetHicon();
        }
        shown = Icon.FromHandle(handle);
        tray.Icon = shown;
        drawn = wanted;
        Release(before, beforeHandle);  // only once the tray has the new one
    }

    public static Bitmap Draw(bool open, bool light, int size)
    {
        var glyph = new Bitmap(size, size);
        using (Graphics g = Graphics.FromImage(glyph))
        using (var font = new Font(FontName(), size, GraphicsUnit.Pixel))
        using (var ink = new SolidBrush(light ? Color.Black : Color.White))
        using (var format = new StringFormat(StringFormat.GenericTypographic))
        {
            // Not GenericTypographic's LineLimit: these fonts' lines are taller than the square,
            // and a line that doesn't fit isn't drawn at all.
            format.FormatFlags = StringFormatFlags.NoClip | StringFormatFlags.NoWrap;
            format.Alignment = StringAlignment.Center;
            format.LineAlignment = StringAlignment.Center;
            // Greyscale: ClearType's coloured edges need an opaque background to land on.
            g.TextRenderingHint = TextRenderingHint.AntiAliasGridFit;
            g.DrawString(open ? OpenBook : ClosedBook, font, ink, new RectangleF(0, 0, size, size), format);
        }
        return Centred(glyph);
    }

    // Moved, by whole pixels so it stays sharp, to the middle of the square by where its ink is:
    // MDL2 Assets (Windows 10) sets the closed book up to a quarter of the square left of it.
    private static Bitmap Centred(Bitmap glyph)
    {
        int size = glyph.Width;
        int left = size, top = size, right = -1, bottom = -1;
        for (int x = 0; x < size; x++)
        {
            for (int y = 0; y < size; y++)
            {
                if (glyph.GetPixel(x, y).A == 0) continue;
                left = Math.Min(left, x);
                right = Math.Max(right, x);
                top = Math.Min(top, y);
                bottom = Math.Max(bottom, y);
            }
        }
        if (right < 0) return glyph;  // nothing drawn: nothing to move
        int dx = (size - 1 - right - left) / 2;
        int dy = (size - 1 - bottom - top) / 2;
        if (dx == 0 && dy == 0) return glyph;
        var centred = new Bitmap(size, size);
        using (Graphics g = Graphics.FromImage(centred))
        {
            g.DrawImageUnscaled(glyph, dx, dy);
        }
        glyph.Dispose();
        return centred;
    }

    private static string FontName()
    {
        if (fontName != null) return fontName;
        fontName = "Segoe MDL2 Assets";
        using (var installed = new InstalledFontCollection())
        {
            foreach (FontFamily family in installed.Families)
            {
                if (family.Name == "Segoe Fluent Icons") fontName = family.Name;
            }
        }
        return fontName;
    }

    // Icon.FromHandle doesn't own the handle GetHicon made: it's given back here.
    private static void Release(Icon icon, IntPtr iconHandle)
    {
        if (icon != null) icon.Dispose();
        if (iconHandle != IntPtr.Zero) DestroyIcon(iconHandle);
    }

    public void Dispose()
    {
        Release(shown, handle);
        shown = null;
        handle = IntPtr.Zero;
    }

    [DllImport("user32.dll")]
    private static extern bool DestroyIcon(IntPtr icon);
}

// The tray's menu, drawn as Windows 11 draws its own: Segoe UI in roomy rows, a rounded
// highlight, no icon column, in the taskbar's light or dark. Windows 11 rounds the menu's
// corners and draws its edge itself; Windows 10 keeps them square, and the edge is drawn here.
internal sealed class MenuLook : ToolStripRenderer
{
    private const int CornerPreference = 33;  // DWMWA_WINDOW_CORNER_PREFERENCE, Windows 11
    private const int BorderColor = 34;  // DWMWA_BORDER_COLOR, Windows 11
    private const int Round = 2;  // DWMWCP_ROUND, what Windows 11 gives its own menus

    private static readonly Palette LightPalette = new Palette(
        Color.FromArgb(249, 249, 249), Color.FromArgb(26, 26, 26), Color.FromArgb(145, 145, 145),
        Color.FromArgb(236, 236, 236), Color.FromArgb(224, 224, 224), Color.FromArgb(214, 214, 214));
    private static readonly Palette DarkPalette = new Palette(
        Color.FromArgb(44, 44, 44), Color.FromArgb(255, 255, 255), Color.FromArgb(130, 130, 130),
        Color.FromArgb(61, 61, 61), Color.FromArgb(64, 64, 64), Color.FromArgb(70, 70, 70));

    private readonly float scale = Look.Scale();
    private Palette colors = LightPalette;
    private bool light = true;
    private bool rounded;

    public static MenuLook Apply(ContextMenuStrip menu)
    {
        var look = new MenuLook();
        menu.Renderer = look;
        menu.ShowImageMargin = false;
        menu.ShowCheckMargin = false;
        menu.Font = SystemFonts.MenuFont;
        menu.Padding = new Padding(0, look.Px(4), 0, look.Px(4));
        menu.MinimumSize = new Size(look.Px(200), 0);
        foreach (ToolStripItem item in menu.Items)
        {
            if (item is ToolStripSeparator)
            {
                item.AutoSize = false;
                item.Height = look.Px(9);
            }
            else
            {
                item.Padding = new Padding(look.Px(8), look.Px(6), look.Px(24), look.Px(6));
            }
        }
        menu.HandleCreated += delegate { look.Frame(menu); };
        return look;
    }

    public void Follow(ContextMenuStrip menu, bool lightNow)
    {
        if (lightNow == light) return;
        light = lightNow;
        colors = light ? LightPalette : DarkPalette;
        if (menu.IsHandleCreated) Frame(menu);
        menu.Invalidate();
    }

    // Windows 11's rounded corners and the edge's colour; Windows 10 answers no to both.
    private void Frame(ContextMenuStrip menu)
    {
        int round = Round;
        rounded = DwmSetWindowAttribute(menu.Handle, CornerPreference, ref round, sizeof(int)) == 0;
        int edge = ColorTranslator.ToWin32(colors.Edge);
        DwmSetWindowAttribute(menu.Handle, BorderColor, ref edge, sizeof(int));
    }

    private int Px(int at96)
    {
        return (int)Math.Round(at96 * scale);
    }

    protected override void OnRenderToolStripBackground(ToolStripRenderEventArgs e)
    {
        e.Graphics.Clear(colors.Back);
    }

    protected override void OnRenderToolStripBorder(ToolStripRenderEventArgs e)
    {
        if (rounded) return;
        using (var pen = new Pen(colors.Edge))
        {
            e.Graphics.DrawRectangle(pen, 0, 0, e.ToolStrip.Width - 1, e.ToolStrip.Height - 1);
        }
    }

    protected override void OnRenderMenuItemBackground(ToolStripItemRenderEventArgs e)
    {
        if (!e.Item.Selected || !e.Item.Enabled) return;
        var area = new Rectangle(Px(4), Px(1), e.Item.Width - Px(8), e.Item.Height - Px(2));
        int corner = Px(4) * 2;
        using (var shape = new GraphicsPath())
        using (var fill = new SolidBrush(colors.Hover))
        {
            shape.AddArc(area.Left, area.Top, corner, corner, 180, 90);
            shape.AddArc(area.Right - corner, area.Top, corner, corner, 270, 90);
            shape.AddArc(area.Right - corner, area.Bottom - corner, corner, corner, 0, 90);
            shape.AddArc(area.Left, area.Bottom - corner, corner, corner, 90, 90);
            shape.CloseFigure();
            e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
            e.Graphics.FillPath(fill, shape);
            e.Graphics.SmoothingMode = SmoothingMode.Default;
        }
    }

    protected override void OnRenderItemText(ToolStripItemTextRenderEventArgs e)
    {
        var area = new Rectangle(Px(16), 0, e.Item.Width - Px(24), e.Item.Height);
        // No background colour: it would be a box over the highlight.
        TextRenderer.DrawText(e.Graphics, e.Text, e.TextFont, area,
            e.Item.Enabled ? colors.Text : colors.Dimmed,
            TextFormatFlags.Left | TextFormatFlags.VerticalCenter | TextFormatFlags.SingleLine |
            TextFormatFlags.NoPrefix | TextFormatFlags.EndEllipsis);
    }

    protected override void OnRenderSeparator(ToolStripSeparatorRenderEventArgs e)
    {
        int y = e.Item.Height / 2;
        using (var pen = new Pen(colors.Line))
        {
            e.Graphics.DrawLine(pen, 0, y, e.Item.Width, y);
        }
    }

    private sealed class Palette
    {
        public readonly Color Back, Text, Dimmed, Hover, Line, Edge;

        public Palette(Color back, Color text, Color dimmed, Color hover, Color line, Color edge)
        {
            Back = back;
            Text = text;
            Dimmed = dimmed;
            Hover = hover;
            Line = line;
            Edge = edge;
        }
    }

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr window, int attribute, ref int value, int size);
}

// A job object that ends every process in it when it's closed: when the app ends, however it
// ends, nothing Lectern started stays behind.
internal sealed class JobObject : IDisposable
{
    private const int JobObjectExtendedLimitInformation = 9;
    private const uint KillOnJobClose = 0x2000;
    private readonly IntPtr handle;

    public JobObject()
    {
        handle = CreateJobObject(IntPtr.Zero, null);
        var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
        info.BasicLimitInformation.LimitFlags = KillOnJobClose;
        int size = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
        IntPtr buffer = Marshal.AllocHGlobal(size);
        try
        {
            Marshal.StructureToPtr(info, buffer, false);
            SetInformationJobObject(handle, JobObjectExtendedLimitInformation, buffer, (uint)size);
        }
        finally
        {
            Marshal.FreeHGlobal(buffer);
        }
    }

    // False when the process couldn't be put in it (Marshal.GetLastWin32Error says why).
    public bool Add(Process process)
    {
        return AssignProcessToJobObject(handle, process.Handle);
    }

    public void EndAll()
    {
        TerminateJobObject(handle, 1);
    }

    public void Dispose()
    {
        CloseHandle(handle);
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IO_COUNTERS
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateJobObject(IntPtr attributes, string name);

    [DllImport("kernel32.dll")]
    private static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint length);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll")]
    private static extern bool TerminateJobObject(IntPtr job, uint exitCode);

    [DllImport("kernel32.dll")]
    private static extern bool CloseHandle(IntPtr handle);
}
