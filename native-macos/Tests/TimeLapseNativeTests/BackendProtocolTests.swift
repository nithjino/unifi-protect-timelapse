import Foundation
import XCTest
@testable import TimeLapseNative

final class BackendProtocolTests: XCTestCase {
    private struct HealthRequest: Encodable, Sendable {
        let id: String
        let command = "health"
    }

    @MainActor
    func testDownloadJobFormatsRequestedTimeRange() {
        let settings = BackendSettings(ConnectionSettings())
        let job = DownloadJob(
            groupNumber: 1,
            camera: CameraInfo(id: "camera-1", name: "Front Door", state: nil, model: nil),
            outputURL: URL(fileURLWithPath: "/tmp/timelapse.mp4"),
            requestSettings: settings,
            requestStart: "2026-07-11T08:00:00.000Z",
            requestEnd: "2026-07-11T09:00:00.000Z",
            requestSpeed: "600x"
        )

        XCTAssertTrue(job.timeRangeText.contains("→"))
        XCTAssertNotEqual(job.timeRangeText, "—")
    }

    func testOnlyFinishedDownloadStatesAreTerminal() {
        XCTAssertTrue(DownloadState.completed.isTerminal)
        XCTAssertTrue(DownloadState.cancelled.isTerminal)
        XCTAssertTrue(DownloadState.failed("network error").isTerminal)
        XCTAssertTrue(DownloadState.stopped.isTerminal)
        XCTAssertFalse(DownloadState.scheduled.isTerminal)
        XCTAssertFalse(DownloadState.preparing.isTerminal)
        XCTAssertFalse(DownloadState.downloading.isTerminal)
        XCTAssertFalse(DownloadState.cancelling.isTerminal)
    }

    func testProgressEventDecodesBackendFieldNames() throws {
        let data = Data(
            #"{"id":"download-1","event":"progress","downloaded_bytes":1024,"total_bytes":4096,"bytes_per_second":512.5,"elapsed_seconds":2.0}"#.utf8
        )

        let event = try JSONDecoder().decode(BackendEvent.self, from: data)

        XCTAssertEqual(event.id, "download-1")
        XCTAssertEqual(event.event, "progress")
        XCTAssertEqual(event.downloadedBytes, 1024)
        XCTAssertEqual(event.totalBytes, 4096)
        XCTAssertEqual(event.bytesPerSecond, 512.5)
        XCTAssertEqual(event.elapsedSeconds, 2.0)
    }

    @MainActor
    func testClaimedPathUpdatesTheJobAndRequestUsesSuffixPolicy() throws {
        let settings = BackendSettings(ConnectionSettings())
        let camera = CameraInfo(id: "camera-1", name: "Front", state: nil, model: nil)
        let request = DownloadRequest(
            id: "download-1", settings: settings, camera: camera,
            start: "2026-07-11T08:00:00Z", end: "2026-07-11T09:00:00Z", speed: "120x", output: "/tmp/front.mp4"
        )
        let encoded = try JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as? [String: Any]
        XCTAssertEqual(encoded?["collision_policy"] as? String, "suffix")
        let event = try JSONDecoder().decode(BackendEvent.self, from: Data(
            #"{"id":"download-1","event":"accepted","output":"/tmp/front_2.mp4"}"#.utf8
        ))
        XCTAssertEqual(event.event, "accepted")
        let job = DownloadJob(
            groupNumber: 1, camera: camera, outputURL: URL(fileURLWithPath: request.output),
            requestSettings: settings, requestStart: request.start, requestEnd: request.end, requestSpeed: request.speed
        )
        job.outputURL = URL(fileURLWithPath: try XCTUnwrap(event.output))
        XCTAssertEqual(job.outputURL.lastPathComponent, "front_2.mp4")
    }

    func testCameraEventDecodesOptionalCameraMetadata() throws {
        let data = Data(
            #"{"id":"list-1","event":"cameras","cameras":[{"id":"camera-1","name":"Front Door","state":null,"model":"G5"}]}"#.utf8
        )

        let event = try JSONDecoder().decode(BackendEvent.self, from: data)

        XCTAssertEqual(event.cameras, [CameraInfo(id: "camera-1", name: "Front Door", state: nil, model: "G5")])
    }

    func testThumbnailEventDecodesImageData() throws {
        let data = Data(
            #"{"id":"thumbnail-1","event":"thumbnail","thumbnail_base64":"anBlZw==","thumbnail_source":"live"}"#.utf8
        )

        let event = try JSONDecoder().decode(BackendEvent.self, from: data)

        XCTAssertEqual(event.thumbnailBase64, "anBlZw==")
        XCTAssertEqual(event.thumbnailSource, "live")
    }

    @MainActor
    func testSessionMultiplexesInterleavedFixtureRequests() async throws {
        guard ProcessInfo.processInfo.environment["TIMELAPSE_BACKEND_PATH"] != nil else { return }
        let first = BackendProcess()
        let second = BackendProcess()
        let completed = expectation(description: "Both requests complete")
        completed.expectedFulfillmentCount = 2
        try first.start(request: HealthRequest(id: "health-one"), onEvent: { _ in }) { completion in
            XCTAssertEqual(completion.exitCode, 0)
            completed.fulfill()
        }
        try second.start(request: HealthRequest(id: "health-two"), onEvent: { _ in }) { completion in
            XCTAssertEqual(completion.exitCode, 0)
            completed.fulfill()
        }
        await fulfillment(of: [completed], timeout: 10)

        let shutdown = expectation(description: "Supervisor shuts down")
        BackendProcess.shutdown { shutdown.fulfill() }
        await fulfillment(of: [shutdown], timeout: 10)
    }
}
