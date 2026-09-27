import Foundation

public enum TurnSource: String, Codable, Sendable, Equatable {
    case text
    case voice
}

public struct Turn: Codable, Sendable, Equatable, Identifiable {
    public let id: String
    public let sessionID: String
    public let role: String
    public let source: TurnSource
    public let text: String
    public let startedAt: Date?
    public let endedAt: Date?
    public let createdAt: Date

    public init(
        id: String,
        sessionID: String,
        role: String = "user",
        source: TurnSource,
        text: String,
        startedAt: Date? = nil,
        endedAt: Date? = nil,
        createdAt: Date
    ) {
        self.id = id
        self.sessionID = sessionID
        self.role = role
        self.source = source
        self.text = text
        self.startedAt = startedAt
        self.endedAt = endedAt
        self.createdAt = createdAt
    }

    enum CodingKeys: String, CodingKey {
        case id, role, source, text
        case sessionID = "session_id"
        case startedAt = "started_at"
        case endedAt = "ended_at"
        case createdAt = "created_at"
    }
}

public struct Session: Codable, Sendable, Equatable, Identifiable {
    public let id: String
    public let status: String
    public let startedAt: Date
    public let endedAt: Date?

    public init(id: String, status: String, startedAt: Date, endedAt: Date? = nil) {
        self.id = id
        self.status = status
        self.startedAt = startedAt
        self.endedAt = endedAt
    }

    public var isActive: Bool { status == "active" }

    enum CodingKeys: String, CodingKey {
        case id, status
        case startedAt = "started_at"
        case endedAt = "ended_at"
    }
}

public struct SessionDetail: Codable, Sendable, Equatable {
    public let id: String
    public let status: String
    public let startedAt: Date
    public let endedAt: Date?
    public let turns: [Turn]

    enum CodingKeys: String, CodingKey {
        case id, status, turns
        case startedAt = "started_at"
        case endedAt = "ended_at"
    }
}

struct TextTurnRequest: Encodable {
    let text: String
}

public enum JSONCoding {
    /// Decodes the backend's ISO 8601 UTC timestamps, with or without fractional seconds
    /// (Pydantic omits the fraction when it is zero and otherwise sends microseconds).
    public static func makeDecoder() -> JSONDecoder {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let raw = try container.decode(String.self)
            guard let date = parseDate(raw) else {
                throw DecodingError.dataCorruptedError(
                    in: container, debugDescription: "Invalid ISO 8601 date: \(raw)")
            }
            return date
        }
        return decoder
    }

    public static func parseDate(_ raw: String) -> Date? {
        let withFraction = Date.ISO8601FormatStyle(includingFractionalSeconds: true)
        if let date = try? withFraction.parse(raw) { return date }
        return try? Date.ISO8601FormatStyle().parse(raw)
    }
}
