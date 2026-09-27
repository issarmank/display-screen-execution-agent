import SwiftUI

struct TurnRow: View {
    let turn: Turn

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: turn.source == .voice ? "mic.fill" : "keyboard")
                .foregroundStyle(.secondary)
                .frame(width: 18)
                .accessibilityLabel(turn.source == .voice ? "Spoken" : "Typed")
            Text(turn.text)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
            Text(turn.createdAt, style: .time)
                .font(.caption)
                .foregroundStyle(.tertiary)
        }
        .padding(.vertical, 4)
    }
}
