// WebSocket client for ws://HOST:8000/ws/session.
// Text frames: JSON ClientMessage / ServerMessage. Binary frames: raw PCM16 mono 16 kHz.
// All state lives on the main actor; sends are async so the audio pipeline never blocks on I/O.

import Foundation
import os

@MainActor
final class SessionSocket {
    enum State: Equatable {
        case disconnected, connecting, connected
    }

    /// Decoded server messages, in arrival order. One consumer (the view model).
    let messages: AsyncStream<ServerMessage>

    private(set) var state: State = .disconnected {
        didSet { if state != oldValue { onStateChange?(state) } }
    }
    var onStateChange: ((State) -> Void)?
    /// Called after every (re)connect, so the owner can re-send `start`.
    var onConnected: (() -> Void)?

    private let url: URL
    private let urlSession: URLSession
    private let continuation: AsyncStream<ServerMessage>.Continuation
    private let log = Logger(subsystem: "commonground", category: "socket")

    private var task: URLSessionWebSocketTask?
    private var receiveLoop: Task<Void, Never>?
    private var reconnectTask: Task<Void, Never>?
    private var wantsConnection = false
    private var attempt = 0

    /// - Parameter host: "192.168.1.20:8000" or "localhost:8000"
    init(host: String) {
        var trimmed = host.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.hasPrefix("ws://") || trimmed.hasPrefix("wss://") {
            // already a full URL prefix
        } else {
            trimmed = "ws://" + trimmed
        }
        url = URL(string: trimmed + "/ws/session") ?? URL(string: "ws://localhost:8000/ws/session")!

        let config = URLSessionConfiguration.default
        config.waitsForConnectivity = true
        urlSession = URLSession(configuration: config)

        var cont: AsyncStream<ServerMessage>.Continuation!
        messages = AsyncStream(bufferingPolicy: .bufferingNewest(256)) { cont = $0 }
        continuation = cont
    }

    // MARK: Lifecycle

    func connect() {
        wantsConnection = true
        attempt = 0
        open()
    }

    func disconnect() {
        wantsConnection = false
        reconnectTask?.cancel()
        reconnectTask = nil
        closeTask()
        state = .disconnected
    }

    private func open() {
        guard wantsConnection, task == nil else { return }
        state = .connecting
        let ws = urlSession.webSocketTask(with: url)
        ws.maximumMessageSize = 4 * 1024 * 1024
        task = ws
        ws.resume()

        receiveLoop = Task { [weak self] in
            await self?.runReceiveLoop(on: ws)
        }
    }

    private func closeTask() {
        receiveLoop?.cancel()
        receiveLoop = nil
        task?.cancel(with: .normalClosure, reason: nil)
        task = nil
    }

    private func runReceiveLoop(on ws: URLSessionWebSocketTask) async {
        // URLSessionWebSocketTask has no "opened" callback without a delegate; the first
        // successful receive (or a ping) is the practical signal. Ping first so that a
        // server that stays silent until `start` still counts as connected.
        do {
            try await ping(ws)
            attempt = 0
            state = .connected
            onConnected?()
        } catch {
            log.error("connect failed: \(error.localizedDescription)")
            await handleDrop()
            return
        }

        while !Task.isCancelled {
            do {
                let frame = try await ws.receive()
                switch frame {
                case .string(let text):
                    decodeAndYield(Data(text.utf8))
                case .data(let data):
                    decodeAndYield(data)
                @unknown default:
                    break
                }
            } catch {
                if Task.isCancelled { return }
                log.error("receive failed: \(error.localizedDescription)")
                await handleDrop()
                return
            }
        }
    }

    private func ping(_ ws: URLSessionWebSocketTask) async throws {
        try await withCheckedThrowingContinuation { (c: CheckedContinuation<Void, Error>) in
            ws.sendPing { error in
                if let error { c.resume(throwing: error) } else { c.resume() }
            }
        }
    }

    private func decodeAndYield(_ data: Data) {
        do {
            let message = try WireCoding.decoder.decode(ServerMessage.self, from: data)
            continuation.yield(message)
        } catch {
            log.warning("undecodable server frame: \(error.localizedDescription)")
        }
    }

    private func handleDrop() async {
        task = nil
        receiveLoop = nil
        guard wantsConnection else {
            state = .disconnected
            return
        }
        state = .connecting
        scheduleReconnect()
    }

    private func scheduleReconnect() {
        reconnectTask?.cancel()
        let delay = min(30.0, 0.5 * pow(2.0, Double(attempt))) + Double.random(in: 0...0.3)
        attempt += 1
        log.info("reconnecting in \(delay, format: .fixed(precision: 1))s (attempt \(self.attempt))")
        reconnectTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(delay))
            guard !Task.isCancelled else { return }
            self?.open()
        }
    }

    // MARK: Sending

    func send(_ message: ClientMessage) async {
        guard let task, state == .connected else { return }
        do {
            let data = try WireCoding.encoder.encode(message)
            try await task.send(.string(String(decoding: data, as: UTF8.self)))
        } catch {
            log.error("send json failed: \(error.localizedDescription)")
        }
    }

    /// Raw PCM16 mono 16 kHz little-endian bytes, as a binary frame.
    func send(pcm: Data) async {
        guard let task, state == .connected else { return }
        do {
            try await task.send(.data(pcm))
        } catch {
            log.error("send pcm failed: \(error.localizedDescription)")
        }
    }
}
