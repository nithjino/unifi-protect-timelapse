import Darwin
import Foundation

enum BackendProcessError: LocalizedError {
    case executableNotFound([URL])
    case couldNotEncodeRequest(Error)
    case couldNotLaunch(Error)
    case couldNotWriteRequest(Error)

    var errorDescription: String? {
        switch self {
        case let .executableNotFound(locations):
            let paths = locations.map(\.path).joined(separator: "\n")
            return "The bundled timelapse backend could not be found. Checked:\n\(paths)"
        case let .couldNotEncodeRequest(error):
            return "The backend request could not be encoded: \(error.localizedDescription)"
        case let .couldNotLaunch(error):
            return "The timelapse backend could not be started: \(error.localizedDescription)"
        case let .couldNotWriteRequest(error):
            return "The request could not be sent to the timelapse backend: \(error.localizedDescription)"
        }
    }
}

struct BackendCompletion: Sendable {
    let exitCode: Int32
    let wasCancelled: Bool
    let stderr: String
}

final class BackendProcess: @unchecked Sendable {
    typealias EventHandler = @MainActor @Sendable (BackendEvent) -> Void
    typealias CompletionHandler = @MainActor @Sendable (BackendCompletion) -> Void

    private static let session = BackendSession()
    private let lock = NSLock()
    private var requestID: String?
    private var cancelRequested = false

    static func executableURL(fileManager: FileManager = .default, bundle: Bundle = .main) throws -> URL {
        var candidates: [URL] = []
        if let override = ProcessInfo.processInfo.environment["TIMELAPSE_BACKEND_PATH"], !override.isEmpty {
            candidates.append(URL(fileURLWithPath: override))
        }
        candidates.append(
            bundle.bundleURL
                .appendingPathComponent("Contents/Helpers/TimeLapseBackend.app/Contents/MacOS", isDirectory: true)
                .appendingPathComponent("timelapse-backend")
        )
        if let resources = bundle.resourceURL {
            candidates.append(resources.appendingPathComponent("timelapse-backend"))
        }
        if let match = candidates.first(where: { fileManager.isExecutableFile(atPath: $0.path) }) {
            return match
        }
        throw BackendProcessError.executableNotFound(candidates)
    }

    func start<Request: Encodable & Sendable>(
        request: Request,
        onEvent: @escaping EventHandler,
        onCompletion: @escaping CompletionHandler
    ) throws {
        let requestData: Data
        do {
            requestData = try JSONEncoder().encode(request)
        } catch {
            throw BackendProcessError.couldNotEncodeRequest(error)
        }
        let identifier: String
        do {
            guard
                let payload = try JSONSerialization.jsonObject(with: requestData) as? [String: Any],
                let value = payload["id"] as? String,
                !value.isEmpty
            else {
                throw CocoaError(.propertyListReadCorrupt)
            }
            identifier = value
        } catch {
            throw BackendProcessError.couldNotEncodeRequest(error)
        }
        lock.withLock {
            requestID = identifier
            cancelRequested = false
        }
        try Self.session.send(
            requestData,
            requestID: identifier,
            onEvent: onEvent
        ) { [weak self] completion in
            let wasCancelled = self?.lock.withLock { self?.cancelRequested ?? false } ?? false
            self?.lock.withLock { self?.requestID = nil }
            onCompletion(
                BackendCompletion(
                    exitCode: completion.exitCode,
                    wasCancelled: wasCancelled || completion.wasCancelled,
                    stderr: completion.stderr
                )
            )
        }
    }

    func cancel() {
        let identifier = lock.withLock { () -> String? in
            cancelRequested = true
            return requestID
        }
        guard let identifier else { return }
        Self.session.cancel(requestID: identifier)
    }

    static func shutdown(completion: @escaping @MainActor @Sendable () -> Void) {
        session.shutdown(completion: completion)
    }
}

private final class BackendSession: @unchecked Sendable {
    private struct Pending: Sendable {
        let onEvent: BackendProcess.EventHandler
        let onCompletion: BackendProcess.CompletionHandler
    }

