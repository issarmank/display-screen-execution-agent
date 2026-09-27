import Foundation

/// REST access to the backend's conversation sessions.
public protocol SessionAPI: Sendable {
    func createSession() async throws -> Session
    func getSession(id: String) async throws -> SessionDetail
    func addTextTurn(sessionID: String, text: String) async throws -> Turn
    func endSession(id: String) async throws -> Session
}

public enum APIError: Error, Equatable, LocalizedError {
    /// The server answered with a non-2xx status; `detail` is FastAPI's error detail.
    case http(status: Int, detail: String?)
    /// The server couldn't be reached (down, refused, timed out).
    case transport(String)
    /// The response wasn't the JSON we expected.
    case decoding(String)

    public var errorDescription: String? {
        switch self {
        case .http(let status, let detail):
            return detail.map { "Server error \(status): \($0)" } ?? "Server error \(status)"
        case .transport(let message):
            return "Can't reach the backend: \(message)"
        case .decoding(let message):
            return "Unexpected response from the backend: \(message)"
        }
    }
}
