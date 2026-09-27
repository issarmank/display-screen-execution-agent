import Foundation
import Testing

@testable import ScreenAgentCore

/// Independent QA pass over the text-only path (commit 28d094f). These tests target gaps
/// not covered by ConversationViewModelTextTests.swift / HTTPSessionAPITests.swift:
/// concurrent-call safety of `ensureSession()`, draft-preservation semantics during an
/// in-flight send, and header/body shape details of HTTPSessionAPI GET requests.
@MainActor
@Suite("QA: text flow concurrency and edge cases")
struct QATextFlowTests {
    let api = MockSessionAPI()

    /// Two concurrent `send()` calls (e.g. a double Send-click before the first request
    /// returns) must only perform one network call; the second must observe `isSending`
    /// and bail out. This exercises the guard-then-set pattern in
    /// ConversationViewModel.send() across the `await ensureSession()` suspension point.
    @Test func concurrentSendCallsOnlySendOneTurn() async {
        let vm = ConversationViewModel(
            api: api, voice: MockVoiceStream(), audio: MockAudioCapture())
        await vm.onAppear()
        vm.draft = "open safari"

        async let first: Void = vm.send()
        async let second: Void = vm.send()
        _ = await (first, second)

        let addTextCalls = await api.calls.filter {
            if case .addText = $0 { return true }
            return false
        }
        #expect(addTextCalls.count == 1)
        #expect(vm.turns.count == 1)
    }

    /// Regression (found by QA): `ensureSession()` had no in-flight guard, so `onAppear()`
    /// and a `send()` made before the first `createSession()` resolved each created a
    /// backend session, leaving one orphaned (possibly the one holding the user's turn).
    /// Concurrent callers now share the single in-flight creation.
    @Test func concurrentOnAppearAndSendDoNotDoubleCreateSession() async {
        let vm = ConversationViewModel(
            api: api, voice: MockVoiceStream(), audio: MockAudioCapture())
        vm.draft = "open safari"

        async let onAppearCall: Void = vm.onAppear()
        async let sendCall: Void = vm.send()
        _ = await (onAppearCall, sendCall)

        let createCalls = await api.calls.filter { $0 == .create }
        #expect(createCalls.count == 1, "ensureSession() is not guarded against concurrent callers")
    }

    /// If the user edits the draft to different text while a send is in flight, the
    /// in-flight text must not stomp the user's newer, unsent edit (only what was actually
    /// sent should be cleared). Uses a continuation-gated fake (rather than `Task.yield()`,
    /// which does not guarantee the child task reaches any particular point) so the ordering
    /// is deterministic: we only mutate `draft` once `addTextTurn` has actually been entered,
    /// proving `send()` already captured the original text.
    @Test func draftEditedDuringInFlightSendIsNotClobbered() async {
        let gate = Gate()
        let vm = ConversationViewModel(
            api: GatedSessionAPI(gate: gate, sessionID: "s1"), voice: MockVoiceStream(),
            audio: MockAudioCapture())
        await vm.onAppear()
        vm.draft = "first message"

        let sendTask = Task { await vm.send() }
        await gate.waitUntilCalled()
        vm.draft = "second message, still typing"
        await gate.proceed()
        await sendTask.value

        #expect(vm.turns.map(\.text) == ["first message"])
        #expect(vm.draft == "second message, still typing")
    }

    /// A GET request carries no body and therefore should not claim a JSON Content-Type;
    /// only the Accept header should be set. HTTPSessionAPITests.swift only asserts on
    /// POST request shapes, so this fills that gap.
    @Test func getSessionSendsNoContentTypeOrBody() async throws {
        final class Recorder: @unchecked Sendable {
            private let lock = NSLock()
            private var _requests: [(URLRequest, Data?)] = []
            func record(_ request: URLRequest, _ body: Data?) {
                lock.withLock { _requests.append((request, body)) }
            }
            var requests: [(URLRequest, Data?)] { lock.withLock { _requests } }
        }
        let recorder = Recorder()
        let detailJSON = Data(
            #"{"id":"s1","status":"active","started_at":"2026-09-27T18:30:00Z","ended_at":null,"turns":[]}"#
                .utf8)
        let (session, base) = StubURLProtocol.make { request, body in
            recorder.record(request, body)
            return (200, detailJSON)
        }
        _ = try await HTTPSessionAPI(baseURL: base, session: session).getSession(id: "s1")

        let (request, body) = try #require(recorder.requests.first)
        #expect(request.httpMethod == "GET")
        #expect(request.value(forHTTPHeaderField: "Content-Type") == nil)
        #expect(request.value(forHTTPHeaderField: "Accept") == "application/json")
        #expect(body == nil || body?.isEmpty == true)
    }

    /// `endSession()` is a no-op once a session was already ended (or none was ever
    /// created), and must not throw or crash the caller even though it swallows API errors.
    @Test func endSessionBeforeAnySessionIsANoOp() async {
        let vm = ConversationViewModel(
            api: api, voice: MockVoiceStream(), audio: MockAudioCapture())
        await vm.endSession()
        #expect(await api.calls.isEmpty)
    }
}

/// A continuation-based rendezvous so a test can deterministically pause execution right
/// inside a fake network call and resume it later, instead of guessing timing with
/// `Task.yield()`.
private actor Gate {
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

/// A `SessionAPI` fake whose `addTextTurn` blocks (via `Gate`) after it has been entered,
/// so a test can observe "send() has captured its text and reached the network call" as a
/// discrete, awaitable event rather than an assumption about scheduling.
private struct GatedSessionAPI: SessionAPI {
    let gate: Gate
    let sessionID: String

    func createSession() async throws -> Session { Fixtures.session(id: sessionID) }

    func getSession(id: String) async throws -> SessionDetail {
        throw APIError.http(status: 404, detail: "not mocked")
    }

    func addTextTurn(sessionID: String, text: String) async throws -> Turn {
        await gate.signalCalled()
        await gate.waitUntilProceed()
        return Fixtures.turn(id: "t1", sessionID: sessionID, source: .text, text: text)
    }

    func endSession(id: String) async throws -> Session {
        Session(id: id, status: "completed", startedAt: Fixtures.epoch, endedAt: Fixtures.epoch)
    }
}
