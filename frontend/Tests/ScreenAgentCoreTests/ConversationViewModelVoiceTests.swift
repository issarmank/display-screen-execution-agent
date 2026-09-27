import Foundation
import Testing

@testable import ScreenAgentCore

/// The spoken-input UI flows, played end to end with a mocked socket and microphone
/// (XCUITest needs Xcode, which isn't available; see CLAUDE.md).
@MainActor
@Suite("ConversationViewModel – voice flow")
struct ConversationViewModelVoiceTests {
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

    @Test func tapMicPartialThenCommittedRowThenIdle() async {
        let audio = MockAudioCapture()
        let vm = makeVM(audio: audio)
        await vm.onAppear()

        await vm.toggleMic()
        #expect(vm.voiceState == .connecting)
        #expect(voice.connectedSessions == ["s1"])
        #expect(audio.startCount == 0)  // mic only starts once the server is ready

        voice.emit(.ready)
        await eventually { vm.voiceState == .recording }
        #expect(audio.startCount == 1)

        let chunk = Data(repeating: 7, count: 3_200)
        audio.emit(chunk)
        await eventually { voice.sent == [chunk] }

        voice.emit(.partial("open the"))
        await eventually { vm.partialText == "open the" }

        voice.emit(.turn(voiceTurn("v1", "open the browser")))
        await eventually { vm.turns.map(\.text) == ["open the browser"] }
        #expect(vm.partialText == "")
        #expect(vm.turns.first?.source == .voice)

        await vm.toggleMic()  // stop
        #expect(vm.voiceState == .stopping)
        #expect(!audio.isCapturing)
        #expect(voice.stopCount == 1)

        voice.emit(.turn(voiceTurn("v2", "and a trailing thought")))  // flushed by the server
        voice.emit(.done(turnCount: 2))
        voice.close()
        await vm.voiceTask?.value
        #expect(vm.voiceState == .idle)
        #expect(vm.turns.map(\.id) == ["v1", "v2"])
        #expect(vm.bannerError == nil)
    }

    @Test func typedAndSpokenTurnsShareTheConversation() async {
        let vm = makeVM()
        await vm.onAppear()
        vm.draft = "open safari"
        await vm.send()

        await vm.toggleMic()
        voice.emit(.ready)
        voice.emit(.turn(voiceTurn("v1", "search for cats")))
        await eventually { vm.turns.count == 2 }
        await vm.toggleMic()
        voice.emit(.done(turnCount: 1))
        voice.close()
        await vm.voiceTask?.value

        #expect(vm.turns.map(\.source) == [.text, .voice])
        #expect(Set(vm.turns.map(\.sessionID)) == ["s1"])
    }

    @Test func fatalVoiceErrorResetsToIdleAndKeepsTheUsefulMessage() async {
        let audio = MockAudioCapture()
        let vm = makeVM(audio: audio)
        await vm.onAppear()
        await vm.toggleMic()
        voice.emit(.ready)
        await eventually { vm.voiceState == .recording }

        voice.emit(.error(VoiceError(code: "auth_error", message: "invalid api key", fatal: true)))
        voice.close(throwing: VoiceStreamError.closed(code: 1011, reason: ""))
        await vm.voiceTask?.value

        #expect(vm.voiceState == .idle)
        #expect(vm.bannerError == "Voice transcription failed: invalid api key")
        #expect(!audio.isCapturing)
        #expect(vm.partialText == "")
    }

    @Test func nonFatalErrorDoesNotInterruptRecording() async {
        let vm = makeVM()
        await vm.onAppear()
        await vm.toggleMic()
        voice.emit(.ready)
        voice.emit(.error(VoiceError(code: "rate_limited", message: "slow down", fatal: false)))
        voice.emit(.unknown("future_event"))
        voice.emit(.partial("still here"))
        await eventually { vm.partialText == "still here" }
        #expect(vm.voiceState == .recording)
        #expect(vm.bannerError == nil)
    }

    @Test func micPermissionDeniedShowsBannerAndNeverConnects() async {
        let audio = MockAudioCapture(permission: false)
        let vm = makeVM(audio: audio)
        await vm.onAppear()
        await vm.toggleMic()
        #expect(vm.voiceState == .idle)
        #expect(vm.bannerError == ConversationViewModel.micDeniedMessage)
        #expect(voice.connectedSessions.isEmpty)
        #expect(audio.startCount == 0)
    }

