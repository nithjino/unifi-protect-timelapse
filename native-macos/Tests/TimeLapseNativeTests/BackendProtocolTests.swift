import Foundation
import XCTest
@testable import TimeLapseNative

final class BackendProtocolTests: XCTestCase {
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

    func testCameraEventDecodesOptionalCameraMetadata() throws {
        let data = Data(
            #"{"id":"list-1","event":"cameras","cameras":[{"id":"camera-1","name":"Front Door","state":null,"model":"G5"}]}"#.utf8
        )

        let event = try JSONDecoder().decode(BackendEvent.self, from: data)

        XCTAssertEqual(event.cameras, [CameraInfo(id: "camera-1", name: "Front Door", state: nil, model: "G5")])
    }
}
