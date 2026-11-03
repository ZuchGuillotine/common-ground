// Audio capture. Both sources deliver PCM16 mono 16 kHz little-endian chunks (~100 ms each)
// through `chunks`, ready to be sent as WebSocket binary frames.
//
//   PhoneMicSource   — AVAudioEngine input tap, resampled with AVAudioConverter.  Works today.
//   GlassesMicSource — Meta Wearables DAT 1.0 experimental camera-stream PCM audio.
//                      Compiles without the SDK; the real calls are inside #if canImport(...).
//
// See docs/WEARABLES_SDK.md for the API sources and gotchas.

import AVFoundation
import Foundation
import os

#if canImport(MWDATCore) && canImport(MWDATCamera)
import MWDATCore
import MWDATCamera
#endif

// MARK: - Protocol

protocol AudioSource: AnyObject {
    /// PCM16 mono 16 kHz chunks. Finishes when the source stops.
    var chunks: AsyncStream<Data> { get }
    func start() async throws
    func stop()
}

enum AudioSourceError: LocalizedError {
    case microphoneDenied
    case engineFormat
    case sdkNotLinked
    case glassesPermissionDenied
    case glassesSessionFailed(String)

    var errorDescription: String? {
        switch self {
        case .microphoneDenied: "Microphone permission was denied."
        case .engineFormat: "Could not build the 16 kHz audio converter."
        case .sdkNotLinked: "Meta Wearables SDK is not added to this target (see ios/README.md)."
        case .glassesPermissionDenied: "Camera + microphone permission for the glasses was not granted in Meta AI."
        case .glassesSessionFailed(let why): "Glasses session failed: \(why)"
        }
    }
}

// MARK: - Shared 16 kHz Int16 format + conversion

enum PCM16 {
    static let sampleRate: Double = 16_000

    /// Target wire format: Int16, mono, 16 kHz, interleaved (single channel so interleaving is moot).
    static let format = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: sampleRate, channels: 1, interleaved: true)!

    /// Convert any PCM buffer (Float32/Int16, any rate, 1–2 ch) to the wire format and return its bytes.
    /// `converter` must have been created for `buffer.format` -> `PCM16.format`.
    static func data(from buffer: AVAudioPCMBuffer, using converter: AVAudioConverter) -> Data? {
        if buffer.format == format, let bytes = buffer.int16ChannelData {
            return Data(bytes: bytes[0], count: Int(buffer.frameLength) * MemoryLayout<Int16>.size)
        }
        let ratio = sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 32
        guard let out = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: capacity) else { return nil }

        var consumed = false
        var error: NSError?
        let status = converter.convert(to: out, error: &error) { _, outStatus in
            if consumed {
                outStatus.pointee = .noDataNow
                return nil
            }
            consumed = true
            outStatus.pointee = .haveData
            return buffer
        }
        guard status != .error, error == nil, let bytes = out.int16ChannelData else { return nil }
        return Data(bytes: bytes[0], count: Int(out.frameLength) * MemoryLayout<Int16>.size)
    }
}

// MARK: - Audio session routing

enum AudioRouting {
    /// iPhone mic in, glasses (or any A2DP device) out. Default and fallback path.
    case phoneMicGlassesSpeaker
    /// Glasses HFP mic in/out via Bluetooth. Mono voice quality; mic is beamformed to the wearer.
    case glassesHFP

    /// Configure before starting any capture or the DAT stream (Meta: starting the DAT stream before
    /// HFP is ready can make the route fail silently).
    func activate() throws {
        let session = AVAudioSession.sharedInstance()
        let options: AVAudioSession.CategoryOptions
        switch self {
        case .phoneMicGlassesSpeaker: options = [.allowBluetoothA2DP]
        case .glassesHFP: options = [.allowBluetoothHFP]   // `.allowBluetooth` on SDKs older than iOS 26
        }
        try session.setCategory(.playAndRecord, mode: .default, options: options)
        try session.setPreferredSampleRate(PCM16.sampleRate)
        try session.setPreferredIOBufferDuration(0.1)
        try session.setActive(true, options: [])
    }

    /// True when some Bluetooth output port is in the current route (so cues will reach the glasses).
    static var bluetoothOutputActive: Bool {
        AVAudioSession.sharedInstance().currentRoute.outputs.contains {
            [.bluetoothA2DP, .bluetoothHFP, .bluetoothLE].contains($0.portType)
        }
    }
}

// MARK: - Phone microphone

final class PhoneMicSource: AudioSource, @unchecked Sendable {
    let chunks: AsyncStream<Data>
    private let continuation: AsyncStream<Data>.Continuation
    private let engine = AVAudioEngine()
    private var converter: AVAudioConverter?
    private let log = Logger(subsystem: "commonground", category: "phone-mic")

    init() {
        var cont: AsyncStream<Data>.Continuation!
        chunks = AsyncStream(bufferingPolicy: .bufferingNewest(64)) { cont = $0 }
        continuation = cont
    }

