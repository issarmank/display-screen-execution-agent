import Foundation
import Testing

@testable import ScreenAgentCore

/// The UI flows for typed input, played end to end against a mocked backend
/// (XCUITest needs Xcode, which isn't available; see CLAUDE.md).
@MainActor
@Suite("ConversationViewModel – text flow")
struct ConversationViewModelTextTests {
    let api = MockSessionAPI()

    @Test func onAppearCreatesSessionOnce() async {
        let vm = ConversationViewModel(api: api)
        await vm.onAppear()
        await vm.onAppear()
        #expect(vm.sessionID == "s1")
        #expect(await api.calls == [.create])
        #expect(vm.bannerError == nil)
    }

    @Test func typeAndSendAppendsRowAndClearsDraft() async {
        let vm = ConversationViewModel(api: api)
        await vm.onAppear()
        vm.draft = "  open safari \n"
        #expect(vm.canSend)
        await vm.send()
        #expect(vm.turns.map(\.text) == ["open safari"])
        #expect(vm.turns.first?.source == .text)
        #expect(vm.draft == "")
        #expect(!vm.isSending)
        #expect(await api.calls == [.create, .addText("s1", "open safari")])
    }

    @Test(arguments: ["", "   ", "\n\t"])
    func emptyDraftIsBlocked(_ draft: String) async {
        let vm = ConversationViewModel(api: api)
        await vm.onAppear()
        vm.draft = draft
        #expect(!vm.canSend)
        await vm.send()
        #expect(vm.turns.isEmpty)
        #expect(await api.calls == [.create])
    }

    @Test func apiFailureKeepsDraftAndShowsBanner() async {
        let vm = ConversationViewModel(api: api)
        await vm.onAppear()
        await api.setAddTextError(.transport("Could not connect to the server."))
        vm.draft = "open safari"
        await vm.send()
        #expect(vm.draft == "open safari")
        #expect(vm.turns.isEmpty)
        #expect(vm.bannerError?.contains("Can't reach the backend") == true)
        #expect(!vm.isSending)

        // Retrying after the backend recovers succeeds and clears the banner.
        await api.setAddTextError(nil)
        await vm.send()
        #expect(vm.turns.map(\.text) == ["open safari"])
        #expect(vm.bannerError == nil)
    }

    @Test func backendDownAtLaunchRecoversOnSend() async {
        await api.setCreateResult(.failure(.transport("refused")))
        let vm = ConversationViewModel(api: api)
        await vm.onAppear()
        #expect(vm.sessionID == nil)
        #expect(vm.bannerError != nil)

        await api.setCreateResult(.success(Fixtures.session(id: "s2")))
        vm.draft = "hello"
        await vm.send()
        #expect(vm.sessionID == "s2")
        #expect(vm.turns.map(\.sessionID) == ["s2"])
        #expect(vm.bannerError == nil)
    }

    @Test func closedSessionErrorIsShown() async {
        let vm = ConversationViewModel(api: api)
        await vm.onAppear()
        await api.setAddTextError(.http(status: 409, detail: "Session s1 is not active"))
        vm.draft = "late"
        await vm.send()
        #expect(vm.bannerError == "Server error 409: Session s1 is not active")
    }

    @Test func endSessionIsCalledOnceAndIsBestEffort() async {
        let vm = ConversationViewModel(api: api)
        await vm.endSession()  // no session yet: nothing to end
        #expect(await api.calls.isEmpty)
        await vm.onAppear()
        await vm.endSession()
        await vm.endSession()
        #expect(await api.calls == [.create, .end("s1")])
    }

    @Test func dismissBannerClearsIt() async {
        await api.setCreateResult(.failure(.transport("refused")))
        let vm = ConversationViewModel(api: api)
        await vm.onAppear()
        vm.dismissBanner()
        #expect(vm.bannerError == nil)
    }

    @Test func duplicateTurnIDsAreNotAppendedTwice() {
        let vm = ConversationViewModel(api: api)
        let turn = Fixtures.turn(id: "t1", sessionID: "s1", source: .voice, text: "hi")
        vm.append(turn)
        vm.append(turn)
        #expect(vm.turns.count == 1)
    }
}

@MainActor
@Suite("AppEnvironment")
struct AppEnvironmentTests {
    @Test func defaultsToLocalBackend() {
        #expect(AppEnvironment.backendURL(environment: [:]) == AppEnvironment.defaultBackendURL)
    }

    @Test func honoursOverride() {
        let url = AppEnvironment.backendURL(
            environment: ["SCREEN_AGENT_BACKEND_URL": "http://10.0.0.5:9000"])
        #expect(url.absoluteString == "http://10.0.0.5:9000")
    }

    @Test(arguments: ["", "not a url", "ftp://x"])
    func ignoresInvalidOverride(_ raw: String) {
        let url = AppEnvironment.backendURL(environment: ["SCREEN_AGENT_BACKEND_URL": raw])
        #expect(url == AppEnvironment.defaultBackendURL)
    }
}
