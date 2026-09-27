import Foundation
import Observation

/// All conversation logic; the views only bind to this.
@MainActor
@Observable
public final class ConversationViewModel {
    public private(set) var sessionID: String?
    public private(set) var turns: [Turn] = []
    public var draft = ""
    public private(set) var isSending = false
    public var bannerError: String?

    private let api: any SessionAPI
    private var sessionEnded = false

    public init(api: any SessionAPI) {
        self.api = api
    }

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

    /// Best-effort close of the session when the window closes or the app quits.
    public func endSession() async {
        guard let sessionID, !sessionEnded else { return }
        sessionEnded = true
        _ = try? await api.endSession(id: sessionID)
    }

    public func dismissBanner() {
        bannerError = nil
    }

    func append(_ turn: Turn) {
        guard !turns.contains(where: { $0.id == turn.id }) else { return }
        turns.append(turn)
    }

    private func ensureSession() async -> String? {
        if let sessionID { return sessionID }
        do {
            let session = try await api.createSession()
            sessionID = session.id
            sessionEnded = false
            bannerError = nil
            return session.id
        } catch {
            bannerError = Self.message(for: error)
            return nil
        }
    }

    static func message(for error: any Error) -> String {
        (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    }
}
