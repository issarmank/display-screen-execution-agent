import Foundation
import Testing

@testable import ScreenAgentCore

/// Opt-in tests against a running backend (the Swift counterpart of
/// `pytest -m integration`). Skipped unless SCREEN_AGENT_INTEGRATION_URL is set:
///   SCREEN_AGENT_INTEGRATION_URL=http://127.0.0.1:8000 swift test --filter LiveBackend
enum LiveBackend {
    static let url: URL? = ProcessInfo.processInfo.environment["SCREEN_AGENT_INTEGRATION_URL"]
        .flatMap(URL.init(string:))
}

@MainActor
@Suite("LiveBackend – text path", .enabled(if: LiveBackend.url != nil))
struct LiveBackendTextTests {
    @Test func typedTurnRoundTripsThroughRealBackend() async throws {
        let api = HTTPSessionAPI(baseURL: try #require(LiveBackend.url))
        let vm = ConversationViewModel(
            api: api, voice: MockVoiceStream(), audio: MockAudioCapture())
        await vm.onAppear()
        let sessionID = try #require(vm.sessionID)

        vm.draft = "open safari"
        await vm.send()
        #expect(vm.bannerError == nil)
        #expect(vm.turns.map(\.text) == ["open safari"])

        let detail = try await api.getSession(id: sessionID)
        #expect(detail.turns.map(\.source) == [.text])
        #expect(detail.turns.first?.id == vm.turns.first?.id)

        vm.draft = "   "
        await vm.send()  // blocked client-side, never reaches the server
        await #expect(throws: APIError.self) {
            try await api.addTextTurn(sessionID: sessionID, text: "   ")  // server says 422
        }

        await vm.endSession()
        #expect(try await api.getSession(id: sessionID).status == "completed")
        vm.draft = "too late"
        await vm.send()
        #expect(vm.bannerError?.contains("409") == true)
        #expect(vm.draft == "too late")
    }
}

@Suite("LiveBackend – voice socket", .enabled(if: LiveBackend.url != nil))
struct LiveBackendVoiceTests {
    @Test func unknownSessionEndsWith4404() async throws {
        let stream = WebSocketVoiceStream(baseURL: try #require(LiveBackend.url))
        var events: [VoiceEvent] = []
        await #expect(throws: VoiceStreamError.closed(code: 4404, reason: "Session not found")) {
            for try await event in stream.connect(sessionID: "does-not-exist") {
                events.append(event)
            }
        }
        #expect(events.isEmpty)
    }

    @Test func endedSessionEndsWith4409() async throws {
        let url = try #require(LiveBackend.url)
        let api = HTTPSessionAPI(baseURL: url)
        let session = try await api.createSession()
        _ = try await api.endSession(id: session.id)
        await #expect(throws: VoiceStreamError.self) {
            for try await _ in WebSocketVoiceStream(baseURL: url).connect(sessionID: session.id) {}
        }
    }

    /// Opens a real upstream ElevenLabs session briefly (no audio is sent).
    @Test func readyThenStopFinishesCleanlyWithDone() async throws {
        let url = try #require(LiveBackend.url)
        let api = HTTPSessionAPI(baseURL: url)
        let session = try await api.createSession()
        let stream = WebSocketVoiceStream(baseURL: url)
        var events: [VoiceEvent] = []
        for try await event in stream.connect(sessionID: session.id) {
            events.append(event)
            if event == .ready { await stream.stop() }
        }
        #expect(events == [.ready, .done(turnCount: 0)])
        _ = try await api.endSession(id: session.id)
    }
}

/// Streams a 16 kHz mono PCM16 WAV at real-time pace, standing in for the microphone.
final class WAVAudioCapture: AudioCapturing, @unchecked Sendable {
    private let pcm: Data
    private let lock = NSLock()
    private var pump: Task<Void, Never>?

    init(wavURL: URL) throws {
        let data = try Data(contentsOf: wavURL)
        // Find the "data" chunk rather than assuming a 44-byte header.
        guard let range = data.range(of: Data("data".utf8)) else { throw URLError(.cannotParseResponse) }
        let start = range.upperBound + 4
        // Trailing silence lets the server-side VAD commit the utterance.
        pcm = data[start...] + Data(count: 3_200 * 15)
    }

