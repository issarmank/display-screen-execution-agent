import Foundation

/// One live voice stream to the backend at a time: `connect`, then `send` PCM chunks,
/// then `stop` (graceful: the server flushes and replies `done`) or `cancel` (abort).
public protocol VoiceStreaming: Sendable {
    /// Opens `/sessions/{id}/voice`. The stream finishes normally after a clean close
    /// (1000) and throws `VoiceStreamError` for any other ending.
    func connect(sessionID: String) -> AsyncThrowingStream<VoiceEvent, any Error>
    /// Sends 16 kHz mono PCM16 little-endian audio. Throws `VoiceStreamError.notConnected`
    /// once the socket is gone, so a sender loop can tell audio is no longer going anywhere.
    func send(pcm: Data) async throws
    /// Asks the server to flush and finish; events keep arriving until `done`. Best-effort:
    /// a no-op if the socket is already gone (the event stream reports how it ended).
    func stop() async
    /// Drops the connection immediately.
    func cancel()
}

public enum VoiceStreamError: Error, Equatable, LocalizedError {
    case closed(code: Int, reason: String)
    case connectionFailed(String)
    case notConnected

    public var errorDescription: String? {
        switch self {
        case .closed(4404, _):
            return "Voice failed: this conversation no longer exists on the backend."
        case .closed(4409, _):
            return "Voice failed: this conversation has already ended."
        case .closed(4429, _):
            return "Voice failed: another voice stream is already running for this conversation."
        case .closed(let code, let reason):
            return reason.isEmpty
                ? "Voice stream closed unexpectedly (code \(code))."
                : "Voice stream closed unexpectedly (code \(code)): \(reason)"
        case .connectionFailed(let message):
            return "Can't reach the backend for voice: \(message)"
        case .notConnected:
            return "The voice stream isn't connected."
        }
    }
}
