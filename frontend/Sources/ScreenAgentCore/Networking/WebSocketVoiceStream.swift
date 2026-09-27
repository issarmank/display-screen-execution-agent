import Foundation

public final class WebSocketVoiceStream: VoiceStreaming, @unchecked Sendable {
    private let baseURL: URL
    private let session: URLSession
    private let lock = NSLock()
    private var task: URLSessionWebSocketTask?  // guarded by lock

    public init(baseURL: URL, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    /// `http(s)://host/base` -> `ws(s)://host/base/sessions/{id}/voice`.
    public static func voiceURL(baseURL: URL, sessionID: String) -> URL {
        var components = URLComponents(
            url: baseURL.appending(path: "sessions/\(sessionID)/voice"),
            resolvingAgainstBaseURL: false)!
        components.scheme = components.scheme == "https" ? "wss" : "ws"
        return components.url!
    }

    public func connect(sessionID: String) -> AsyncThrowingStream<VoiceEvent, any Error> {
        let task = session.webSocketTask(
            with: Self.voiceURL(baseURL: baseURL, sessionID: sessionID))
        let previous = lock.withLock { () -> URLSessionWebSocketTask? in
            defer { self.task = task }
            return self.task
        }
        previous?.cancel(with: .goingAway, reason: nil)  // outside the lock: may do I/O
        task.resume()

        return AsyncThrowingStream { continuation in
            let receiver = Task {
                do {
                    while true {
                        let message = try await task.receive()
                        guard case .string(let text) = message,
                            let event = try? VoiceEvent.decode(Data(text.utf8))
                        else { continue }  // the protocol only sends JSON text frames
                        continuation.yield(event)
                    }
                } catch {
                    continuation.finish(throwing: Self.endingError(for: task, error: error))
                }
                self.clear(task)
            }
            // Also runs after the receiver finishes the stream itself; cancelling an
            // already-closed task is a harmless no-op. It matters when the consumer stops
            // iterating early.
            continuation.onTermination = { _ in
                receiver.cancel()
                task.cancel(with: .goingAway, reason: nil)
            }
        }
    }

    public func send(pcm: Data) async throws {
        guard let task = lock.withLock({ self.task }) else {
            throw VoiceStreamError.notConnected
        }
        try await task.send(.data(pcm))
    }

    public func stop() async {
        guard let task = lock.withLock({ self.task }) else { return }
        try? await task.send(.string(#"{"type":"stop"}"#))
    }

    public func cancel() {
        let task = lock.withLock { () -> URLSessionWebSocketTask? in
            defer { self.task = nil }
            return self.task
        }
        task?.cancel(with: .normalClosure, reason: nil)
    }

    private func clear(_ finished: URLSessionWebSocketTask) {
        lock.withLock {
            if self.task === finished { self.task = nil }
        }
    }

    /// nil means a clean ending (server closed with 1000, or we cancelled it).
    static func endingError(for task: URLSessionWebSocketTask, error: any Error) -> (any Error)? {
        let code = task.closeCode
        if code == .normalClosure || code == .goingAway { return nil }
        if code != .invalid {
            let reason = task.closeReason.flatMap { String(data: $0, encoding: .utf8) } ?? ""
            return VoiceStreamError.closed(code: code.rawValue, reason: reason)
        }
        if (error as? URLError)?.code == .cancelled { return nil }
        return VoiceStreamError.connectionFailed(error.localizedDescription)
    }
}
