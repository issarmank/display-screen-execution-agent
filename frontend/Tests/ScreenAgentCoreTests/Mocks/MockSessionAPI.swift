import Foundation

@testable import ScreenAgentCore

actor MockSessionAPI: SessionAPI {
    enum Call: Equatable {
        case create
        case get(String)
        case addText(String, String)
        case end(String)
    }

    private(set) var calls: [Call] = []
    var createResult: Result<Session, APIError> = .success(Fixtures.session(id: "s1"))
    var addTextError: APIError?
    var endError: APIError?
    private var nextTurn = 0

    func setCreateResult(_ result: Result<Session, APIError>) { createResult = result }
    func setAddTextError(_ error: APIError?) { addTextError = error }

    func createSession() async throws -> Session {
        calls.append(.create)
        return try createResult.get()
    }

    func getSession(id: String) async throws -> SessionDetail {
        calls.append(.get(id))
        throw APIError.http(status: 404, detail: "not mocked")
    }

    func addTextTurn(sessionID: String, text: String) async throws -> Turn {
        calls.append(.addText(sessionID, text))
        if let addTextError { throw addTextError }
        nextTurn += 1
        return Fixtures.turn(id: "t\(nextTurn)", sessionID: sessionID, source: .text, text: text)
    }

    func endSession(id: String) async throws -> Session {
        calls.append(.end(id))
        if let endError { throw endError }
        return Session(id: id, status: "completed", startedAt: Fixtures.epoch, endedAt: Fixtures.epoch)
    }
}

enum Fixtures {
    static let epoch = Date(timeIntervalSince1970: 1_790_000_000)

    static func session(id: String) -> Session {
        Session(id: id, status: "active", startedAt: epoch)
    }

    static func turn(id: String, sessionID: String, source: TurnSource, text: String) -> Turn {
        Turn(id: id, sessionID: sessionID, source: source, text: text, createdAt: epoch)
    }

    /// Loads a contract fixture written by backend/tests/test_api_contract.py.
    static func contract(_ name: String) throws -> Data {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()  // Mocks
            .deletingLastPathComponent()  // ScreenAgentCoreTests
            .appending(path: "Fixtures/\(name)")
        return try Data(contentsOf: url)
    }
}
