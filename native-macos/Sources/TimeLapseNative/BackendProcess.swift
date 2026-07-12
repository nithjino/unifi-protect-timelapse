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

    private let lock = NSLock()
    private var process: Process?
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
            requestData = try JSONEncoder().encode(request) + Data([0x0A])
        } catch {
            throw BackendProcessError.couldNotEncodeRequest(error)
        }

        let executableURL = try Self.executableURL()
        let process = Process()
        let input = Pipe()
        let output = Pipe()
        let errorOutput = Pipe()
        process.executableURL = executableURL
        process.standardInput = input
        process.standardOutput = output
        process.standardError = errorOutput

        lock.withLock {
            cancelRequested = false
            self.process = process
        }
        do {
            try process.run()
        } catch {
            lock.withLock { self.process = nil }
            throw BackendProcessError.couldNotLaunch(error)
        }

        do {
            try input.fileHandleForWriting.write(contentsOf: requestData)
            try input.fileHandleForWriting.close()
        } catch {
            let writeError = error
            try? input.fileHandleForWriting.close()
            if process.isRunning {
                kill(process.processIdentifier, SIGKILL)
                process.waitUntilExit()
            }
            lock.withLock { self.process = nil }
            throw BackendProcessError.couldNotWriteRequest(writeError)
        }

        let group = DispatchGroup()
        let stderrBox = LockedBox("")

        group.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            defer { group.leave() }
            Self.readEvents(from: output.fileHandleForReading, onEvent: onEvent)
        }

        group.enter()
        DispatchQueue.global(qos: .utility).async {
            defer { group.leave() }
            let data = errorOutput.fileHandleForReading.readDataToEndOfFile()
            let text = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            stderrBox.set(text)
            if !text.isEmpty {
                let event = BackendEvent(
                    id: nil,
                    event: "log",
                    level: "ERROR",
                    message: text,
                    cameras: nil,
                    downloadedBytes: nil,
                    totalBytes: nil,
                    bytesPerSecond: nil,
                    elapsedSeconds: nil,
                    output: nil
                )
                DispatchQueue.main.async { onEvent(event) }
            }
        }

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            process.waitUntilExit()
            group.wait()
            let cancelled = self?.lock.withLock { self?.cancelRequested ?? false } ?? false
            self?.lock.withLock { self?.process = nil }
            let completion = BackendCompletion(
                exitCode: process.terminationStatus,
                wasCancelled: cancelled,
                stderr: stderrBox.get()
            )
            DispatchQueue.main.async { onCompletion(completion) }
        }
    }

    func cancel() {
        let runningProcess: Process? = lock.withLock {
            cancelRequested = true
            return process
        }
        guard let runningProcess, runningProcess.isRunning else { return }
        runningProcess.terminate()
        let processIdentifier = runningProcess.processIdentifier
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 3) {
            if runningProcess.isRunning {
                kill(processIdentifier, SIGKILL)
            }
        }
    }

    private static func readEvents(from handle: FileHandle, onEvent: @escaping EventHandler) {
        var buffer = Data()
        while true {
            let data = handle.availableData
            if data.isEmpty { break }
            buffer.append(data)
            while let newline = buffer.firstIndex(of: 0x0A) {
                let line = buffer[..<newline]
                buffer.removeSubrange(...newline)
                decodeAndDeliver(Data(line), onEvent: onEvent)
            }
        }
        if !buffer.isEmpty {
            decodeAndDeliver(buffer, onEvent: onEvent)
        }
    }

    private static func decodeAndDeliver(_ data: Data, onEvent: @escaping EventHandler) {
        guard !data.isEmpty else { return }
        do {
            let event = try JSONDecoder().decode(BackendEvent.self, from: data)
            DispatchQueue.main.async { onEvent(event) }
        } catch {
            let line = String(decoding: data, as: UTF8.self)
            let event = BackendEvent(
                id: nil,
                event: "log",
                level: "WARNING",
                message: "Ignored malformed backend output: \(line)",
                cameras: nil,
                downloadedBytes: nil,
                totalBytes: nil,
                bytesPerSecond: nil,
                elapsedSeconds: nil,
                output: nil
            )
            DispatchQueue.main.async { onEvent(event) }
        }
    }
}

private final class LockedBox<Value: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var value: Value

    init(_ value: Value) {
        self.value = value
    }

    func set(_ value: Value) {
        lock.withLock { self.value = value }
    }

    func get() -> Value {
        lock.withLock { value }
    }
}