    func start() async throws {
        guard await AVAudioApplication.requestRecordPermission() else {
            throw AudioSourceError.microphoneDenied
        }

        let input = engine.inputNode
        let inputFormat = input.outputFormat(forBus: 0)
        guard inputFormat.sampleRate > 0,
              let converter = AVAudioConverter(from: inputFormat, to: PCM16.format) else {
            throw AudioSourceError.engineFormat
        }
        self.converter = converter

        // ~100 ms at the hardware rate.
        let bufferSize = AVAudioFrameCount(inputFormat.sampleRate / 10)
        input.installTap(onBus: 0, bufferSize: bufferSize, format: inputFormat) { [weak self] buffer, _ in
            guard let self, let data = PCM16.data(from: buffer, using: converter) else { return }
            self.continuation.yield(data)
        }

        engine.prepare()
        try engine.start()
        log.info("phone mic started at \(inputFormat.sampleRate) Hz, \(inputFormat.channelCount) ch")
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        continuation.finish()
    }
}

// MARK: - Glasses microphone (Meta Wearables DAT, experimental)

/// Synchronized PCM audio from the Ray-Ban Meta camera stream.
///
/// API names from the official `audio-streaming` / `camera-streaming` skills in
/// github.com/facebook/meta-wearables-dat-ios (plugins/mwdat-ios/skills/). The feature is
/// "Dev and beta only" and cannot be published to production release channels.
final class GlassesMicSource: AudioSource, @unchecked Sendable {
    let chunks: AsyncStream<Data>
    private let continuation: AsyncStream<Data>.Continuation
    private var converter: AVAudioConverter?
    private let log = Logger(subsystem: "commonground", category: "glasses-mic")

    #if canImport(MWDATCore) && canImport(MWDATCamera)
    private var session: DeviceSession?
    private var camera: Camera?
    private let tokens = ListenerTokenBag()
    #endif

    init() {
        var cont: AsyncStream<Data>.Continuation!
        chunks = AsyncStream(bufferingPolicy: .bufferingNewest(64)) { cont = $0 }
        continuation = cont
    }

    func start() async throws {
        #if canImport(MWDATCore) && canImport(MWDATCamera)
        let wearables = Wearables.shared

        // 1. Permissions. Each requestPermission app-switches to Meta AI; the view explains that first.
        for permission in [Permission.camera, Permission.microphone] {
            if try await wearables.checkPermissionStatus(permission) != .granted {
                guard try await wearables.requestPermission(permission) == .granted else {
                    throw AudioSourceError.glassesPermissionDenied
                }
            }
        }

        // 2. Device session.
        let session = try wearables.createSession(deviceSelector: AutoDeviceSelector(wearables: wearables))
        self.session = session
        try session.start()
        for await state in session.stateStream() {
            if state == .started { break }
            if state == .stopped { throw AudioSourceError.glassesSessionFailed("device session stopped before starting") }
        }

        // 3. Camera stream with PCM audio. Audio only exists as part of the camera stream,
        //    so use the cheapest video settings and ignore videoFramePublisher.
        let config = StreamConfiguration(
            videoCodec: .raw,
            audioCodec: .pcm(sampleRate: .rate16000, numberOfChannels: 1),
            resolution: .low,
            frameRate: 2
        )
        guard let camera = try session.addCamera(config: config) else {
            throw AudioSourceError.glassesSessionFailed("addCamera returned nil (session not started?)")
        }
        self.camera = camera
        let stream = camera.stream

        // 4. Listen BEFORE start(); keep tokens on this long-lived owner.
        stream.audioFramePublisher.listen { [weak self] frame in
            // TODO(SDK): confirm `frame.pcmBuffer` is AVAudioPCMBuffer (AudioCodec docs: "Apple Standard PCM,
            // Float32 native-endian") and `frame.presentationTimeStamp` is CMTime. If pcmBuffer is a raw
            // Data/UnsafeBufferPointer instead, wrap it in an AVAudioPCMBuffer with a Float32 16 kHz mono
            // format before calling ingest(_:).
            self?.ingest(frame.pcmBuffer)
        }.store(in: tokens)

        stream.errorPublisher.listen { [weak self] error in
            self?.log.error("stream error: \(error.localizedDescription)")
        }.store(in: tokens)

        stream.statePublisher.listen { [weak self] state in
            self?.log.info("stream state: \(String(describing: state))")
        }.store(in: tokens)

        stream.start()
        #else
        throw AudioSourceError.sdkNotLinked
        #endif
    }

    func stop() {
        #if canImport(MWDATCore) && canImport(MWDATCamera)
        tokens.clear()          // cancel listeners deterministically before stopping the camera
        camera?.stop()          // cascades to the stream
        session?.stop()
        camera = nil
        session = nil
        #endif
        continuation.finish()
    }

    /// Converts whatever the SDK delivers (expected Float32 16 kHz mono) to Int16 and yields it.
    private func ingest(_ buffer: AVAudioPCMBuffer) {
        if converter == nil || converter?.inputFormat != buffer.format {
            converter = AVAudioConverter(from: buffer.format, to: PCM16.format)
        }
        guard let converter, let data = PCM16.data(from: buffer, using: converter) else { return }
        continuation.yield(data)
    }
}
