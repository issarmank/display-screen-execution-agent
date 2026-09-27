import Foundation
import Testing

@testable import ScreenAgentCore

@Suite("HTTPSessionAPI request shape and status handling")
struct HTTPSessionAPITests {
    private static let sessionJSON = Data(
        #"{"id":"s1","status":"active","started_at":"2026-09-27T18:30:00.123456Z","ended_at":null}"#
            .utf8)
    private static let turnJSON = Data(
        #"{"id":"t1","session_id":"s1","role":"user","source":"text","text":"open safari","started_at":"2026-09-27T18:30:01Z","ended_at":"2026-09-27T18:30:01Z","created_at":"2026-09-27T18:30:01Z"}"#
            .utf8)

    /// Records the requests seen by the stub.
    final class Recorder: @unchecked Sendable {
        private let lock = NSLock()
        private var _requests: [(URLRequest, Data?)] = []
        func record(_ request: URLRequest, _ body: Data?) {
            lock.withLock { _requests.append((request, body)) }
        }
        var requests: [(URLRequest, Data?)] { lock.withLock { _requests } }
    }

    private func api(status: Int = 200, body: Data, recorder: Recorder = Recorder())
        -> HTTPSessionAPI
    {
        let (session, base) = StubURLProtocol.make { request, requestBody in
            recorder.record(request, requestBody)
            return (status, body)
        }
        return HTTPSessionAPI(baseURL: base, session: session)
    }

    @Test func createSessionPostsToSessions() async throws {
        let recorder = Recorder()
        let session = try await api(status: 201, body: Self.sessionJSON, recorder: recorder)
            .createSession()
        #expect(session.id == "s1" && session.isActive)
        let (request, _) = try #require(recorder.requests.first)
        #expect(request.httpMethod == "POST")
        #expect(request.url?.path() == "/sessions")
    }

    @Test func addTextTurnSendsJSONBody() async throws {
        let recorder = Recorder()
        let turn = try await api(status: 201, body: Self.turnJSON, recorder: recorder)
            .addTextTurn(sessionID: "s1", text: "open safari")
        #expect(turn.source == .text && turn.text == "open safari")
        let (request, body) = try #require(recorder.requests.first)
        #expect(request.httpMethod == "POST")
        #expect(request.url?.path() == "/sessions/s1/turns")
        #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
        let bodyData = try #require(body)
        let json = try #require(
            try JSONSerialization.jsonObject(with: bodyData) as? [String: String])
        #expect(json == ["text": "open safari"])
    }

    @Test func getAndEndUseExpectedRoutes() async throws {
        let recorder = Recorder()
        let detailJSON = Data(
            #"{"id":"s1","status":"active","started_at":"2026-09-27T18:30:00Z","ended_at":null,"turns":[]}"#
                .utf8)
        let detail = try await api(body: detailJSON, recorder: recorder).getSession(id: "s1")
        #expect(detail.turns.isEmpty)
        _ = try await api(body: Self.sessionJSON, recorder: recorder).endSession(id: "s1")
        #expect(recorder.requests.map { $0.0.httpMethod } == ["GET", "POST"])
        #expect(recorder.requests.map { $0.0.url?.path() } == ["/sessions/s1", "/sessions/s1/end"])
    }

    @Test(arguments: [
        (404, #"{"detail":"Session nope not found"}"#, "Session nope not found"),
        (409, #"{"detail":"Session s1 is not active"}"#, "Session s1 is not active"),
        (422, #"{"detail":[{"msg":"String should have at least 1 character"}]}"#,
         "String should have at least 1 character"),
    ])
    func non2xxBecomesHTTPErrorWithDetail(status: Int, body: String, detail: String) async {
        await #expect(throws: APIError.http(status: status, detail: detail)) {
            try await api(status: status, body: Data(body.utf8)).addTextTurn(
                sessionID: "s1", text: "x")
        }
    }

    @Test func serverErrorWithoutJSONHasNoDetail() async {
        await #expect(throws: APIError.http(status: 500, detail: nil)) {
            try await api(status: 500, body: Data("Internal Server Error".utf8)).createSession()
        }
    }

    @Test func malformedSuccessBodyIsDecodingError() async {
        await #expect {
            try await api(status: 201, body: Data("{}".utf8)).createSession()
        } throws: { error in
            if case APIError.decoding = error { return true }
            return false
        }
    }

    @Test func unreachableServerIsTransportError() async {
        let (session, base) = StubURLProtocol.make { _, _ in throw URLError(.cannotConnectToHost) }
        await #expect {
            try await HTTPSessionAPI(baseURL: base, session: session).createSession()
        } throws: { error in
            if case APIError.transport = error { return true }
            return false
        }
    }
}
