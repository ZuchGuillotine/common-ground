// Codable mirrors of backend/common_ground/schema.py.
// Wire format is JSON with snake_case keys; Swift side is camelCase via explicit CodingKeys.
// Keep this file in sync with schema.py — nothing else in the app knows the wire names.

import Foundation

// MARK: - Shared enums

enum Speaker: String, Codable {
    case wearer, counterpart, unknown
}

enum Certainty: String, Codable {
    case none, tentative, moderate, strong
}

enum SessionGoal: String, Codable, CaseIterable, Identifiable {
    case explain, understand, resolve, negotiate

    var id: String { rawValue }

    var title: String {
        switch self {
        case .explain: "Explain"
        case .understand: "Understand"
        case .resolve: "Resolve"
        case .negotiate: "Negotiate"
        }
    }

    var subtitle: String {
        switch self {
        case .explain: "Explain something accurately"
        case .understand: "Understand their concerns"
        case .resolve: "Resolve disagreement or tension"
        case .negotiate: "Reach a mutually acceptable outcome"
        }
    }
}

struct SessionConfig: Codable {
    var sessionId: String
    var goal: SessionGoal = .explain
    var intendedMessage: String?
    var minSecondsBetweenCues: Double = 20
    var consentConfirmed: Bool = false

    enum CodingKeys: String, CodingKey {
        case sessionId = "session_id"
        case goal
        case intendedMessage = "intended_message"
        case minSecondsBetweenCues = "min_seconds_between_cues"
        case consentConfirmed = "consent_confirmed"
    }
}

// MARK: - Perception

/// Subset of `Turn` the phone displays. Unknown keys (acoustic, text_emotion, ...) are ignored by the decoder.
struct Turn: Codable, Identifiable {
    var turnId: Int
    var speaker: Speaker
    var text: String
    var tStart: Double
    var tEnd: Double
    var isFinal: Bool = true
    var coachingOverlap: Bool = false

    var id: Int { turnId }

    enum CodingKeys: String, CodingKey {
        case turnId = "turn_id"
        case speaker, text
        case tStart = "t_start"
        case tEnd = "t_end"
        case isFinal = "is_final"
        case coachingOverlap = "coaching_overlap"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        turnId = try c.decode(Int.self, forKey: .turnId)
        speaker = try c.decodeIfPresent(Speaker.self, forKey: .speaker) ?? .unknown
        text = try c.decodeIfPresent(String.self, forKey: .text) ?? ""
        tStart = try c.decodeIfPresent(Double.self, forKey: .tStart) ?? 0
        tEnd = try c.decodeIfPresent(Double.self, forKey: .tEnd) ?? tStart
        isFinal = try c.decodeIfPresent(Bool.self, forKey: .isFinal) ?? true
        coachingOverlap = try c.decodeIfPresent(Bool.self, forKey: .coachingOverlap) ?? false
    }
}

// MARK: - Interpretation

enum MismatchType: String, Codable {
    case termAmbiguity = "term_ambiguity"
    case politeConfusion = "polite_confusion"
    case unansweredObjection = "unanswered_objection"
    case indirectRefusal = "indirect_refusal"
    case escalatingFriction = "escalating_friction"
    case genuineDisagreement = "genuine_disagreement"
    case distraction
    case none

    var label: String {
        rawValue.replacingOccurrences(of: "_", with: " ").capitalized
    }
}

struct Hypothesis: Codable {
    var type: MismatchType
    var summary: String
    var evidenceTurns: [Int] = []
    var certainty: Certainty = .tentative
    var alternatives: [String] = []

    enum CodingKeys: String, CodingKey {
        case type, summary
        case evidenceTurns = "evidence_turns"
        case certainty, alternatives
    }
}

enum Strategy: String, Codable {
    case clarifyDefinition = "clarify_definition"
    case askCheckQuestion = "ask_check_question"
    case labelEmotion = "label_emotion"
    case mirror
    case slowDown = "slow_down"
    case switchFrame = "switch_frame"
    case acknowledgeObjection = "acknowledge_objection"
    case inviteNo = "invite_no"
    case staySilent = "stay_silent"

    var label: String {
        rawValue.replacingOccurrences(of: "_", with: " ").capitalized
    }
}

struct Intervention: Codable {
    var strategy: Strategy
    var suggestion: String      // phone screen: one sentence
    var earpieceCue: String     // glasses speaker: <= 4 words
    var successTest: String
    var rationale: String = ""

    enum CodingKeys: String, CodingKey {
        case strategy, suggestion
        case earpieceCue = "earpiece_cue"
        case successTest = "success_test"
        case rationale
    }
}

enum Outcome: String, Codable {
    case pending, resolved, unresolved, superseded
    case noIntervention = "no_intervention"
}

enum CoachState: String, Codable {
    case listening = "LISTENING"
    case possibleMismatch = "POSSIBLE_MISMATCH"
    case repairSuggested = "REPAIR_SUGGESTED"
    case awaitingResponse = "AWAITING_RESPONSE"
    case resolved = "RESOLVED"
    case unresolved = "UNRESOLVED"

    var label: String {
        switch self {
        case .listening: "Listening"
        case .possibleMismatch: "Possible mismatch"
        case .repairSuggested: "Repair suggested"
        case .awaitingResponse: "Awaiting response"
        case .resolved: "Resolved"
        case .unresolved: "Unresolved"
        }
    }
}

struct GraphEvent: Codable, Identifiable {
    var turnId: Int
    var speaker: Speaker
    var transcript: String
    var signals: [String: JSONValue] = [:]
    var interpretation: Hypothesis?
    var intervention: Intervention?
    var outcome: Outcome = .noIntervention
    var stateAfter: CoachState = .listening

