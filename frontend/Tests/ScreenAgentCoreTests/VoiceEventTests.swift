import Foundation
import Testing

@testable import ScreenAgentCore

@Suite("VoiceEvent decoding (contract fixture shared with the backend)")
struct VoiceEventTests {
    @Test func decodesEveryFixtureEvent() throws {
        let root = try #require(
            try JSONSerialization.jsonObject(with: Fixtures.contract("voice_events.json"))
                as? [String: Any])
        let raw = try #require(root["events"] as? [Any])
        let events = try raw.map {
            try VoiceEvent.decode(JSONSerialization.data(withJSONObject: $0))
        }
        #expect(events.count == 7)
        #expect(events[0] == .ready)
        #expect(events[1] == .partial("open the"))
        guard case .turn(let timed) = events[2], case .turn(let untimed) = events[3] else {
            Issue.record("expected two turn events, got \(events[2]) and \(events[3])")
            return
        }
        #expect(timed.source == .voice && timed.text == "open the browser")
        #expect(timed.startedAt != nil && timed.endedAt != nil)
        #expect(untimed.startedAt == nil && untimed.endedAt == nil)
        #expect(
            events[4]
                == .error(VoiceError(
                    code: "bad_json", message: "Control message is not valid JSON", fatal: false)))
        #expect(
            events[5]
                == .error(VoiceError(code: "auth_error", message: "invalid api key", fatal: true)))
        #expect(events[6] == .done(turnCount: 2))
    }

    @Test func unknownTypeDecodesAsUnknown() throws {
        let event = try VoiceEvent.decode(Data(#"{"type":"speaker_changed","speaker":2}"#.utf8))
        #expect(event == .unknown("speaker_changed"))
    }

    @Test(arguments: [
        #"{"no_type":true}"#,
        #"{"type":"partial"}"#,
        #"{"type":"done","turn_count":"two"}"#,
        #"not json"#,
    ])
    func malformedEventsThrow(_ json: String) {
        #expect(throws: (any Error).self) { try VoiceEvent.decode(Data(json.utf8)) }
    }
}

@Suite("WebSocketVoiceStream helpers")
struct WebSocketVoiceStreamTests {
    @Test(arguments: [
        ("http://127.0.0.1:8000", "ws://127.0.0.1:8000/sessions/s1/voice"),
        ("https://agent.example.com", "wss://agent.example.com/sessions/s1/voice"),
        ("http://localhost:8000/api/", "ws://localhost:8000/api/sessions/s1/voice"),
    ])
    func voiceURL(base: String, expected: String) {
        let url = WebSocketVoiceStream.voiceURL(baseURL: URL(string: base)!, sessionID: "s1")
        #expect(url.absoluteString == expected)
    }

    @Test(arguments: [
        (VoiceStreamError.closed(code: 4404, reason: ""), "no longer exists"),
        (.closed(code: 4409, reason: ""), "already ended"),
        (.closed(code: 4429, reason: ""), "already running"),
        (.closed(code: 1011, reason: "boom"), "code 1011): boom"),
        (.connectionFailed("refused"), "Can't reach the backend"),
        (.notConnected, "isn't connected"),
    ])
    func errorMessages(error: VoiceStreamError, fragment: String) {
        #expect(error.errorDescription?.contains(fragment) == true)
    }

    @Test func sendWithoutConnectionThrowsButStopAndCancelAreNoOps() async {
        let stream = WebSocketVoiceStream(baseURL: URL(string: "http://127.0.0.1:9")!)
        await #expect(throws: VoiceStreamError.notConnected) {
            try await stream.send(pcm: Data([1, 2]))
        }
        await stream.stop()
        stream.cancel()
    }
}
