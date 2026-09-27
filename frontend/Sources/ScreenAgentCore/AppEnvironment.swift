import Foundation

/// Wires the live implementations used by the app.
@MainActor
public enum AppEnvironment {
    public static let defaultBackendURL = URL(string: "http://127.0.0.1:8000")!

    /// `SCREEN_AGENT_BACKEND_URL` overrides the backend location.
    public static func backendURL(
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> URL {
        guard let raw = environment["SCREEN_AGENT_BACKEND_URL"], let url = URL(string: raw),
            url.scheme == "http" || url.scheme == "https"
        else { return defaultBackendURL }
        return url
    }

    public static func makeViewModel() -> ConversationViewModel {
        let url = backendURL()
        return ConversationViewModel(
            api: HTTPSessionAPI(baseURL: url),
            voice: WebSocketVoiceStream(baseURL: url),
            audio: MicrophoneCapture())
    }
}
