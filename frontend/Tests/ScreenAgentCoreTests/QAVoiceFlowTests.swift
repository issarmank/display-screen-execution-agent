import Foundation
import Testing

@testable import ScreenAgentCore

/// Independent QA pass over the voice path (commit af07a7c). These tests target gaps not
/// covered by ConversationViewModelVoiceTests.swift: a stale stop-timeout task from an earlier
/// voice run leaking into a later one, cancellation strictly during the `ensureSession()` await
/// (before the socket is ever opened), a fire-and-forget rapid double-tap matching the real
/// `InputBar` usage (`Task { await model.toggleMic() }`, not a sequential `await await`), and
/// two characterization tests that reproduce real gaps found during review without modifying
/// application code (see the QA report for severity and suggested fixes).
@MainActor
@Suite("QA: voice flow state machine edge cases")
struct QAVoiceFlowTests {
    let api = MockSessionAPI()
    let voice = MockVoiceStream()

    func makeVM(audio: MockAudioCapture = MockAudioCapture(), stopTimeout: Duration = .seconds(30))
        -> ConversationViewModel
    {
        ConversationViewModel(api: api, voice: voice, audio: audio, stopTimeout: stopTimeout)
    }

    func voiceTurn(_ id: String, _ text: String) -> Turn {
        Fixtures.turn(id: id, sessionID: "s1", source: .voice, text: text)
    }

    /// Regression-shaped test for the checklist item "stale stop-timeout tasks affecting a
    /// later voice run". Run 1 stops cleanly (server answers `done` well before its own
    /// `stopTimeout`), so `requestServerStop()`'s safety-net Task is still pending when run 2
    /// starts. The stale timer must not fire against run 2's `voiceTask` when it eventually
    /// wakes up: `requestServerStop()` in ConversationViewModel.swift:192-202 guards on
    /// `self.voiceTask == pending`, and by the time the stale timer fires, `self.voiceTask`
    /// refers to run 2's Task (a different instance from run 1's `pending`), so the guard
    /// correctly no-ops. This test proves that end to end rather than by inspection.
    @Test func staleStopTimeoutFromAnEarlierRunDoesNotDisruptALaterRun() async {
        let vm = makeVM(stopTimeout: .milliseconds(50))
        await vm.onAppear()

        // Run 1: clean stop, well inside the timeout window.
        await vm.toggleMic()
        voice.emit(.ready)
        await eventually { vm.voiceState == .recording }
        await vm.toggleMic()
        voice.emit(.done(turnCount: 0))
        voice.close()
        await vm.voiceTask?.value
        #expect(vm.voiceState == .idle)
        #expect(voice.cancelCount == 0)  // ended cleanly, not via the safety net

        // Run 2 starts immediately, while run 1's stale timer (~50 ms out) is still pending.
        await vm.toggleMic()
        voice.emit(.ready)
        await eventually { vm.voiceState == .recording }

        // Let run 1's stale timer fire while run 2 is genuinely still recording.
        try? await Task.sleep(for: .milliseconds(150))
        #expect(vm.voiceState == .recording)  // not knocked back to idle by run 1's timer
        #expect(vm.bannerError == nil)  // no stray "didn't finish in time" from run 1
        #expect(voice.cancelCount == 0)  // run 1's stale timer must not have cancelled anything

        // Run 2 still finishes normally afterwards.
        voice.emit(.turn(voiceTurn("v1", "second run works")))
        await eventually { vm.turns.map(\.text) == ["second run works"] }
        await vm.toggleMic()
        voice.emit(.done(turnCount: 1))
        voice.close()
        await vm.voiceTask?.value
        #expect(vm.voiceState == .idle)
        #expect(vm.bannerError == nil)
    }

    /// Cancellation while `startVoice()` is suspended inside `ensureSession()` (i.e. before
    /// `voice.connect` is ever called) must prevent the socket from opening at all, not just
    /// reset the UI. `ConversationViewModelVoiceTests.tappingWhileConnectingCancels` cancels
    /// *after* `connect()` already ran (both `toggleMic()` calls are fully sequential `await`s,
    /// so the first completes the whole `startVoice()` body, socket included, before the
    /// second runs); this test isolates the earlier window using a session API that blocks
    /// until released, exercising the guard at ConversationViewModel.swift:114 & :118.
    @Test func cancelDuringSessionCreationPreventsConnectingTheSocket() async {
        let gate = QAGate()
        let audio = MockAudioCapture()
        let vm = ConversationViewModel(
            api: QAGatedCreateSessionAPI(gate: gate), voice: voice, audio: audio)

        let micTask = Task { await vm.toggleMic() }
        await gate.waitUntilCalled()
        #expect(vm.voiceState == .connecting)

        await vm.toggleMic()  // cancel while still awaiting session creation
        await gate.proceed()  // let createSession() finally resolve
        await micTask.value

        #expect(voice.connectedSessions.isEmpty)
        #expect(audio.startCount == 0)
        #expect(vm.voiceState == .idle)
        #expect(vm.bannerError == nil)
    }

