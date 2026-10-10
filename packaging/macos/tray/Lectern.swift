// Lectern's menu-bar app for macOS. It runs Lectern as its child process
// (<install folder>/bin/lectern-serve), shows whether it's up, opens it in the browser, keeps
// its output in ~/Library/Logs/Lectern/lectern.log, and on Shut Down stops it, its models
// with it, and leaves.
// One file, AppKit only, built by packaging/macos/build_app.sh with swiftc: no Xcode project.
import AppKit

enum State {
    case stopped, starting, running, stopping
    case failed  // Lectern ended without being asked to
}

@main
final class LecternApp: NSObject, NSApplicationDelegate {
    // Stopping is SIGINT, as Ctrl+C: Lectern then stops its model runners itself. After the
    // same patience as the Linux unit's TimeoutStopSec it gets SIGTERM, which it treats the
    // same way. Never SIGKILL: nothing would clean up the llama-server processes left behind.
    private static let interruptPatience: TimeInterval = 30
    private static let terminatePatience: TimeInterval = 10

    // The install folder (~/.lectern) and the version, written into Info.plist at build time.
    private let dir = Bundle.main.object(forInfoDictionaryKey: "LecternDir") as? String ?? ""
    private let version =
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? ""

    private var state = State.stopped { didSet { refresh() } }
    private var detail = ""  // said after the status, when there's more to say
    private var child: Process?
    // A Lectern this app didn't start: its own child from before it crashed or was force-quit,
    // found again by the pid file. It can be signalled and watched, but isn't a Process here.
    private var adopted: pid_t?
    private var aliveTimer: Timer?
    private var pid: pid_t? { child?.processIdentifier ?? adopted }
    private var afterStop: (() -> Void)?
    // What Lectern had started when it was asked to stop: its model runners, and anything else
    // it was running, such as a script in the sandbox. It stops them itself on the way out.
    // Any still there once it's gone (it was cut short) are ended by the app, so that stopping
    // never leaves a model in memory.
    private var runners: [(pid: pid_t, program: String)] = []
    private var healthTimer: Timer?
    private var stopTimers: [Timer] = []
    private var signalSource: DispatchSourceSignal?
    private var openWhenReady = false

    // Lectern's output, without the access link `serve` prints when it starts.
    private let logURL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/Lectern/lectern.log")
    private var log: FileHandle?
    private var unfinishedLine = Data()
    private static let tokenMark = Data("#token=".utf8)
    private static let logLimit = 5_000_000  // bytes; past it the file becomes lectern.log.1

