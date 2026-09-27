import Foundation
import Testing

@testable import ScreenAgentCore

/// A voice socket the test drives by hand: `emit` server events, `close` the stream.
final class MockVoiceStream: VoiceStreaming, @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: AsyncThrowingStream<VoiceEvent, any Error>.Continuation?
    private var _connectedSessions: [String] = []
    private var _sent: [Data] = []
    private var _stopCount = 0
    private var _cancelCount = 0
    var sendError: (any Error)?

    var connectedSessions: [String] { lock.withLock { _connectedSessions } }
    var sent: [Data] { lock.withLock { _sent } }
    var stopCount: Int { lock.withLock { _stopCount } }
    var cancelCount: Int { lock.withLock { _cancelCount } }

    func connect(sessionID: String) -> AsyncThrowingStream<VoiceEvent, any Error> {
        let (stream, continuation) = AsyncThrowingStream.makeStream(of: VoiceEvent.self)
        lock.withLock {
            _connectedSessions.append(sessionID)
            self.continuation = continuation
        }
        return stream
    }

    func send(pcm: Data) async throws {
        if let sendError { throw sendError }
        lock.withLock { _sent.append(pcm) }
    }

    func stop() async {
        lock.withLock { _stopCount += 1 }
    }

    func cancel() {
        let continuation = lock.withLock { () -> AsyncThrowingStream<VoiceEvent, any Error>.Continuation? in
            _cancelCount += 1
            return self.continuation
        }
        continuation?.finish()  // like a real socket we closed ourselves
    }

    func emit(_ event: VoiceEvent) {
        _ = lock.withLock { continuation }?.yield(event)
    }

    func close(throwing error: (any Error)? = nil) {
        lock.withLock { continuation }?.finish(throwing: error)
    }
}

final class MockAudioCapture: AudioCapturing, @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: AsyncStream<Data>.Continuation?
    private var _startCount = 0
    private var _stopCount = 0
    let permission: Bool
    let startError: (any Error)?

    init(permission: Bool = true, startError: (any Error)? = nil) {
        self.permission = permission
        self.startError = startError
    }

    var startCount: Int { lock.withLock { _startCount } }
    var stopCount: Int { lock.withLock { _stopCount } }
    var isCapturing: Bool { lock.withLock { continuation != nil } }

    func requestPermission() async -> Bool { permission }

    func start() throws -> AsyncStream<Data> {
        if let startError { throw startError }
        let (stream, continuation) = AsyncStream.makeStream(of: Data.self)
        lock.withLock {
            _startCount += 1
            self.continuation = continuation
        }
        return stream
    }

    func stop() {
        let continuation = lock.withLock { () -> AsyncStream<Data>.Continuation? in
            _stopCount += 1
            defer { self.continuation = nil }
            return self.continuation
        }
        continuation?.finish()
    }

    func emit(_ chunk: Data) {
        _ = lock.withLock { continuation }?.yield(chunk)
    }

    /// The audio ends without `stop()` being called (e.g. the input device disappeared).
    func endOnItsOwn() {
        lock.withLock { () -> AsyncStream<Data>.Continuation? in
            defer { continuation = nil }
            return continuation
        }?.finish()
    }
}

/// Waits (by yielding, not sleeping for a fixed time) until `condition` holds.
@MainActor
func eventually(
    _ condition: @MainActor () -> Bool, sourceLocation: SourceLocation = #_sourceLocation
) async {
    for _ in 0..<2_000 {
        if condition() { return }
        await Task.yield()
        try? await Task.sleep(for: .milliseconds(1))
    }
    Issue.record("condition never became true", sourceLocation: sourceLocation)
}
