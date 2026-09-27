import SwiftUI

struct InputBar: View {
    @Bindable var model: ConversationViewModel

    var body: some View {
        HStack(spacing: 8) {
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