    /// Real double-tap as the UI actually issues it: `InputBar`'s mic button fires an
    /// unawaited `Task { await model.toggleMic() }` per tap, so two fast taps genuinely
    /// overlap instead of running fully sequentially. Whatever the interleaving, the state
    /// machine must always settle back to `.idle` once any voice task finishes -- it must
    /// never leave the mic button permanently stuck.
    @Test func rapidUnawaitedDoubleTapNeverLeavesTheMicButtonStuck() async {
        let audio = MockAudioCapture()
        let vm = makeVM(audio: audio)
        await vm.onAppear()

        let tap1 = Task { await vm.toggleMic() }
        let tap2 = Task { await vm.toggleMic() }
        await tap1.value
        await tap2.value
        if let pending = vm.voiceTask { await pending.value }

        await eventually { vm.voiceState == .idle }
        #expect(vm.voiceState == .idle)
    }

    /// Regression (found by QA): cancelling while connecting left `voiceState == .connecting`,
    /// so a `ready` already read off the wire before `cancel()` took effect still started the
    /// mic. This double's `cancel()` doesn't end the stream synchronously (unlike
    /// `MockVoiceStream`), which is what exposes the race.
    @Test func readyArrivingAfterCancelDoesNotStartTheMic() async {
        let stream = QADelayedCancelVoiceStream()
        let audio = MockAudioCapture()
        let vm = ConversationViewModel(api: api, voice: stream, audio: audio)
        await vm.onAppear()

        await vm.toggleMic()  // connecting
        await vm.toggleMic()  // user cancels while still connecting
        #expect(stream.cancelCount == 1)
        #expect(vm.voiceState == .stopping)  // winding down, no longer accepting `ready`

        stream.emit(.ready)  // a message already in flight when cancel() was called
        stream.finish()  // the transport then tears down for real
        await vm.voiceTask?.value
        #expect(audio.startCount == 0)  // the mic never went live after the cancel
        #expect(vm.voiceState == .idle)
    }

    /// Regression (found by QA): a successful `send()` cleared an earlier error banner but a
    /// successful voice turn didn't, so a stale banner stayed on screen.
    @Test func successfulVoiceTurnClearsAStaleBanner() async {
        let audio = MockAudioCapture()
        let vm = makeVM(audio: audio)
        await vm.onAppear()
        vm.bannerError = "Couldn't reach the backend for an earlier send."

        await vm.toggleMic()
        voice.emit(.ready)
        await eventually { vm.voiceState == .recording }
        voice.emit(.turn(voiceTurn("v1", "hello there")))
        await eventually { vm.turns.map(\.text) == ["hello there"] }
        await vm.toggleMic()
        voice.emit(.done(turnCount: 1))
        voice.close()
        await vm.voiceTask?.value

        #expect(vm.voiceState == .idle)
        #expect(vm.turns.map(\.text) == ["hello there"])
        #expect(vm.bannerError == nil)
    }
}

/// A continuation-based rendezvous mirroring `QATextFlowTests.Gate`, kept private to this file
/// to avoid coupling across QA test files.
private actor QAGate {
    private var hasBeenCalled = false
    private var canProceed = false
    private var calledContinuation: CheckedContinuation<Void, Never>?
    private var proceedContinuation: CheckedContinuation<Void, Never>?

    func waitUntilCalled() async {
        if hasBeenCalled { return }
        await withCheckedContinuation { calledContinuation = $0 }
    }

    func signalCalled() {
        hasBeenCalled = true
        calledContinuation?.resume()
        calledContinuation = nil
    }

    func waitUntilProceed() async {
        if canProceed { return }
        await withCheckedContinuation { proceedContinuation = $0 }
    }

    func proceed() {
        canProceed = true
        proceedContinuation?.resume()
        proceedContinuation = nil
    }
}

/// A `SessionAPI` fake whose `createSession()` blocks (via `QAGate`) after being entered, so a
/// test can deterministically observe "startVoice() has reached ensureSession()'s network call"
/// and cancel from exactly that window.
private struct QAGatedCreateSessionAPI: SessionAPI {
    let gate: QAGate

    func createSession() async throws -> Session {
        await gate.signalCalled()
        await gate.waitUntilProceed()
        return Fixtures.session(id: "gated-session")
    }

    func getSession(id: String) async throws -> SessionDetail {
        throw APIError.http(status: 404, detail: "not mocked")
    }

    func addTextTurn(sessionID: String, text: String) async throws -> Turn {
        Fixtures.turn(id: "t1", sessionID: sessionID, source: .text, text: text)
    }

    func endSession(id: String) async throws -> Session {
        Session(id: id, status: "completed", startedAt: Fixtures.epoch, endedAt: Fixtures.epoch)
    }
}

/// A `VoiceStreaming` double whose `cancel()` counts the call but, unlike `MockVoiceStream`,
/// does NOT synchronously finish the event stream. This simulates a real `URLSessionWebSocketTask`
/// where a message already read off the wire can still be delivered to the consumer after a
/// local `cancel()` was requested but before the underlying connection has actually torn down.
final class QADelayedCancelVoiceStream: VoiceStreaming, @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: AsyncThrowingStream<VoiceEvent, any Error>.Continuation?
    private var _cancelCount = 0

    var cancelCount: Int { lock.withLock { _cancelCount } }

    func connect(sessionID: String) -> AsyncThrowingStream<VoiceEvent, any Error> {
        let (stream, continuation) = AsyncThrowingStream.makeStream(of: VoiceEvent.self)
        lock.withLock { self.continuation = continuation }
        return stream
    }

    func send(pcm: Data) async throws {}

    func stop() async {}

    func cancel() {
        lock.withLock { _cancelCount += 1 }
    }

    func emit(_ event: VoiceEvent) {
        _ = lock.withLock { continuation }?.yield(event)
    }

    func finish() {
        lock.withLock { continuation }?.finish()
    }
}
