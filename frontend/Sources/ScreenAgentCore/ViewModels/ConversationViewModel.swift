import Foundation
import Observation

public enum VoiceState: Equatable, Sendable {
    case idle
    /// Mic permission check and WebSocket handshake; waiting for the server's `ready`.
    case connecting
    case recording
    /// `stop` sent; waiting for the server to flush the last segment and send `done`.
    case stopping
}

/// All conversation logic; the views only bind to this.
@MainActor
@Observable
public final class ConversationViewModel {
    public private(set) var sessionID: String?
    public private(set) var turns: [Turn] = []
    public var draft = ""
    public private(set) var isSending = false
    public var bannerError: String?
    public private(set) var voiceState: VoiceState = .idle
    /// The live, not-yet-committed transcript of what the user is saying.
    public private(set) var partialText = ""

    private let api: any SessionAPI
    private let voice: any VoiceStreaming
    private let audio: any AudioCapturing
    private var sessionEnded = false
    /// The in-flight `POST /sessions`, shared by concurrent callers (e.g. launch + an early send).
    private var sessionCreation: Task<String?, Never>?
    /// Consumes server events for the current voice stream; nil when idle.
    private(set) var voiceTask: Task<Void, Never>?
    /// Pumps mic chunks to the socket while recording.
    private var audioTask: Task<Void, Never>?
    private var voiceErrorShown = false
    private let stopTimeout: Duration

    /// - Parameter stopTimeout: how long to wait for the server's `done` after stopping
    ///   before dropping the connection.
    public init(
        api: any SessionAPI, voice: any VoiceStreaming, audio: any AudioCapturing,
        stopTimeout: Duration = .seconds(5)
    ) {
        self.api = api
        self.voice = voice
        self.audio = audio
        self.stopTimeout = stopTimeout
    }

    static let micDeniedMessage =
        "Microphone access is off. Allow Screen Agent in System Settings › Privacy & Security › Microphone."

    public var canSend: Bool {
        !isSending && !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// Opens the conversation session. Safe to call repeatedly.
    public func onAppear() async {
        _ = await ensureSession()
    }

    public func send() async {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isSending else { return }
        isSending = true
        defer { isSending = false }
        // A backend that was down at launch gets another chance to open the session here.
        guard let sessionID = await ensureSession() else { return }
        do {
            let turn = try await api.addTextTurn(sessionID: sessionID, text: text)
            append(turn)
            // Only clear what was sent, in case the user kept typing during the request.
            if draft.trimmingCharacters(in: .whitespacesAndNewlines) == text { draft = "" }
            bannerError = nil
        } catch {
            bannerError = Self.message(for: error)  // draft is kept so nothing is lost
        }
    }

    /// Mic button: start listening, stop (and let the server flush), or abort a pending connect.
    public func toggleMic() async {
        switch voiceState {
        case .idle: await startVoice()
        case .connecting: cancelVoice()
        case .recording: await stopVoice()
        case .stopping: break
        }
    }

    /// Best-effort close of the session when the window closes or the app quits.
    public func endSession() async {
        if voiceState != .idle { cancelVoice() }
        guard let sessionID, !sessionEnded else { return }
        sessionEnded = true
        _ = try? await api.endSession(id: sessionID)
    }

    public func dismissBanner() {
        bannerError = nil
    }

    // MARK: Voice

    private func startVoice() async {
        voiceState = .connecting
        voiceErrorShown = false
        guard await audio.requestPermission() else {
            voiceState = .idle
            bannerError = Self.micDeniedMessage
            return
        }
        guard voiceState == .connecting else { return }  // cancelled while prompting
        guard let sessionID = await ensureSession() else {
            voiceState = .idle
            return
        }
        guard voiceState == .connecting else { return }
        let events = voice.connect(sessionID: sessionID)
        voiceTask = Task { [weak self] in
            await self?.consume(events)
        }
    }

    private func consume(_ events: AsyncThrowingStream<VoiceEvent, any Error>) async {
        do {
            for try await event in events {
                handle(event)
            }
        } catch {
            showVoiceError(Self.message(for: error))
        }
        finishVoice()
    }

    func handle(_ event: VoiceEvent) {
        switch event {
        case .ready:
            guard voiceState == .connecting else { return }
            startAudio()
        case .partial(let text):
            partialText = text
        case .turn(let turn):
            append(turn)
            partialText = ""
            bannerError = nil  // like a successful send, clears an earlier failure
        case .error(let error) where error.fatal:
            showVoiceError("Voice transcription failed: \(error.message)")
        case .error, .done, .unknown:
            // Non-fatal errors (a dropped frame, rate limiting) don't interrupt the user;
            // `done` is followed by the server closing the socket, which ends `consume`.
            break
        }
    }

    private func startAudio() {
        let chunks: AsyncStream<Data>
        do {
            chunks = try audio.start()
        } catch {
            showVoiceError(Self.message(for: error))
            voice.cancel()
            return
        }
        voiceState = .recording
        audioTask = Task { [weak self, voice] in
            for await chunk in chunks {
                do {
                    try await voice.send(pcm: chunk)
                } catch {
                    return  // socket gone; `consume` reports why
                }
            }
            // The audio ended without the user stopping (e.g. the input device went away):
            // stop gracefully so the server commits what was said instead of timing out.
            await self?.audioEnded()
        }
    }

    private func audioEnded() async {
        guard voiceState == .recording else { return }
        voiceState = .stopping
        await requestServerStop()
    }

    private func stopVoice() async {
        voiceState = .stopping
        audio.stop()  // finishes the chunk stream after its last partial chunk
        await audioTask?.value  // make sure every chunk is sent before `stop`
        await requestServerStop()
    }

    private func requestServerStop() async {
        await voice.stop()
        // Safety net: never leave the mic button stuck if the server doesn't answer.
        let pending = voiceTask
        Task { [weak self, voice, stopTimeout] in
            try? await Task.sleep(for: stopTimeout)
            guard let self, self.voiceTask == pending, pending != nil else { return }
            self.showVoiceError("The backend didn't finish the voice stream in time.")
            voice.cancel()
        }
    }

    private func cancelVoice() {
        audio.stop()
        voice.cancel()
        // Still in the permission/session step: nothing to wait for. Otherwise wait for the
        // socket to wind down, but leave `.connecting` right away so a `ready` that was
        // already in flight can't start the mic after the user cancelled.
        voiceState = voiceTask == nil ? .idle : .stopping
    }

    private func finishVoice() {
        audio.stop()
        audioTask?.cancel()
        audioTask = nil
        voiceTask = nil
        partialText = ""
        voiceState = .idle
    }

    /// Shows the first error of a voice run; the close that follows a fatal error
    /// would otherwise replace the useful message with a bare close code.
    private func showVoiceError(_ message: String) {
        guard !voiceErrorShown else { return }
        voiceErrorShown = true
        bannerError = message
    }

    func append(_ turn: Turn) {
        guard !turns.contains(where: { $0.id == turn.id }) else { return }
        turns.append(turn)
    }

    private func ensureSession() async -> String? {
        if let sessionID { return sessionID }
        if let sessionCreation { return await sessionCreation.value }
        let creation = Task { [api] () -> String? in
            do {
                let session = try await api.createSession()
                self.sessionID = session.id
                self.sessionEnded = false
                self.bannerError = nil
                return session.id
            } catch {
                self.bannerError = Self.message(for: error)
                return nil
            }
        }
        sessionCreation = creation
        defer { sessionCreation = nil }
        return await creation.value
    }

    static func message(for error: any Error) -> String {
        (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    }
}
