import Foundation
import Testing

@testable import ScreenAgentCore

/// Opt-in tests against a running backend (the Swift counterpart of
/// `pytest -m integration`). Skipped unless SCREEN_AGENT_INTEGRATION_URL is set:
///   SCREEN_AGENT_INTEGRATION_URL=http://127.0.0.1:8000 swift test --filter LiveBackend
enum LiveBackend {
    static let url: URL? = ProcessInfo.processInfo.environment["SCREEN_AGENT_INTEGRATION_URL"]
        .flatMap(URL.init(string:))
}

@MainActor
@Suite("LiveBackend – text path", .enabled(if: LiveBackend.url != nil))
struct LiveBackendTextTests {
    @Test func typedTurnRoundTripsThroughRealBackend() async throws {
        let api = HTTPSessionAPI(baseURL: try #require(LiveBackend.url))
        let vm = ConversationViewModel(api: api)
        await vm.onAppear()
        let sessionID = try #require(vm.sessionID)

        vm.draft = "open safari"
        await vm.send()
        #expect(vm.bannerError == nil)
        #expect(vm.turns.map(\.text) == ["open safari"])

        let detail = try await api.getSession(id: sessionID)
        #expect(detail.turns.map(\.source) == [.text])
        #expect(detail.turns.first?.id == vm.turns.first?.id)

        vm.draft = "   "
        await vm.send()  // blocked client-side, never reaches the server
        await #expect(throws: APIError.self) {
            try await api.addTextTurn(sessionID: sessionID, text: "   ")  // server says 422
        }

        await vm.endSession()
        #expect(try await api.getSession(id: sessionID).status == "completed")
        vm.draft = "too late"
        await vm.send()
        #expect(vm.bannerError?.contains("409") == true)
        #expect(vm.draft == "too late")
    }
}
