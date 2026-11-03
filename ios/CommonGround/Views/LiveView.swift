// The "live conversation" speed: three lines of large, calm text and one quiet state pill.
// No meters, no scores, no scrolling transcript — glance, then look back at the person.

import SwiftUI

struct LiveView: View {
    @Bindable var model: SessionViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 32) {
            header

            block(title: "Noticed") {
                Text(model.observedSignal ?? "Nothing yet. Just talk.")
                    .font(.title2)
                    .foregroundStyle(model.observedSignal == nil ? .secondary : .primary)
            }

            block(title: "Try next") {
                if let suggestion = model.suggestion {
                    VStack(alignment: .leading, spacing: 10) {
                        Text(suggestion.suggestion)
                            .font(.title.weight(.medium))
                        Text("\u{201C}\(suggestion.earpieceCue)\u{201D}")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                } else {
                    Text("Keep going.")
                        .font(.title2)
                        .foregroundStyle(.secondary)
                }
            }

            Spacer()

            if !model.liveTranscript.isEmpty {
                Text(model.liveTranscript)
                    .font(.footnote)
                    .foregroundStyle(.tertiary)
                    .lineLimit(2)
            }

            if let error = model.errorText {
                Text(error)
                    .font(.footnote)
                    .foregroundStyle(.red)
            }

            footer
        }
        .padding(24)
        .animation(.easeInOut(duration: 0.35), value: model.observedSignal)
        .animation(.easeInOut(duration: 0.35), value: model.suggestion?.suggestion)
        .navigationBarBackButtonHidden(true)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                NavigationLink("Analysis") {
                    TurnAnalysisView(events: model.events)
                }
            }
        }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            VStack(alignment: .leading, spacing: 2) {
                Text(model.goal.title.uppercased())
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                if let message = model.intendedMessageOrNil {
                    Text(message)
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
            Spacer()
            StatePill(state: model.coachState)
        }
    }

    private var footer: some View {
        HStack {
            Text(model.connectionLabel)
                .font(.caption)
                .foregroundStyle(.tertiary)
            Spacer()
            Button("End", role: .destructive) {
                model.stop()
            }
            .buttonStyle(.bordered)
        }
    }

    private func block<Content: View>(title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title.uppercased())
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            content()
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

struct StatePill: View {
    let state: CoachState

    var body: some View {
        Text(state.label)
            .font(.caption.weight(.medium))
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .background(tint.opacity(0.15), in: Capsule())
            .foregroundStyle(tint)
    }

    private var tint: Color {
        switch state {
        case .listening: .secondary
        case .possibleMismatch, .awaitingResponse: .orange
        case .repairSuggested: .blue
        case .resolved: .green
        case .unresolved: .gray
        }
    }
}

extension SessionViewModel {
    var intendedMessageOrNil: String? {
        let trimmed = intendedMessage.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}

#Preview {
    let model = SessionViewModel()
    model.demoMode = true
    model.consentConfirmed = true
    return NavigationStack { LiveView(model: model) }
        .task { await model.start() }
}