    private let stateLock = NSLock()
    private let writeLock = NSLock()
    private var process: Process?
    private var input: FileHandle?
    private var pending: [String: Pending] = [:]
    private var stderr = ""
    private var lastStartedAt = Date.distantPast
    private var recentCrashCount = 0
    private var hydrationRequests: [String: Data] = [:]
    private var shuttingDown = false
    private var handshakeWaiters: [String: DispatchSemaphore] = [:]
    private var failedHandshakes: Set<String> = []

    func send(
        _ requestData: Data,
        requestID: String,
        onEvent: @escaping BackendProcess.EventHandler,
        onCompletion: @escaping BackendProcess.CompletionHandler
    ) throws {
        try ensureStarted()
        if
            let payload = try? JSONSerialization.jsonObject(with: requestData) as? [String: Any],
            payload["command"] as? String == "hydrate_credentials",
            let profileID = payload["profile_id"] as? String
        {
            stateLock.withLock { hydrationRequests[profileID] = requestData }
        }
        let entry = Pending(onEvent: onEvent, onCompletion: onCompletion)
        let duplicate = stateLock.withLock { () -> Bool in
            if pending[requestID] != nil { return true }
            pending[requestID] = entry
            return false
        }
        if duplicate {
            let message = "The backend request ID is already active: \(requestID)"
            throw BackendProcessError.couldNotWriteRequest(CocoaError(.fileWriteFileExists, userInfo: [NSLocalizedDescriptionKey: message]))
        }
        do {
            try write(requestData + Data([0x0A]))
        } catch {
            _ = stateLock.withLock { pending.removeValue(forKey: requestID) }
            throw BackendProcessError.couldNotWriteRequest(error)
        }
    }

    func cancel(requestID: String) {
        let cancelID = "cancel-\(UUID().uuidString)"
        let payload: [String: String] = ["id": cancelID, "command": "cancel", "target_id": requestID]
        guard let data = try? JSONSerialization.data(withJSONObject: payload) else { return }
        try? write(data + Data([0x0A]))
    }

    func shutdown(completion: @escaping @MainActor @Sendable () -> Void) {
        stateLock.withLock { shuttingDown = true }
        let shutdownID = "shutdown-\(UUID().uuidString)"
        let payload: [String: String] = ["id": shutdownID, "command": "shutdown"]
        guard let data = try? JSONSerialization.data(withJSONObject: payload) else {
            DispatchQueue.main.async { completion() }
            return
        }
        do {
            try send(
                data,
                requestID: shutdownID,
                onEvent: { _ in },
                onCompletion: { _ in completion() }
            )
        } catch {
            DispatchQueue.main.async { completion() }
        }
    }

    private func ensureStarted() throws {
        if stateLock.withLock({ process?.isRunning == true }) {
            stateLock.withLock {
                if Date().timeIntervalSince(lastStartedAt) >= 300 { recentCrashCount = 0 }
            }
            return
        }
        let executableURL = try BackendProcess.executableURL()
        let launchedProcess = Process()
        let input = Pipe()
        let output = Pipe()
        let errorOutput = Pipe()
        launchedProcess.executableURL = executableURL
        launchedProcess.standardInput = input
        launchedProcess.standardOutput = output
        launchedProcess.standardError = errorOutput
        do {
            try launchedProcess.run()
        } catch {
            throw BackendProcessError.couldNotLaunch(error)
        }
        stateLock.withLock {
            self.process = launchedProcess
            self.input = input.fileHandleForWriting
            stderr = ""
            lastStartedAt = Date()
            shuttingDown = false
        }
        startReaders(process: launchedProcess, output: output, errorOutput: errorOutput)
        let handshakeID = "handshake-\(UUID().uuidString)"
        let handshakeWaiter = DispatchSemaphore(value: 0)
        stateLock.withLock { handshakeWaiters[handshakeID] = handshakeWaiter }
        var handshake: [String: Any] = [
            "id": handshakeID,
            "command": "handshake",
            "protocol_version": 2,
        ]
        if stateLock.withLock({ recentCrashCount >= 2 }) {
            handshake["recovery_mode"] = "quiescent"
        }
        do {
            try write(try JSONSerialization.data(withJSONObject: handshake) + Data([0x0A]))
        } catch {
            _ = stateLock.withLock { handshakeWaiters.removeValue(forKey: handshakeID) }
            launchedProcess.terminate()
            throw BackendProcessError.couldNotWriteRequest(error)
        }
        guard handshakeWaiter.wait(timeout: .now() + 10) == .success else {
            _ = stateLock.withLock { handshakeWaiters.removeValue(forKey: handshakeID) }
            launchedProcess.terminate()
            throw BackendProcessError.couldNotWriteRequest(
                CocoaError(.fileWriteUnknown, userInfo: [NSLocalizedDescriptionKey: "Backend handshake timed out."])
            )
        }
        if stateLock.withLock({ failedHandshakes.remove(handshakeID) != nil }) {
            launchedProcess.terminate()
            throw BackendProcessError.couldNotWriteRequest(CocoaError(.fileWriteUnknown))
        }
    }

