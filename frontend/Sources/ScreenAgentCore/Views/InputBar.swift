import SwiftUI

struct InputBar: View {
    @Bindable var model: ConversationViewModel

    var body: some View {
        HStack(spacing: 8) {
            MicButton(state: model.voiceState) { Task { await model.toggleMic() } }
            TextField("Type a message…", text: $model.draft, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .lineLimit(1...5)
                .onSubmit { Task { await model.send() } }
            Button("Send") { Task { await model.send() } }
                .keyboardShortcut(.return, modifiers: .command)
                .disabled(!model.canSend)
        }
        .padding(12)
    }
}

struct MicButton: View {
    let state: VoiceState
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Group {
                switch state {
                case .idle:
                    Image(systemName: "mic")
                case .connecting, .stopping:
                    ProgressView().controlSize(.small)
                case .recording:
                    Image(systemName: "stop.circle.fill").foregroundStyle(.red)
                }
            }
            .frame(width: 20, height: 20)
        }
        .buttonStyle(.borderless)
        .disabled(state == .stopping)
        .help(help)
        .accessibilityLabel(help)
    }

    private var help: String {
        switch state {
        case .idle: return "Start speaking"
        case .connecting: return "Connecting… (click to cancel)"
        case .recording: return "Stop speaking"
        case .stopping: return "Finishing transcription…"
        }
    }
}
