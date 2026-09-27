import Foundation
import Testing

@testable import ScreenAgentCore

@Suite("DTO decoding (contract fixtures shared with the backend)")
struct DTOTests {
    private func samples() throws -> [String: Any] {
        try #require(
            JSONSerialization.jsonObject(with: Fixtures.contract("rest_samples.json"))
                as? [String: Any])
    }

    private func decode<T: Decodable>(_ type: T.Type, key: String) throws -> T {
        let object = try #require(try samples()[key])
        let data = try JSONSerialization.data(withJSONObject: object)
        return try JSONCoding.makeDecoder().decode(T.self, from: data)
    }

    @Test func decodesActiveSession() throws {
        let session = try decode(Session.self, key: "session")
        #expect(session.id == "8a4c7a2e-1f3b-4c1e-9d0a-5b6f7e8d9c01")
        #expect(session.isActive)
        #expect(session.endedAt == nil)
        #expect(session.startedAt == JSONCoding.parseDate("2026-09-27T18:30:00Z"))
    }

    @Test func decodesEndedSessionWithFractionalSeconds() throws {
        let session = try decode(Session.self, key: "session_ended")
        #expect(!session.isActive)
        let ended = try #require(session.endedAt)
        #expect(abs(ended.timeIntervalSince(session.startedAt) - 1.25) < 0.001)
    }

    @Test func decodesSessionDetailWithMixedTurns() throws {
        let detail = try decode(SessionDetail.self, key: "session_detail")
        #expect(detail.turns.map(\.source) == [.text, .voice])
        #expect(detail.turns.map(\.text) == ["open safari", "open the browser"])
        #expect(detail.turns.allSatisfy { $0.sessionID == detail.id })
    }

    @Test func decodesTextTurn() throws {
        let turn = try decode(Turn.self, key: "text_turn")
        #expect(turn.source == .text)
        #expect(turn.role == "user")
        #expect(turn.startedAt == turn.createdAt)
    }

    @Test(arguments: [
        "2026-09-27T18:30:00Z",
        "2026-09-27T18:30:00.5Z",
        "2026-09-27T18:30:00.250000Z",
        "2026-09-27T18:30:00+00:00",
    ])
    func parsesBackendDateFormats(_ raw: String) {
        #expect(JSONCoding.parseDate(raw) != nil)
    }

    @Test(arguments: ["", "yesterday", "2026-09-27"])
    func rejectsInvalidDates(_ raw: String) {
        #expect(JSONCoding.parseDate(raw) == nil)
    }

    @Test func unknownTurnSourceFailsToDecode() {
        let json = Data(
            #"{"id":"t","session_id":"s","role":"user","source":"telepathy","text":"x","started_at":null,"ended_at":null,"created_at":"2026-09-27T18:30:00Z"}"#
                .utf8)
        #expect(throws: DecodingError.self) {
            try JSONCoding.makeDecoder().decode(Turn.self, from: json)
        }
    }
}
