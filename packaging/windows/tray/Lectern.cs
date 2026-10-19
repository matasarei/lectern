// Lectern's tray app for Windows. It runs Lectern as its child process
// (<install folder>\app\venv\Scripts\lectern.exe serve), shows whether it's up, opens it in the
// browser, keeps its output in %LOCALAPPDATA%\Lectern\Logs\lectern.log, and on Quit stops it,
// its models with it, and leaves.
// One file, WinForms only, compiled at install time by the csc.exe that ships with Windows
// (.NET Framework 4: C# 5, so no $"" strings and no ?.): packaging\windows\build_tray.ps1.
//
// Stopping is closing Lectern's standard input (serve --stop-on-stdin-close): Windows has no
// Ctrl+C to send a process with no console. Lectern then stops its model runners itself.
// Every process Lectern starts is in a job object that ends with the app, so neither a
// Lectern that won't stop nor the app being killed leaves a model in memory.
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
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
        string dir = InstallFolder();
        bool background = Array.IndexOf(args, "--background") >= 0;
        // One app per install folder. Opened again (Start menu, a second click), it asks the one
        // already running to open Lectern in the browser, and leaves. The installer and the
        // uninstaller ask it to quit through "-quit" (install.ps1 names them the same way).
        string name = "Lectern-" + dir.ToLowerInvariant().Replace('\\', '_').Replace(':', '_');
        bool first;
        using (var one = new Mutex(true, "Local\\" + name, out first))
        using (var reopen = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\" + name + "-open"))
        using (var quit = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\" + name + "-quit"))
        {
            if (!first)
            {
                if (!background) reopen.Set();
                return 0;
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            using (var app = new TrayApp(dir, !background, reopen, quit))
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
}

internal enum State { Stopped, Starting, Running, Stopping, Failed }

internal sealed class TrayApp : IDisposable
{
    private const int InterruptPatienceMs = 30000;  // as the Mac app and the Linux unit give it
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
    private readonly System.Windows.Forms.Timer healthTimer = new System.Windows.Forms.Timer();
    private readonly System.Windows.Forms.Timer stopTimer = new System.Windows.Forms.Timer();
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

    public TrayApp(string dir, bool openWhenReady, EventWaitHandle reopen, EventWaitHandle quit)
    {
        this.dir = dir;
        this.openWhenReady = openWhenReady;
        ui = new WindowsFormsSynchronizationContext();
        SynchronizationContext.SetSynchronizationContext(ui);
        string here = Path.GetDirectoryName(Application.ExecutablePath);
        string versionFile = Path.Combine(here, "Lectern.version");
        version = File.Exists(versionFile) ? File.ReadAllText(versionFile).Trim() : "";
        logPath = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            "Lectern", "Logs", "lectern.log");

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
        icon.ContextMenuStrip = menu;
        icon.Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        icon.DoubleClick += delegate { OpenLectern(); };
        icon.Visible = true;

        healthTimer.Interval = 1000;
        healthTimer.Tick += delegate { AskHealth(); };
        stopTimer.Tick += delegate { StopTookTooLong(); };

        // Signing out, or Windows shutting down: Lectern is stopped first (the job object
        // would end it anyway; this lets it stop its models itself).
        SystemEvents.SessionEnding += delegate { CloseInput(); };
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
            "serve --no-browser --port " + Port().ToString() + " --stop-on-stdin-close");
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.RedirectStandardInput = true;
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
        detail = "";
        state = State.Stopping;
        Refresh();
        CloseInput();
        stopTimer.Interval = InterruptPatienceMs;
        stopTimer.Start();
    }

    private void CloseInput()
    {
        try
        {
            if (child != null && !child.HasExited) child.StandardInput.Close();
        }
        catch (InvalidOperationException) { }
        catch (IOException) { }
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
            bool up = false;
            try
            {
                var request = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + port.ToString() + "/health");
                request.Proxy = null;  // 127.0.0.1 is never asked through a proxy
                request.Timeout = 2000;
                using (var response = (HttpWebResponse)request.GetResponse())
                {
                    up = response.StatusCode == HttpStatusCode.OK;
                }
            }
            catch (WebException) { }
            if (!up) return;
            ui.Post(delegate
            {
                if (state != State.Starting) return;
                healthTimer.Stop();
                state = State.Running;
                Refresh();
                if (openWhenReady)
                {
                    openWhenReady = false;
                    OpenLectern();
                }
            }, null);
        });
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
    private void OpenLectern()
    {
        if (state != State.Running) return;
        string link = "";
        try
        {
            var info = new ProcessStartInfo(Path.Combine(dir, "bin", "lectern-url.cmd"));
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.RedirectStandardOutput = true;
            using (var ask = Process.Start(info))
            {
                link = ask.StandardOutput.ReadToEnd().Trim();
                ask.WaitForExit(10000);
            }
        }
        catch (Win32Exception) { }
        if (link.StartsWith("http://") || link.StartsWith("https://"))
        {
            Process.Start(link);
        }
        else
        {
            detail = "couldn't get the link (bin\\lectern-url.cmd)";
            Refresh();
        }
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
            case State.Running: title = version.Length == 0 ? "Lectern is running" : "Lectern is running · " + version; break;
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
        icon.Dispose();
        menu.Dispose();
        healthTimer.Dispose();
        stopTimer.Dispose();
        job.Dispose();
        lock (logLock)
        {
            if (log != null) log.Dispose();
        }
    }
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