    private func write(_ data: Data) throws {
        try writeLock.withLock {
            guard let input = stateLock.withLock({ input }) else {
                throw CocoaError(.fileNoSuchFile)
            }
            try input.write(contentsOf: data)
        }
    }

    private func startReaders(process: Process, output: Pipe, errorOutput: Pipe) {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.readEvents(from: output.fileHandleForReading)
        }
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let data = errorOutput.fileHandleForReading.readDataToEndOfFile()
            let text = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            self?.stateLock.withLock { self?.stderr = text }
        }
        DispatchQueue.global(qos: .utility).async { [weak self] in
            process.waitUntilExit()
            self?.processExited(process.terminationStatus)
        }
    }

    private func readEvents(from handle: FileHandle) {
        var buffer = Data()
        while true {
            let data = handle.availableData
            if data.isEmpty { break }
            buffer.append(data)
            while let newline = buffer.firstIndex(of: 0x0A) {
                let line = buffer[..<newline]
                buffer.removeSubrange(...newline)
                deliver(Data(line))
            }
        }
        if !buffer.isEmpty { deliver(buffer) }
    }

    private func deliver(_ data: Data) {
        guard !data.isEmpty, let event = try? JSONDecoder().decode(BackendEvent.self, from: data) else { return }
        guard let identifier = event.id else { return }
        if let waiter = stateLock.withLock({ () -> DispatchSemaphore? in
            guard ["complete", "cancelled", "error"].contains(event.event) else { return nil }
            if event.event == "error" { failedHandshakes.insert(identifier) }
            return handshakeWaiters.removeValue(forKey: identifier)
        }) {
            waiter.signal()
            return
        }
        let entry = stateLock.withLock { pending[identifier] }
        guard let entry else { return }
        DispatchQueue.main.async { entry.onEvent(event) }
        guard ["complete", "cancelled", "error"].contains(event.event) else { return }
        _ = stateLock.withLock { pending.removeValue(forKey: identifier) }
        let completion = BackendCompletion(
            exitCode: event.event == "error" ? 1 : 0,
            wasCancelled: event.event == "cancelled",
            stderr: stateLock.withLock { stderr }
        )
        DispatchQueue.main.async { entry.onCompletion(completion) }
    }

    private func processExited(_ status: Int32) {
        let recovery = stateLock.withLock { () -> (entries: [Pending], waiters: [DispatchSemaphore], shouldRestart: Bool) in
            process = nil
            input = nil
            if Date().timeIntervalSince(lastStartedAt) < 300 {
                recentCrashCount += 1
            } else {
                recentCrashCount = 0
            }
            let values = Array(pending.values)
            pending.removeAll()
            let waiters = Array(handshakeWaiters.values)
            handshakeWaiters.removeAll()
            return (values, waiters, !shuttingDown && recentCrashCount <= 2 && !hydrationRequests.isEmpty)
        }
        let completion = BackendCompletion(
            exitCode: status == 0 ? 1 : status,
            wasCancelled: false,
            stderr: stateLock.withLock { stderr }
        )
        for entry in recovery.entries {
            DispatchQueue.main.async { entry.onCompletion(completion) }
        }
        for waiter in recovery.waiters { waiter.signal() }
        if recovery.shouldRestart {
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 1) { [weak self] in
                self?.restartAfterCrash()
            }
        }
    }

    private func restartAfterCrash() {
        do {
            try ensureStarted()
            let requests = stateLock.withLock { recentCrashCount < 2 ? Array(hydrationRequests.values) : [] }
            for request in requests { try write(request + Data([0x0A])) }
        } catch {
            // The next explicit request reports an unavailable supervisor.
        }
    }
}
