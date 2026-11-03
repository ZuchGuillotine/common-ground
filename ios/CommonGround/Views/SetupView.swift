import SwiftUI

#if canImport(MWDATCore)
import MWDATCore
#endif

struct SetupView: View {
    @Bindable var model: SessionViewModel
    @State private var registrationNote: String?

    var body: some View {
        Form {
            Section("What do you want from this conversation?") {
                ForEach(SessionGoal.allCases) { goal in
                    Button {
                        model.goal = goal
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(goal.title).font(.headline)
                                Text(goal.subtitle).font(.subheadline).foregroundStyle(.secondary)
                            }
                            Spacer()
                            if model.goal == goal {
                                Image(systemName: "checkmark").foregroundStyle(.tint)
                            }
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
            }

            Section {
                TextField("e.g. I'm offering an integration, not a replacement", text: $model.intendedMessage, axis: .vertical)
                    .lineLimit(2...4)
            } header: {
                Text("The one thing you want them to take away (optional)")
            }

            Section("Input") {
                Picker("Microphone", selection: $model.inputMode) {
                    ForEach(SessionViewModel.InputMode.allCases) { mode in
                        Text(mode.title).tag(mode)
                    }
                }
                .pickerStyle(.segmented)
                TextField("Server host:port", text: $model.serverHost)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .keyboardType(.URL)
                Toggle("Demo mode (no server)", isOn: $model.demoMode)

                #if canImport(MWDATCore)
                Button("Register with Meta AI") {
                    Task {
                        do {
                            try await Wearables.shared.startRegistration()
                            registrationNote = "Opened Meta AI. Approve the app, then come back."
                        } catch {
                            registrationNote = error.localizedDescription
                        }
                    }
                }
                if let registrationNote {
                    Text(registrationNote).font(.footnote).foregroundStyle(.secondary)
                }
                #endif
            }

            Section {
                Toggle(isOn: $model.consentConfirmed) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Everyone here knows this is recorded")
                        Text("Ask out loud before you start. The glasses' light will be on.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }
                }
            }

            Section {
                Button {
                    Task { await model.start() }
                } label: {
                    Text("Start listening")
                        .font(.title3.weight(.semibold))
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 6)
                }
                .buttonStyle(.borderedProminent)
                .disabled(!model.canStart)
                .listRowBackground(Color.clear)
            }
        }
        .navigationTitle("Common Ground")
    }
}

#Preview {
    NavigationStack { SetupView(model: SessionViewModel()) }
}