    @Test func socketRejectedBeforeReadyShowsCloseReason() async {
        let vm = makeVM()
        await vm.onAppear()
        await vm.toggleMic()
        voice.close(throwing: VoiceStreamError.closed(code: 4409, reason: "Session is not active"))
        await vm.voiceTask?.value
        #expect(vm.voiceState == .idle)
        #expect(vm.bannerError?.contains("already ended") == true)
    }

    @Test func backendUnreachableForVoiceShowsBanner() async {
        let vm = makeVM()
        await vm.onAppear()
        await vm.toggleMic()
        voice.close(throwing: VoiceStreamError.connectionFailed("Could not connect to the server."))
        await vm.voiceTask?.value
        #expect(vm.voiceState == .idle)
        #expect(vm.bannerError?.contains("Can't reach the backend") == true)
    }

    @Test func tappingWhileConnectingCancels() async {
        let audio = MockAudioCapture()
        let vm = makeVM(audio: audio)
        await vm.onAppear()
        await vm.toggleMic()
        await vm.toggleMic()
        #expect(voice.cancelCount == 1)
        await vm.voiceTask?.value
        #expect(vm.voiceState == .idle)
        #expect(audio.startCount == 0)
        #expect(vm.bannerError == nil)
    }

    @Test func micFailingToStartCancelsTheStream() async {
        let audio = MockAudioCapture(startError: AudioCaptureError.noInputDevice)
        let vm = makeVM(audio: audio)
        await vm.onAppear()
        await vm.toggleMic()
        voice.emit(.ready)
        await vm.voiceTask?.value
        #expect(vm.voiceState == .idle)
        #expect(voice.cancelCount == 1)
        #expect(vm.bannerError == "No microphone is available.")
    }

    @Test func voiceWithoutSessionCreatesOneFirst() async {
        await api.setCreateResult(.failure(.transport("refused")))
        let vm = makeVM()
        await vm.toggleMic()  // backend down: no session, no socket
        #expect(vm.voiceState == .idle)
        #expect(voice.connectedSessions.isEmpty)

        await api.setCreateResult(.success(Fixtures.session(id: "s9")))
        await vm.toggleMic()
        #expect(voice.connectedSessions == ["s9"])
    }

    @Test func stopTimesOutIfServerNeverFinishes() async {
        let vm = makeVM(stopTimeout: .milliseconds(20))
        await vm.onAppear()
        await vm.toggleMic()
        voice.emit(.ready)
        await eventually { vm.voiceState == .recording }
        await vm.toggleMic()
        // No `done` ever arrives; the timeout cancels the socket.
        await vm.voiceTask?.value
        #expect(voice.cancelCount == 1)
        #expect(vm.voiceState == .idle)
        #expect(vm.bannerError?.contains("didn't finish") == true)
    }

    @Test func endingTheSessionWhileRecordingStopsVoiceFirst() async {
        let audio = MockAudioCapture()
        let vm = makeVM(audio: audio)
        await vm.onAppear()
        await vm.toggleMic()
        voice.emit(.ready)
        await eventually { vm.voiceState == .recording }
        await vm.endSession()
        await vm.voiceTask?.value
        #expect(voice.cancelCount == 1)
        #expect(!audio.isCapturing)
        #expect(vm.voiceState == .idle)
        #expect(await api.calls == [.create, .end("s1")])
    }

    @Test func audioEndingOnItsOwnStopsGracefullySoTheServerCommits() async {
        // Regression: when the audio source ended by itself the VM stayed "recording"
        // without sending stop, so ElevenLabs timed out and the utterance was lost.
        let audio = MockAudioCapture()
        let vm = makeVM(audio: audio)
        await vm.onAppear()
        await vm.toggleMic()
        voice.emit(.ready)
        await eventually { vm.voiceState == .recording }
        audio.emit(Data(repeating: 1, count: 3_200))
        audio.endOnItsOwn()

        await eventually { voice.stopCount == 1 }
        #expect(vm.voiceState == .stopping)
        voice.emit(.turn(voiceTurn("v1", "flushed on stop")))
        voice.emit(.done(turnCount: 1))
        voice.close()
        await vm.voiceTask?.value
        #expect(vm.voiceState == .idle)
        #expect(vm.turns.map(\.text) == ["flushed on stop"])
        #expect(voice.sent.count == 1)
        #expect(voice.stopCount == 1)  // user-initiated and automatic stop never both fire
    }

    @Test func userStopDoesNotAlsoTriggerAutomaticStop() async {
        let vm = makeVM()
        await vm.onAppear()
        await vm.toggleMic()
        voice.emit(.ready)
        await eventually { vm.voiceState == .recording }
        await vm.toggleMic()
        for _ in 0..<50 { await Task.yield() }
        #expect(voice.stopCount == 1)
    }
}
