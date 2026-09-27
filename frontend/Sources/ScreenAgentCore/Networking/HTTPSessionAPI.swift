import Foundation

public struct HTTPSessionAPI: SessionAPI {
    public let baseURL: URL
    private let session: URLSession

    public init(baseURL: URL, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.session = session
    }

    public func createSession() async throws -> Session {
        try await send("POST", "sessions")
    }

    public func getSession(id: String) async throws -> SessionDetail {
        try await send("GET", "sessions/\(id)")
    }

    public func addTextTurn(sessionID: String, text: String) async throws -> Turn {
        try await send("POST", "sessions/\(sessionID)/turns", body: TextTurnRequest(text: text))
    }

    public func endSession(id: String) async throws -> Session {
        try await send("POST", "sessions/\(id)/end")
    }

    private func send<Response: Decodable>(
        _ method: String, _ path: String, body: (some Encodable)? = String?.none
    ) async throws -> Response {
        var request = URLRequest(url: baseURL.appending(path: path))
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder().encode(body)
        }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw APIError.transport(error.localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else {
            throw APIError.decoding("not an HTTP response")
        }
        guard (200..<300).contains(http.statusCode) else {
            throw APIError.http(status: http.statusCode, detail: Self.detail(from: data))
        }
        do {
            return try JSONCoding.makeDecoder().decode(Response.self, from: data)
        } catch {
            throw APIError.decoding(String(describing: error))
        }
    }

    /// FastAPI sends `{"detail": "..."}` for raised HTTP errors and
    /// `{"detail": [{"msg": "..."}, ...]}` for validation (422) errors.
    static func detail(from data: Data) -> String? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        if let text = object["detail"] as? String { return text }
        if let items = object["detail"] as? [[String: Any]] {
            let messages = items.compactMap { $0["msg"] as? String }
            return messages.isEmpty ? nil : messages.joined(separator: "; ")
        }
        return nil
    }
}
