import Foundation
import Testing

@testable import ScreenAgentCore

/// Extra coverage for `WebSocketVoiceStream` itself (commit af07a7c review). The existing
/// suites either exercise it through mocks (`ConversationViewModelVoiceTests`) or only check
/// its pure helpers (`WebSocketVoiceStreamTests` in VoiceEventTests.swift); neither one opens
/// a real socket. These tests do, against a real backend, and target the races and error
/// classification called out in review: connect() replacing an in-flight task, our-own-cancel
/// vs. server-close classification, malformed frames, reconnect after a failed connect, and
/// concurrent streams on the same/different sessions. No real ElevenLabs audio is sent (no
/// `send(pcm:)` call ever carries real speech), so these don't spend Scribe credit beyond the
/// same "connect, don't speak, stop" shape already used by `LiveBackendVoiceTests`.
///
/// Run with a backend up:
///   SCREEN_AGENT_INTEGRATION_URL=http://127.0.0.1:8791 swift test --filter WebSocketClientReviewTests
@Suite("WebSocketVoiceStream review (live backend)", .enabled(if: LiveBackend.url != nil))
struct WebSocketClientReviewTests {

    /// Polls (never sleeps for a fixed duration) until `condition` is true or times out.
    static func eventually(
        _ condition: @Sendable () -> Bool, sourceLocation: SourceLocation = #_sourceLocation
    ) async {
        for _ in 0..<3_000 {
            if condition() { return }
            try? await Task.sleep(for: .milliseconds(2))
        }
        Issue.record("condition never became true", sourceLocation: sourceLocation)
    }

    // MARK: connect() replacing an in-flight task

    /// Calling `connect()` a second time (a fresh `sessionID`) while the first stream's
    /// receiver loop hasn't unwound yet must not corrupt the second, current task: the old
    /// receiver's cleanup has to recognise it's stale (identity-checked in `clear(_:)`) and
    /// leave `self.task` alone. If that guard regressed, `send`/`stop` on the new stream would
    /// silently no-op (see `WebSocketVoiceStream.send`/`stop`, which swallow a nil task), and
    /// the second loop below would hang until the test times out instead of seeing `done`.
    @Test func connectReplacesInFlightTaskWithoutCorruptingTheNewOne() async throws {
        let url = try #require(LiveBackend.url)
        let api = HTTPSessionAPI(baseURL: url)
        let sessionA = try await api.createSession()
        let sessionB = try await api.createSession()
        let stream = WebSocketVoiceStream(baseURL: url)

        let eventsA = stream.connect(sessionID: sessionA.id)
        let box = ResultBox()
        let taskA = Task {
            var seen: [VoiceEvent] = []
            do {
                for try await event in eventsA { seen.append(event) }
                box.set(events: seen, error: nil)
            } catch {
                box.set(events: seen, error: error)
            }
        }

        // Replace A's task with B before A's receiver has necessarily finished unwinding.
        let eventsB = stream.connect(sessionID: sessionB.id)
        var seenB: [VoiceEvent] = []
        for try await event in eventsB {
            seenB.append(event)
            if event == .ready { await stream.stop() }
        }
        #expect(seenB == [.ready, .done(turnCount: 0)])

        await taskA.value
        // A's own connect() was pre-empted by our own `.cancel(with: .goingAway)`, which the
        // client classifies as a clean ending (nil error), matching "our own cancel" in the
        // endingError contract.
        #expect(box.error == nil)
    }

    // MARK: cancel() during an active stream

    /// `cancel()` while a stream is actively receiving must end the `AsyncThrowingStream`
    /// cleanly (no throw) -- it's "our own cancel", not a server- or network-initiated ending.
    @Test func cancelDuringActiveStreamEndsCleanly() async throws {
        let url = try #require(LiveBackend.url)
        let api = HTTPSessionAPI(baseURL: url)
        let session = try await api.createSession()
        let stream = WebSocketVoiceStream(baseURL: url)

        var events: [VoiceEvent] = []
        var thrown: (any Error)?
        do {
            for try await event in stream.connect(sessionID: session.id) {
                events.append(event)
                if event == .ready { stream.cancel() }
            }
        } catch {
            thrown = error
        }
        #expect(events == [.ready])
        #expect(thrown == nil)
    }

    // MARK: malformed frames don't kill the stream

    /// An odd-length binary frame is dropped server-side with a non-fatal `error`; the stream
    /// must keep running afterwards (round-trip a normal stop -> done).
    @Test func oddLengthFrameProducesNonFatalErrorAndStreamContinues() async throws {
        let url = try #require(LiveBackend.url)
        let api = HTTPSessionAPI(baseURL: url)
        let session = try await api.createSession()
        let stream = WebSocketVoiceStream(baseURL: url)

        var events: [VoiceEvent] = []
        for try await event in stream.connect(sessionID: session.id) {
            events.append(event)
            switch event {
            case .ready:
                try await stream.send(pcm: Data([0x01]))  // odd byte count
            case .error(let error):
                #expect(error.code == "bad_audio")
                #expect(error.fatal == false)
                await stream.stop()
            default:
                break
            }
        }
        #expect(events.count == 3)
        #expect(events.first == .ready)
        #expect(events.last == .done(turnCount: 0))
    }

