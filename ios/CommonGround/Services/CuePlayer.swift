// Speaks the <= 4-word earpiece cue through the current audio route (the glasses over Bluetooth)
// and reports the exact window it was audible so the server can mask the echo in the transcript.

import AVFoundation
import Foundation

@MainActor
final class CuePlayer: NSObject {
    /// Session clock in seconds (time since the `start` message). Set by the view model.
    var clock: () -> Double = { 0 }
    /// Called with [start, end] on the session clock after each cue finishes or is cancelled.
    var onCuePlayed: ((Double, Double) -> Void)?

    private(set) var isSpeaking = false

    private let synthesizer = AVSpeechSynthesizer()
    private var cueStart: Double?
    /// Extra margin after didFinish, covering Bluetooth output latency.
    private let tailMargin = 0.5

    override init() {
        super.init()
        synthesizer.delegate = self
    }

    func speak(_ cue: String) {
        let text = cue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        if synthesizer.isSpeaking {
            synthesizer.stopSpeaking(at: .immediate)
        }
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = AVSpeechSynthesisVoice(language: "en-US")
        utterance.rate = AVSpeechUtteranceDefaultSpeechRate
        utterance.volume = 1
        utterance.preUtteranceDelay = 0
        synthesizer.speak(utterance)
    }

    func stop() {
        synthesizer.stopSpeaking(at: .immediate)
    }

    private func didStart() {
        isSpeaking = true
        cueStart = clock()
    }

    private func didEnd() {
        isSpeaking = false
        guard let start = cueStart else { return }
        cueStart = nil
        onCuePlayed?(start, clock() + tailMargin)
    }
}

extension CuePlayer: AVSpeechSynthesizerDelegate {
    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didStart utterance: AVSpeechUtterance) {
        Task { @MainActor in self.didStart() }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        Task { @MainActor in self.didEnd() }
    }

    nonisolated func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didCancel utterance: AVSpeechUtterance) {
        Task { @MainActor in self.didEnd() }
    }
}
