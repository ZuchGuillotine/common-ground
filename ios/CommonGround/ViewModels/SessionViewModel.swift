// Ties socket, audio input and cue playback together and exposes the two-speed UI state.
// `demoMode` replays Resources/demo_session.json through the same `handle(_:)` path, so the
// views can be demoed with no server and no glasses.

import Foundation
import Observation
import os

@MainActor
@Observable
final class SessionViewModel {
    enum Phase: Equatable { case setup, live }
    enum InputMode: String, CaseIterable, Identifiable {
        case phoneMic, glasses
        var id: String { rawValue }
        var title: String { self == .phoneMic ? "Phone mic" : "Glasses" }
    }

    // Setup
    var goal: SessionGoal = .explain
    var intendedMessage = ""
    var consentConfirmed = false
    var inputMode: InputMode = .phoneMic
    var serverHost = "localhost:8000"
    var demoMode = false

    // Live
    private(set) var phase: Phase = .setup
    private(set) var coachState: CoachState = .listening
    private(set) var liveTranscript = ""              // latest partial/final text, for a faint "hearing" line
    private(set) var observedSignal: String?          // what we noticed, in plain words
    private(set) var suggestion: Intervention?        // "try next"
    private(set) var events: [GraphEvent] = []        // turn analysis
    private(set) var connectionLabel = "Not connected"
    private(set) var errorText: String?

    var canStart: Bool { consentConfirmed }

    // Plumbing
    private var socket: SessionSocket?
    private var audio: AudioSource?
    private let cues = CuePlayer()
    private var tasks: [Task<Void, Never>] = []
    private var sessionStart = Date()
    private var config: SessionConfig?
    private let log = Logger(subsystem: "commonground", category: "session")

    init() {
        cues.clock = { [weak self] in self?.now ?? 0 }
        cues.onCuePlayed = { [weak self] start, end in
            guard let self else { return }
            Task { await self.socket?.send(.cuePlayed(start: start, end: end)) }
        }
    }

    /// Seconds since `start` was sent. The server's turn/cue timestamps use the same origin.
    var now: Double { Date().timeIntervalSince(sessionStart) }

    // MARK: - Start / stop

    func start() async {
        guard canStart else { return }
        errorText = nil
        events = []
        suggestion = nil
        observedSignal = nil
        liveTranscript = ""
        coachState = .listening
        sessionStart = Date()

        let trimmedMessage = intendedMessage.trimmingCharacters(in: .whitespacesAndNewlines)
        let config = SessionConfig(
            sessionId: UUID().uuidString,
            goal: goal,
            intendedMessage: trimmedMessage.isEmpty ? nil : trimmedMessage,
            consentConfirmed: consentConfirmed
        )
        self.config = config
        phase = .live

        if demoMode {
            connectionLabel = "Demo"
            startDemoReplay()
            return
        }

        // Audio route first (see docs/WEARABLES_SDK.md gotcha 4). Both input modes use A2DP for
        // cue output: in glasses mode the mic comes from the DAT camera stream, not from HFP.
        do {
            try AudioRouting.phoneMicGlassesSpeaker.activate()
        } catch {
            errorText = "Audio session: \(error.localizedDescription)"
        }

        let socket = SessionSocket(host: serverHost)
        self.socket = socket
        socket.onStateChange = { [weak self] state in
            self?.connectionLabel = switch state {
            case .connected: "Connected"
            case .connecting: "Connecting…"
            case .disconnected: "Not connected"
            }
        }
        socket.onConnected = { [weak self, weak socket] in
            guard let self, let socket, let config = self.config else { return }
            Task { await socket.send(.start(config)) }
        }
        socket.connect()

        tasks.append(Task { [weak self] in
            for await message in socket.messages {
                self?.handle(message)
            }
        })

        let source: AudioSource = inputMode == .glasses ? GlassesMicSource() : PhoneMicSource()
        audio = source
        do {
            try await source.start()
        } catch {
            errorText = error.localizedDescription
            log.error("audio start failed: \(error.localizedDescription)")
            return
        }
        tasks.append(Task { [weak self] in
            for await chunk in source.chunks {
                await self?.socket?.send(pcm: chunk)
            }
        })
    }

    func stop() {
        cues.stop()
        audio?.stop()
        audio = nil
        tasks.forEach { $0.cancel() }
        tasks = []
        if let socket {
            Task {
                await socket.send(.stop)
                socket.disconnect()
            }
        }
        socket = nil
        phase = .setup
        connectionLabel = "Not connected"
    }

    // MARK: - Server messages

    func handle(_ message: ServerMessage) {
        do {
            switch message.type {
            case .transcript:
                if let text = message.payload["text"]?.stringValue {
                    liveTranscript = text
                }

            case .turn:
                let turn = try message.payload(as: Turn.self)
                if turn.isFinal { liveTranscript = turn.text }

            case .state:
                if let raw = message.payload["state"]?.stringValue, let state = CoachState(rawValue: raw) {
                    coachState = state
                    if state == .listening || state == .resolved { suggestion = nil }
                }

            case .cue:
                let intervention = try message.payload(as: Intervention.self)
                suggestion = intervention
                cues.speak(intervention.earpieceCue)

            case .graphEvent:
                let event = try message.payload(as: GraphEvent.self)
                if let index = events.firstIndex(where: { $0.turnId == event.turnId }) {
                    events[index] = event
                } else {
                    events.append(event)
                }
                if let hypothesis = event.interpretation, hypothesis.type != .none {
                    observedSignal = hypothesis.summary
                } else if event.outcome == .resolved {
                    observedSignal = "That landed."
                }
                coachState = event.stateAfter

            case .error:
                errorText = message.payload["message"]?.stringValue ?? "Server error"

            case .metrics:
                break
            }
        } catch {
            log.warning("could not decode \(message.type.rawValue) payload: \(error.localizedDescription)")
        }
    }

    // MARK: - Demo replay

    private struct DemoStep: Decodable {
        var delay: Double
        var message: ServerMessage

        enum CodingKeys: String, CodingKey { case delay = "delay_s", message }
    }

    private func startDemoReplay() {
        guard let url = Bundle.main.url(forResource: "demo_session", withExtension: "json"),
              let data = try? Data(contentsOf: url),
              let steps = try? WireCoding.decoder.decode([DemoStep].self, from: data) else {
            errorText = "demo_session.json is missing from the bundle."
            return
        }
        tasks.append(Task { [weak self] in
            for step in steps {
                try? await Task.sleep(for: .seconds(step.delay))
                guard !Task.isCancelled else { return }
                self?.handle(step.message)
            }
        })
    }
}