    var id: Int { turnId }

    enum CodingKeys: String, CodingKey {
        case turnId = "turn_id"
        case speaker, transcript, signals, interpretation, intervention, outcome
        case stateAfter = "state_after"
    }

    init(turnId: Int, speaker: Speaker, transcript: String, signals: [String: JSONValue] = [:],
         interpretation: Hypothesis? = nil, intervention: Intervention? = nil,
         outcome: Outcome = .noIntervention, stateAfter: CoachState = .listening) {
        self.turnId = turnId
        self.speaker = speaker
        self.transcript = transcript
        self.signals = signals
        self.interpretation = interpretation
        self.intervention = intervention
        self.outcome = outcome
        self.stateAfter = stateAfter
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        turnId = try c.decode(Int.self, forKey: .turnId)
        speaker = try c.decodeIfPresent(Speaker.self, forKey: .speaker) ?? .unknown
        transcript = try c.decodeIfPresent(String.self, forKey: .transcript) ?? ""
        signals = try c.decodeIfPresent([String: JSONValue].self, forKey: .signals) ?? [:]
        interpretation = try c.decodeIfPresent(Hypothesis.self, forKey: .interpretation)
        intervention = try c.decodeIfPresent(Intervention.self, forKey: .intervention)
        outcome = try c.decodeIfPresent(Outcome.self, forKey: .outcome) ?? .noIntervention
        stateAfter = try c.decodeIfPresent(CoachState.self, forKey: .stateAfter) ?? .listening
    }
}

// MARK: - Transport

/// Phone -> server. Exactly one payload field is set per message.
struct ClientMessage: Encodable {
    enum Kind: String, Encodable {
        case start, audio, textTurn = "text_turn", cuePlayed = "cue_played", assignSpeaker = "assign_speaker", stop
    }

    var type: Kind
    var config: SessionConfig?
    var textTurn: [String: JSONValue]?
    var cueWindow: [Double]?               // [t_start, t_end] on the session clock
    var speakerMap: [String: Speaker]?

    enum CodingKeys: String, CodingKey {
        case type, config
        case textTurn = "text_turn"
        case cueWindow = "cue_window"
        case speakerMap = "speaker_map"
    }

    static func start(_ config: SessionConfig) -> ClientMessage { .init(type: .start, config: config) }
    static let stop = ClientMessage(type: .stop)
    static func cuePlayed(start: Double, end: Double) -> ClientMessage {
        .init(type: .cuePlayed, cueWindow: [start, end])
    }
    static func assignSpeaker(_ map: [String: Speaker]) -> ClientMessage {
        .init(type: .assignSpeaker, speakerMap: map)
    }
    static func textTurn(speaker: Speaker, text: String, tStart: Double, tEnd: Double) -> ClientMessage {
        .init(type: .textTurn, textTurn: [
            "speaker": .string(speaker.rawValue), "text": .string(text),
            "t_start": .number(tStart), "t_end": .number(tEnd),
        ])
    }
}

/// Server -> phone. `payload` shape depends on `type`:
///   transcript   -> partial/final text: {"speaker", "text", "is_final", "turn_id"?}
///   turn         -> Turn
///   state        -> {"state": CoachState}
///   cue          -> Intervention
///   graph_event  -> GraphEvent
///   error        -> {"message": String}
///   metrics      -> free-form
struct ServerMessage: Decodable {
    enum Kind: String, Decodable {
        case transcript, turn, state, cue, graphEvent = "graph_event", error, metrics
    }

    var type: Kind
    var payload: JSONValue
    var t: Double

    init(type: Kind, payload: JSONValue, t: Double) {
        self.type = type
        self.payload = payload
        self.t = t
    }

    /// Re-decode the untyped payload into a concrete model.
    func payload<T: Decodable>(as _: T.Type) throws -> T {
        let data = try WireCoding.encoder.encode(payload)
        return try WireCoding.decoder.decode(T.self, from: data)
    }
}

enum WireCoding {
    static let encoder: JSONEncoder = {
        let e = JSONEncoder()
        e.outputFormatting = [.withoutEscapingSlashes]
        return e
    }()
    static let decoder = JSONDecoder()
}

// MARK: - Untyped JSON

/// Minimal JSON tree for `dict` fields in the schema (payload, signals, text_turn).
enum JSONValue: Codable, Equatable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case null
    case array([JSONValue])
    case object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let n = try? c.decode(Double.self) { self = .number(n) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([JSONValue].self) { self = .array(a) }
        else if let o = try? c.decode([String: JSONValue].self) { self = .object(o) }
        else {
            throw DecodingError.dataCorruptedError(in: c, debugDescription: "Unsupported JSON value")
        }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let s): try c.encode(s)
        case .number(let n): try c.encode(n)
        case .bool(let b): try c.encode(b)
        case .null: try c.encodeNil()
        case .array(let a): try c.encode(a)
        case .object(let o): try c.encode(o)
        }
    }

    subscript(key: String) -> JSONValue? {
        if case .object(let o) = self { return o[key] }
        return nil
    }

    var stringValue: String? { if case .string(let s) = self { return s }; return nil }
    var boolValue: Bool? { if case .bool(let b) = self { return b }; return nil }
    var doubleValue: Double? { if case .number(let n) = self { return n }; return nil }

    /// Short human-readable rendering for the analysis view ("is_question: true").
    var displayText: String {
        switch self {
        case .string(let s): s
        case .number(let n): n == n.rounded() ? String(Int(n)) : String(format: "%.2f", n)
        case .bool(let b): b ? "yes" : "no"
        case .null: "–"
        case .array(let a): a.map(\.displayText).joined(separator: ", ")
        case .object(let o): o.keys.sorted().joined(separator: ", ")
        }
    }
}
