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

/// The live transcript while the user is still speaking.
struct PartialRow: View {
    static let id = "partial-transcript"
    let text: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "waveform")
                .foregroundStyle(.secondary)
                .frame(width: 18)
                .symbolEffect(.variableColor.iterative)
            Text(text)
                .italic()
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.vertical, 4)
        .accessibilityLabel("Listening: \(text)")
    }
}