    func requestPermission() async -> Bool { true }

    func start() throws -> AsyncStream<Data> {
        let (stream, continuation) = AsyncStream.makeStream(of: Data.self)
        let pcm = self.pcm
        let task = Task {
            var offset = pcm.startIndex
            while offset < pcm.endIndex, !Task.isCancelled {
                let end = min(offset + 3_200, pcm.endIndex)
                continuation.yield(Data(pcm[offset..<end]))
                offset = end
                try? await Task.sleep(for: .milliseconds(100))
            }
            continuation.finish()
        }
        lock.withLock { pump = task }
        continuation.onTermination = { _ in task.cancel() }
        return stream
    }

    func stop() {
        lock.withLock { pump }?.cancel()
    }
}

/// Real backend + real ElevenLabs (spends a few seconds of Scribe credit). Needs
/// SCREEN_AGENT_INTEGRATION_URL and SCREEN_AGENT_SPEECH_WAV (16 kHz mono PCM16), e.g.
///   say -o /tmp/clip.wav --data-format=LEI16@16000 "Open Safari and search for cats."
@MainActor
@Suite(
    "LiveBackend – spoken turn",
    .enabled(
        if: LiveBackend.url != nil
            && ProcessInfo.processInfo.environment["SCREEN_AGENT_SPEECH_WAV"] != nil))
struct LiveBackendSpeechTests {
    @Test func spokenClipBecomesAVoiceTurnInTheSameSession() async throws {
        let url = try #require(LiveBackend.url)
        let wav = URL(
            fileURLWithPath: try #require(
                ProcessInfo.processInfo.environment["SCREEN_AGENT_SPEECH_WAV"]))
        let api = HTTPSessionAPI(baseURL: url)
        let vm = ConversationViewModel(
            api: api, voice: WebSocketVoiceStream(baseURL: url),
            audio: try WAVAudioCapture(wavURL: wav))
        await vm.onAppear()
        let sessionID = try #require(vm.sessionID)
        vm.draft = "open safari"
        await vm.send()

        // The clip ends by itself (like a mic being unplugged), so the app stops on its
        // own and the server flushes the utterance.
        await vm.toggleMic()
        var sawPartial = false
        for _ in 0..<300 where vm.voiceState != .idle {  // up to ~30 s
            sawPartial = sawPartial || !vm.partialText.isEmpty
            try await Task.sleep(for: .milliseconds(100))
        }

        #expect(vm.voiceState == .idle)
        #expect(vm.bannerError == nil)
        #expect(sawPartial)
        #expect(vm.turns.map(\.source) == [.text, .voice])
        #expect(vm.turns.last?.text.lowercased().contains("safari") == true)
        let detail = try await api.getSession(id: sessionID)
        #expect(detail.status == "active")
        #expect(detail.turns.map(\.id) == vm.turns.map(\.id))
        await vm.endSession()
    }
}

/// Real AVAudioEngine capture; needs mic permission for the process running `swift test`
/// (macOS attributes it to the terminal app). Enable with SCREEN_AGENT_MIC_TEST=1.
@Suite(
    "Live microphone",
    .enabled(if: ProcessInfo.processInfo.environment["SCREEN_AGENT_MIC_TEST"] == "1"))
struct LiveMicrophoneTests {
    @Test func capturesOneSecondAs3200ByteChunks() async throws {
        let mic = MicrophoneCapture()
        try #require(await mic.requestPermission(), "mic access denied for this terminal")
        let chunks = try mic.start()
        var received: [Data] = []
        for await chunk in chunks {
            received.append(chunk)
            if received.count == 10 { mic.stop() }  // ~1 s; stop() yields the tail then ends
        }
        #expect(received.count >= 10)
        #expect(received.prefix(10).allSatisfy { $0.count == 3_200 })
        #expect(received.allSatisfy { $0.count % 2 == 0 })
    }
}
