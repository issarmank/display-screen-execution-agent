import SwiftUI

public struct ConversationView: View {
    @Bindable var model: ConversationViewModel

    public init(model: ConversationViewModel) {
        self.model = model
    }

    public var body: some View {
        VStack(spacing: 0) {
            if let error = model.bannerError {
                ErrorBanner(message: error, onDismiss: model.dismissBanner)
            }
            ScrollViewReader { proxy in
                List {
                    ForEach(model.turns) { turn in
                        TurnRow(turn: turn).id(turn.id)
                    }
                    if !model.partialText.isEmpty {
                        PartialRow(text: model.partialText).id(PartialRow.id)
                    }
                }
                .overlay {
                    if model.turns.isEmpty && model.partialText.isEmpty {
                        ContentUnavailableView(
                            "No messages yet", systemImage: "text.bubble",
                            description: Text("Type a message or click the mic to speak."))
                    }
                }
                .onChange(of: model.turns.last?.id) { _, id in
                    if let id { withAnimation { proxy.scrollTo(id, anchor: .bottom) } }
                }
                .onChange(of: model.partialText.isEmpty) { _, isEmpty in
                    if !isEmpty { proxy.scrollTo(PartialRow.id, anchor: .bottom) }
                }
            }
            Divider()
            InputBar(model: model)
        }
        .frame(minWidth: 420, minHeight: 360)
        .task { await model.onAppear() }
    }
}

struct ErrorBanner: View {
    let message: String
    let onDismiss: () -> Void

    var body: some View {
        HStack {
            Image(systemName: "exclamationmark.triangle.fill")
            Text(message).frame(maxWidth: .infinity, alignment: .leading)
            Button("Dismiss", systemImage: "xmark", action: onDismiss)
                .labelStyle(.iconOnly)
                .buttonStyle(.plain)
        }
        .padding(10)
        .foregroundStyle(.white)
        .background(.red.opacity(0.85))
    }
}