    private var statusItem: NSStatusItem!
    private let statusLine = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let openItem = NSMenuItem(title: "Open Lectern", action: #selector(openLectern), keyEquivalent: "o")
    private let logItem = NSMenuItem(title: "Show Log", action: #selector(showLog), keyEquivalent: "")
    private let startStopItem = NSMenuItem(title: "", action: #selector(startOrStop), keyEquivalent: "")
    private let restartItem = NSMenuItem(title: "Restart", action: #selector(restart), keyEquivalent: "")

    static func main() {
        // For build_app.sh: write the icon's .iconset into the folder given, and leave.
        let arguments = CommandLine.arguments
        if let flag = arguments.firstIndex(of: "--draw-icon"), flag + 1 < arguments.count {
            exit(Icon.writeSet(into: arguments[flag + 1]) ? 0 : 1)
        }
        let app = NSApplication.shared
        let delegate = LecternApp()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        app.run()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        let menu = NSMenu()
        menu.autoenablesItems = false  // the items are enabled by state, in refresh()
        statusLine.isEnabled = false
        for item in [openItem, startStopItem, restartItem, logItem] { item.target = self }
        let quitItem = NSMenuItem(
            title: "Shut Down Lectern", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        quitItem.target = NSApp
        for item in [
            statusLine, .separator(), openItem, startStopItem, restartItem, .separator(), logItem, quitItem,
        ] {
            menu.addItem(item)
        }
        statusItem.menu = menu
        quitOnSIGTERM()

        // Opened by hand, the browser follows once Lectern answers. Not when the installer
        // starts the app (--background), nor when macOS does at login.
        let launch = NSAppleEventManager.shared().currentAppleEvent
        let atLogin =
            launch?.paramDescriptor(forKeyword: AEKeyword(keyAEPropData))?.enumCodeValue
            == OSType(keyAELaunchedAsLogInItem)
        openWhenReady = !CommandLine.arguments.contains("--background") && !atLogin
        refresh()
        if let left = lecternLeftRunning() { adopt(left) } else { start() }
    }

    // Opening the app again while it runs (Finder, Spotlight) opens Lectern in the browser.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if state == .running {
            openLectern()
        } else if pid == nil {
            openWhenReady = true
            start()
        }
        return false
    }

    // `kill <pid>` (the installer's way to stop the app before replacing it) quits like the
    // menu does, so Lectern is stopped first. A handler, not SIG_IGN: an ignored signal would
    // be inherited by Lectern, which then couldn't be told to stop while it starts.
    private func quitOnSIGTERM() {
        signal(SIGTERM) { _ in }
        let source = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        // Through the run loop, not from this block: quitting waits for Lectern in a nested run
        // loop, and the main queue (where the child's end is reported) isn't served inside
        // one of its own blocks.
        source.setEventHandler { RunLoop.main.perform(inModes: [.common]) { NSApp.terminate(nil) } }
        source.resume()
        signalSource = source
    }

    // MARK: Lectern, the child process

    private var pidFile: String { dir + "/app/lectern.pid" }

    private func start() {
        guard pid == nil else { return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: dir + "/bin/lectern-serve")
        process.standardInput = FileHandle.nullDevice
        let output = Pipe()
        process.standardOutput = output
        process.standardError = output
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty {  // every writer is gone
                handle.readabilityHandler = nil
                DispatchQueue.main.async { self?.finishLogLine() }
                return
            }
            DispatchQueue.main.async { self?.writeLog(data) }
        }
        process.terminationHandler = { [weak self] ended in
            DispatchQueue.main.async { self?.childEnded(ended) }
        }
        do {
            try process.run()
        } catch {
            detail = error.localizedDescription
            state = .failed
            return
        }
        child = process
        try? "\(process.processIdentifier)\n".write(toFile: pidFile, atomically: true, encoding: .utf8)
        writeLog(Data("---- Lectern \(version) started \(ISO8601DateFormatter().string(from: Date())) ----\n".utf8))
        detail = ""
        state = .starting
        healthTimer = timer(every: 1, repeats: true) { [weak self] in self?.askHealth() }
    }

    private func stop(then next: (() -> Void)? = nil) {
        guard let pid else {
            next?()
            return
        }
        afterStop = next
        healthTimer?.invalidate()
        detail = ""
        state = .stopping
        runners = children(of: pid).map { (pid: $0, program: program(of: $0)) }
        kill(pid, SIGINT)
        stopTimers = [
            timer(every: Self.interruptPatience, repeats: false) { [weak self] in
                if self?.pid == pid { kill(pid, SIGTERM) }
            },
            timer(every: Self.interruptPatience + Self.terminatePatience, repeats: false) { [weak self] in
                self?.detail = "still stopping (process \(pid))"
                self?.refresh()
            },
        ]
    }

    private func childEnded(_ ended: Process) {
        guard ended === child else { return }
        child = nil
        lecternEnded(exit: ended.terminationStatus)
    }

    private func lecternEnded(exit status: Int32?) {
        healthTimer?.invalidate()
        aliveTimer?.invalidate()
        stopTimers.forEach { $0.invalidate() }
        stopTimers = []
        try? FileManager.default.removeItem(atPath: pidFile)
        endRunnersLeftBehind()
        if state == .stopping {
            detail = ""
            state = .stopped
            let next = afterStop
            afterStop = nil
            next?()
        } else {
            detail = status.map { "see the log (exit \($0))" } ?? "see the log"
            state = .failed
        }
    }

    // MARK: What Lectern leaves behind

    // Of the processes noted when Lectern was asked to stop, those that outlived it: the same
    // program under the same number, now with no parent. They get SIGTERM, which a model
    // runner ends on.
    private func endRunnersLeftBehind() {
        let left = runners.filter {
            !$0.program.isEmpty && parent(of: $0.pid) == 1 && program(of: $0.pid) == $0.program
        }
        runners = []
        guard !left.isEmpty else { return }
        left.forEach { kill($0.pid, SIGTERM) }
        writeLog(Data("---- ended \(left.count) process(es) Lectern left running ----\n".utf8))
    }

    private func children(of process: pid_t) -> [pid_t] {
        var found = [pid_t](repeating: 0, count: 512)
        let count = proc_listchildpids(process, &found, Int32(found.count * MemoryLayout<pid_t>.size))
        return count > 0 ? found.prefix(Int(count)).filter { $0 > 1 } : []
    }

    private func program(of process: pid_t) -> String {
        var path = [CChar](repeating: 0, count: 4096)
        return proc_pidpath(process, &path, UInt32(path.count)) > 0 ? String(cString: path) : ""
    }

    private func parent(of process: pid_t) -> pid_t? {
        var info = proc_bsdinfo()
        let size = Int32(MemoryLayout<proc_bsdinfo>.size)
        guard proc_pidinfo(process, PROC_PIDTBSDINFO, 0, &info, size) == size else { return nil }
        return pid_t(info.pbi_ppid)
    }

    // MARK: A Lectern left running

    // The process the pid file names, when it's still there and is Lectern's. A number the
    // system has since given to something else is never signalled. A stale file is removed.
    private func lecternLeftRunning() -> pid_t? {
        guard let written = try? String(contentsOfFile: pidFile, encoding: .utf8),
            let left = pid_t(written.trimmingCharacters(in: .whitespacesAndNewlines)), left > 1
        else { return nil }
        if kill(left, 0) == 0, isLecterns(left) { return left }
        try? FileManager.default.removeItem(atPath: pidFile)
        return nil
    }

    // Lectern's own process runs <install folder>/app/venv/bin/lectern, or is still the
    // bin/lectern-serve that becomes it a moment after being started. Compared as real paths:
    // the system may hand a path on (/var/…) in another spelling than it was given (/private/var/…).
    private func isLecterns(_ process: pid_t) -> Bool {
        let ours = [dir + "/app/venv/bin/lectern", dir + "/bin/lectern-serve"].compactMap(realPath)
        return arguments(of: process).prefix(2).compactMap(realPath).contains { ours.contains($0) }
    }

    private func realPath(_ path: String) -> String? {
        guard let resolved = realpath(path, nil) else { return nil }
        defer { free(resolved) }
        return String(cString: resolved)
    }

    // What a process was started with. The system gives it as: the number of arguments, the
    // program's path, padding, then the arguments and the environment, each ended by a NUL.
    private func arguments(of process: pid_t) -> [String] {
        var name = [CTL_KERN, KERN_PROCARGS2, process]
        var size = 0
        guard sysctl(&name, 3, nil, &size, nil, 0) == 0, size > MemoryLayout<Int32>.size else {
            return []
        }
        var bytes = [UInt8](repeating: 0, count: size)
        guard sysctl(&name, 3, &bytes, &size, nil, 0) == 0 else { return [] }
        let count = Int(bytes.withUnsafeBytes { $0.load(as: Int32.self) })
        let strings = bytes[MemoryLayout<Int32>.size..<size].split(separator: 0)
        return strings.dropFirst().prefix(count).map { String(decoding: $0, as: UTF8.self) }
    }

    private func adopt(_ left: pid_t) {
        adopted = left
        // Its output went to the app that started it, which is gone: nothing more reaches the
        // log until Lectern is started again. Said here, and in the menu (refresh()).
        writeLog(Data("---- took over Lectern (process \(left)), left running; its output isn't kept until Restart ----\n".utf8))
        detail = ""
        state = .starting  // "running" once /health answers, as for a child
        healthTimer = timer(every: 1, repeats: true) { [weak self] in self?.askHealth() }
        aliveTimer = timer(every: 1, repeats: true) { [weak self] in
            guard let self, self.adopted == left, kill(left, 0) != 0 else { return }
            self.adopted = nil
            self.lecternEnded(exit: nil)
        }
    }

    // MARK: The log

    private func writeLog(_ data: Data) {
        openLog()
        unfinishedLine.append(data)
        while let newline = unfinishedLine.firstIndex(of: 0x0A) {
            writeLogLine(unfinishedLine.prefix(through: newline))
            unfinishedLine.removeSubrange(...newline)
        }
        if unfinishedLine.count > 65_536 {  // one endless line: don't hold it all
            writeLogLine(unfinishedLine)
            unfinishedLine.removeAll()
        }
    }

    // What Lectern printed last, when it didn't end the line.
    private func finishLogLine() {
        guard !unfinishedLine.isEmpty else { return }
        writeLogLine(unfinishedLine + Data("\n".utf8))
        unfinishedLine.removeAll()
    }

    // The link `serve` prints carries the access token: that line never reaches the file.
    private func writeLogLine(_ line: Data) {
        if line.range(of: Self.tokenMark) == nil { try? log?.write(contentsOf: line) }
    }

    private func openLog() {
        guard log == nil else { return }
        let files = FileManager.default
        try? files.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        let size = (try? files.attributesOfItem(atPath: logURL.path))?[.size] as? Int ?? 0
        if size > Self.logLimit {
            let older = logURL.appendingPathExtension("1")
            try? files.removeItem(at: older)
            try? files.moveItem(at: logURL, to: older)
        }
        if !files.fileExists(atPath: logURL.path) {
            files.createFile(atPath: logURL.path, contents: nil)
        }
        try? files.setAttributes([.posixPermissions: 0o600], ofItemAtPath: logURL.path)
        log = try? FileHandle(forWritingTo: logURL)
        _ = try? log?.seekToEnd()
    }

    // Lectern counts as running once /health answers on its port. A child that ends is
    // "failed" whatever answers there, so another Lectern holding the port isn't taken for ours.
    private func askHealth() {
        guard state == .starting, let url = URL(string: "http://127.0.0.1:\(port())/health") else {
            return
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 2
        Self.local.dataTask(with: request) { [weak self] _, response, _ in
            guard (response as? HTTPURLResponse)?.statusCode == 200 else { return }
            DispatchQueue.main.async {
                guard let self, self.state == .starting else { return }
                self.healthTimer?.invalidate()
                self.state = .running
                if self.openWhenReady {
                    self.openWhenReady = false
                    self.openLectern()
                }
            }
        }.resume()
    }

    // 127.0.0.1 is never asked through a proxy the system may have set.
    private static let local: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.connectionProxyDictionary = [:]
        return URLSession(configuration: configuration)
    }()

    // LECTERN_PORT in the settings, the last one given, as install.sh reads it; else 8765.
    private func port() -> Int {
        let settings = (try? String(contentsOfFile: dir + "/lectern.env", encoding: .utf8)) ?? ""
        let given = settings.split(separator: "\n").last { $0.hasPrefix("LECTERN_PORT=") }
        let value = given?.dropFirst("LECTERN_PORT=".count).trimmingCharacters(in: .whitespaces)
        return value.flatMap { Int($0) } ?? 8765
    }

    // MARK: The menu

    // The link comes from bin/lectern-url (the settings loaded, the token from its file) and
    // goes to the browser and nowhere else.
    @objc private func openLectern() {
        guard state == .running else { return }
        let ask = Process()
        ask.executableURL = URL(fileURLWithPath: dir + "/bin/lectern-url")
        let answer = Pipe()
        ask.standardOutput = answer
        ask.standardError = FileHandle.nullDevice
        ask.terminationHandler = { [weak self] _ in
            let printed = answer.fileHandleForReading.readDataToEndOfFile()
            let link = String(decoding: printed, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            DispatchQueue.main.async { self?.openInBrowser(link) }
        }
        do {
            try ask.run()
        } catch {
            openInBrowser("")
        }
    }

    private func openInBrowser(_ link: String) {
        if let url = URL(string: link), url.scheme == "http" || url.scheme == "https" {
            NSWorkspace.shared.open(url)
        } else if state == .running {
            detail = "couldn't get the link (bin/lectern-url)"
            refresh()
        }
    }

    @objc private func showLog() {
        NSWorkspace.shared.open(logURL)
    }

    @objc private func startOrStop() {
        if pid == nil { start() } else { stop() }
    }

    @objc private func restart() {
        stop { [weak self] in self?.start() }
    }

    // Shut Down (and logging out, and the Mac shutting down) stops Lectern first: the server,
    // and with it every model it has loaded. Only then does the app leave.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard pid != nil else { return .terminateNow }
        let quit = { sender.reply(toApplicationShouldTerminate: true) }
        if state == .stopping { afterStop = quit } else { stop(then: quit) }
        return .terminateLater
    }

    private func refresh() {
        var title: String
        switch state {
        case .stopped: title = "Stopped"
        case .starting: title = "Starting…"
        case .running: title = version.isEmpty ? "Lectern is running" : "Lectern is running · \(version)"
        case .stopping: title = "Stopping…"
        case .failed: title = "Stopped unexpectedly"
        }
        if adopted != nil, state == .starting || state == .running {
            title += " · log paused until Restart"
        }
        if !detail.isEmpty { title += ": \(detail)" }
        statusLine.title = title
        openItem.isEnabled = state == .running
        logItem.isEnabled = FileManager.default.fileExists(atPath: logURL.path)
        startStopItem.title = pid == nil ? "Start" : "Stop"
        startStopItem.isEnabled = state != .stopping
        restartItem.isEnabled = state == .starting || state == .running

        let symbol = state == .running ? "book" : "book.closed"
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: "Lectern")
        image?.isTemplate = true
        statusItem.button?.image = image
        statusItem.button?.title = image == nil ? "Lectern" : ""
        statusItem.button?.toolTip = "Lectern: \(title)"
    }

    // In the common modes, so it also fires while the menu is open and while quitting waits
    // for Lectern to stop (the run loop is in a modal mode then).
    private func timer(every seconds: TimeInterval, repeats: Bool, _ body: @escaping () -> Void) -> Timer {
        let timer = Timer(timeInterval: seconds, repeats: repeats) { _ in body() }
        RunLoop.main.add(timer, forMode: .common)
        return timer
    }
}
