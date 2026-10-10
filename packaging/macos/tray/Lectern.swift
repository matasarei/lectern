// Lectern's menu-bar app for macOS. It runs Lectern as its child process
// (<install folder>/bin/lectern-serve), shows whether it's up, and stops it on Quit.
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
    private var afterStop: (() -> Void)?
    private var healthTimer: Timer?
    private var stopTimers: [Timer] = []
    private var signalSource: DispatchSourceSignal?

    private var statusItem: NSStatusItem!
    private let statusLine = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let startStopItem = NSMenuItem(title: "", action: #selector(startOrStop), keyEquivalent: "")
    private let restartItem = NSMenuItem(title: "Restart", action: #selector(restart), keyEquivalent: "")

    static func main() {
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
        startStopItem.target = self
        restartItem.target = self
        let quitItem = NSMenuItem(
            title: "Quit Lectern", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        quitItem.target = NSApp
        for item in [statusLine, .separator(), startStopItem, restartItem, .separator(), quitItem] {
            menu.addItem(item)
        }
        statusItem.menu = menu
        quitOnSIGTERM()
        refresh()
        start()
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

    private func start() {
        guard child == nil else { return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: dir + "/bin/lectern-serve")
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
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
        detail = ""
        state = .starting
        healthTimer = timer(every: 1, repeats: true) { [weak self] in self?.askHealth() }
    }

    private func stop(then next: (() -> Void)? = nil) {
        guard let child, child.isRunning else {
            next?()
            return
        }
        afterStop = next
        healthTimer?.invalidate()
        detail = ""
        state = .stopping
        child.interrupt()
        let pid = child.processIdentifier
        stopTimers = [
            timer(every: Self.interruptPatience, repeats: false) { [weak self] in
                self?.child?.terminate()
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
        healthTimer?.invalidate()
        stopTimers.forEach { $0.invalidate() }
        stopTimers = []
        if state == .stopping {
            detail = ""
            state = .stopped
            let next = afterStop
            afterStop = nil
            next?()
        } else {
            detail = "exit \(ended.terminationStatus)"
            state = .failed
        }
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

    @objc private func startOrStop() {
        if child == nil { start() } else { stop() }
    }

    @objc private func restart() {
        stop { [weak self] in self?.start() }
    }

    // Quit, logging out and shutting down stop Lectern first.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let child, child.isRunning else { return .terminateNow }
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
        if !detail.isEmpty { title += ": \(detail)" }
        statusLine.title = title
        startStopItem.title = child == nil ? "Start" : "Stop"
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