    // MARK: reconnect after a failed connect

    /// A stream instance that failed to connect (unknown session -> 4404) must still be able
    /// to open a fresh, working connection afterwards -- the failure shouldn't leave internal
    /// state (the locked `task`) wedged.
    @Test func reconnectAfterAFailedConnectSucceeds() async throws {
        let url = try #require(LiveBackend.url)
        let api = HTTPSessionAPI(baseURL: url)
        let stream = WebSocketVoiceStream(baseURL: url)

        await #expect(throws: VoiceStreamError.closed(code: 4404, reason: "Session not found")) {
            for try await _ in stream.connect(sessionID: "does-not-exist-\(UUID().uuidString)") {}
        }

        let session = try await api.createSession()
        var events: [VoiceEvent] = []
        for try await event in stream.connect(sessionID: session.id) {
            events.append(event)
            if event == .ready { await stream.stop() }
        }
        #expect(events == [.ready, .done(turnCount: 0)])
    }

    // MARK: concurrent connections

    /// A second stream on the *same* session while one is already active is rejected with
    /// 4429, matching the backend's single-voice-stream-per-session registry.
    @Test func secondConcurrentStreamOnSameSessionGets4429() async throws {
        let url = try #require(LiveBackend.url)
        let api = HTTPSessionAPI(baseURL: url)
        let session = try await api.createSession()

        let streamA = WebSocketVoiceStream(baseURL: url)
        let box = ResultBox()
        let taskA = Task {
            var seen: [VoiceEvent] = []
            do {
                for try await event in streamA.connect(sessionID: session.id) {
                    seen.append(event)
                    box.set(events: seen, error: nil)  // published as each event arrives
                }
            } catch {
                box.set(events: seen, error: error)
            }
        }
        await Self.eventually { box.events.contains(.ready) }

        let streamB = WebSocketVoiceStream(baseURL: url)
        await #expect(throws: VoiceStreamError.closed(code: 4429, reason: "A voice stream is already active for this session")) {
            for try await _ in streamB.connect(sessionID: session.id) {}
        }

        streamA.cancel()
        await taskA.value
    }

    /// Two different sessions can stream concurrently without one interfering with the other
    /// (no accidental shared/global state across independent `WebSocketVoiceStream` instances).
    @Test func twoDifferentSessionsStreamConcurrently() async throws {
        let url = try #require(LiveBackend.url)
        let api = HTTPSessionAPI(baseURL: url)
        let sessionA = try await api.createSession()
        let sessionB = try await api.createSession()
        let streamA = WebSocketVoiceStream(baseURL: url)
        let streamB = WebSocketVoiceStream(baseURL: url)

        async let resultA: [VoiceEvent] = {
            var seen: [VoiceEvent] = []
            for try await event in streamA.connect(sessionID: sessionA.id) {
                seen.append(event)
                if event == .ready { await streamA.stop() }
            }
            return seen
        }()
        async let resultB: [VoiceEvent] = {
            var seen: [VoiceEvent] = []
            for try await event in streamB.connect(sessionID: sessionB.id) {
                seen.append(event)
                if event == .ready { await streamB.stop() }
            }
            return seen
        }()

        let (eventsA, eventsB) = try await (resultA, resultB)
        #expect(eventsA == [.ready, .done(turnCount: 0)])
        #expect(eventsB == [.ready, .done(turnCount: 0)])
    }

    // MARK: send after the socket is gone

    /// Regression (found in WebSocket review): `send` returned successfully once the task was
    /// gone despite being `throws`, hiding a dead socket from a sender loop. It now throws;
    /// `stop` stays a best-effort no-op.
    @Test func sendAfterCleanFinishThrowsNotConnected() async throws {
        let url = try #require(LiveBackend.url)
        let api = HTTPSessionAPI(baseURL: url)
        let session = try await api.createSession()
        let stream = WebSocketVoiceStream(baseURL: url)

        for try await event in stream.connect(sessionID: session.id) {
            if event == .ready { await stream.stop() }
        }
        // The AsyncThrowingStream above only finishes once `clear(_:)` has nulled the task
        // (it runs before the receiver task exits), so `self.task` is nil here.
        await #expect(throws: VoiceStreamError.notConnected) {
            try await stream.send(pcm: Data(repeating: 0, count: 10))
        }
        await stream.stop()  // best-effort: no-op
    }
}

/// Thread-safe box for a background Task's result, read by polling from the test body.
private final class ResultBox: @unchecked Sendable {
    private let lock = NSLock()
    private var _events: [VoiceEvent] = []
    private var _error: (any Error)?

    var events: [VoiceEvent] { lock.withLock { _events } }
    var error: (any Error)? { lock.withLock { _error } }

    func set(events: [VoiceEvent], error: (any Error)?) {
        lock.withLock {
            _events = events
            _error = error
        }
    }
}
