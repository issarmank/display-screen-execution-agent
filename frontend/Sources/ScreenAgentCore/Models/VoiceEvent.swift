import Foundation

public struct VoiceError: Codable, Sendable, Equatable {
    public let code: String
    public let message: String
    public let fatal: Bool

    public init(code: String, message: String, fatal: Bool) {
        self.code = code
        self.message = message
        self.fatal = fatal
    }
}

/// A server -> client message on the voice WebSocket (see backend/app/api/voice.py).
public enum VoiceEvent: Sendable, Equatable {
    case ready
    case partial(String)
    case turn(Turn)
    case error(VoiceError)
    case done(turnCount: Int)
    /// A message type this client doesn't know yet; ignored rather than treated as a failure.
    case unknown(String)

    public static func decode(_ data: Data) throws -> VoiceEvent {
        try JSONCoding.makeDecoder().decode(VoiceEvent.self, from: data)
    }
}

extension VoiceEvent: Decodable {
    private enum CodingKeys: String, CodingKey {
        case type, text, turn, code, message, fatal
        case turnCount = "turn_count"
    }

    public init(from decoder: any Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let type = try container.decode(String.self, forKey: .type)
        switch type {
        case "ready":
            self = .ready
        case "partial":
            self = .partial(try container.decode(String.self, forKey: .text))
        case "turn":
            self = .turn(try container.decode(Turn.self, forKey: .turn))
        case "error":
            self = .error(try VoiceError(from: decoder))
        case "done":
            self = .done(turnCount: try container.decode(Int.self, forKey: .turnCount))
        default:
            self = .unknown(type)
        }
    }
}
