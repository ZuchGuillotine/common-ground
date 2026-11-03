// The "turn analysis" speed: for review after (or during a lull in) the conversation.
// Observation, interpretation (+ alternatives), intervention and outcome are shown as
// separate blocks so a guess never looks like a fact.

import SwiftUI

struct TurnAnalysisView: View {
    let events: [GraphEvent]

    var body: some View {
        Group {
            if events.isEmpty {
                ContentUnavailableView("No turns yet", systemImage: "waveform", description: Text("Turns appear here as the conversation unfolds."))
            } else {
                List(events) { event in
                    GraphEventRow(event: event)
                        .listRowSeparator(.hidden)
                        .padding(.vertical, 6)
                }
                .listStyle(.plain)
            }
        }
        .navigationTitle("Turn analysis")
        .navigationBarTitleDisplayMode(.inline)
    }
}

private struct GraphEventRow: View {
    let event: GraphEvent

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(speakerLabel)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(event.speaker == .wearer ? .blue : .primary)
                Spacer()
                StatePill(state: event.stateAfter)
            }

            Text(event.transcript)
                .font(.body)

            if !activeSignals.isEmpty {
                labeled("Observed") {
                    Text(activeSignals.joined(separator: " · "))
                        .font(.subheadline)
                }
            }

            if let hypothesis = event.interpretation, hypothesis.type != .none {
                labeled("Interpretation · \(hypothesis.certainty.rawValue)") {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("\(hypothesis.type.label): \(hypothesis.summary)")
                            .font(.subheadline)
                        ForEach(hypothesis.alternatives, id: \.self) { alternative in
                            Text("or: \(alternative)")
                                .font(.subheadline)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }

            if let intervention = event.intervention {
                labeled("Suggested · \(intervention.strategy.label)") {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(intervention.suggestion).font(.subheadline)
                        Text("Cue: \u{201C}\(intervention.earpieceCue)\u{201D}")
                            .font(.subheadline).foregroundStyle(.secondary)
                        Text("Success looks like: \(intervention.successTest)")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                }
            }

            if event.outcome != .noIntervention {
                labeled("Outcome") {
                    Text(outcomeLabel)
                        .font(.subheadline.weight(.medium))
                        .foregroundStyle(outcomeColor)
                }
            }
        }
    }

    private var speakerLabel: String {
        switch event.speaker {
        case .wearer: "You · turn \(event.turnId)"
        case .counterpart: "Them · turn \(event.turnId)"
        case .unknown: "Unknown · turn \(event.turnId)"
        }
    }

    /// Signals that are set (true / non-empty), rendered as "is question", "jargon: integration".
    private var activeSignals: [String] {
        event.signals.keys.sorted().compactMap { key in
            guard let value = event.signals[key] else { return nil }
            let name = key.replacingOccurrences(of: "_", with: " ")
            switch value {
            case .bool(true): return name
            case .bool(false), .null: return nil
            case .array(let items) where items.isEmpty: return nil
            default: return "\(name): \(value.displayText)"
            }
        }
    }

    private var outcomeLabel: String {
        switch event.outcome {
        case .pending: "Waiting to see"
        case .resolved: "Resolved"
        case .unresolved: "Unresolved"
        case .superseded: "Superseded"
        case .noIntervention: ""
        }
    }

    private var outcomeColor: Color {
        switch event.outcome {
        case .resolved: .green
        case .unresolved: .orange
        default: .secondary
        }
    }

    private func labeled<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title.uppercased())
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
            content()
        }
    }
}

#Preview {
    NavigationStack {
        TurnAnalysisView(events: [
            GraphEvent(turnId: 1, speaker: .wearer, transcript: "So the integration layer sits between your CRM and ours.",
                       signals: ["jargon_terms": .array([.string("integration layer")])]),
            GraphEvent(turnId: 2, speaker: .counterpart, transcript: "Sure, I guess.",
                       signals: ["hedged_agreement": .bool(true), "minimal_response": .bool(true)],
                       interpretation: Hypothesis(type: .politeConfusion, summary: "Agreed in words, but the reply was minimal and hedged.",
                                                  evidenceTurns: [1, 2], certainty: .moderate,
                                                  alternatives: ["They agree and are just brief", "They are distracted"]),
                       intervention: Intervention(strategy: .askCheckQuestion, suggestion: "Ask whether 'integration layer' is clear to them.",
                                                  earpieceCue: "Check: integration clear?", successTest: "They restate it or ask a specific question."),
                       outcome: .pending, stateAfter: .awaitingResponse),
        ])
    }
}
