# Meta Wearables Device Access Toolkit (DAT) 1.0 — iOS spike notes

Goal of the spike (≈2 h): get **16 kHz mono PCM from the Ray-Ban Meta microphones into
`SessionSocket`** and get **short TTS cues out of the glasses speaker**, using the
official iOS SDK. If the experimental audio stream does not work on our hardware /
Meta AI build, fall back to the phone mic + glasses as a Bluetooth speaker (section 6).

Everything below was checked on 2026-11-03 against Meta's docs and the SDK repo. The
most precise source for the audio API is the SDK's own agent-skill file, because the
public API reference does **not** document `AudioFrame`:

* SDK repo: <https://github.com/facebook/meta-wearables-dat-ios>
* Audio-streaming skill (authoritative for the audio API):
  <https://github.com/facebook/meta-wearables-dat-ios/blob/main/plugins/mwdat-ios/skills/audio-streaming/SKILL.md>
* Camera-streaming skill: <https://github.com/facebook/meta-wearables-dat-ios/blob/main/plugins/mwdat-ios/skills/camera-streaming/SKILL.md>
* Getting-started skill (Info.plist block): <https://github.com/facebook/meta-wearables-dat-ios/blob/main/plugins/mwdat-ios/skills/getting-started/SKILL.md>
* Debugging / Developer Mode skill: <https://github.com/facebook/meta-wearables-dat-ios/blob/main/plugins/mwdat-ios/skills/debugging/SKILL.md>
* Integration guide: <https://wearables.developer.meta.com/docs/develop/dat/build-integration-ios/>
* Getting started (Developer Mode steps): <https://wearables.developer.meta.com/docs/develop/dat/getting-started-toolkit/>
* Microphones & speakers (HFP vs A2DP, AVAudioSession options): <https://wearables.developer.meta.com/docs/develop/dat/microphones-and-speakers/>
* API reference: `Stream` <https://wearables.developer.meta.com/docs/reference/ios_swift/dat/latest/mwdatcamera_stream>,
  `StreamConfiguration` <https://wearables.developer.meta.com/docs/reference/ios_swift/dat/latest/mwdatcamera_streamconfiguration>,
  `AudioCodec` <https://wearables.developer.meta.com/docs/reference/ios_swift/dat/latest/mwdatcamera_audiocodec>,
  `AudioSampleRate` <https://wearables.developer.meta.com/docs/reference/ios_swift/dat/latest/mwdatcamera_audiosamplerate>
* Version matrix: <https://wearables.developer.meta.com/docs/develop/dat/version-dependencies/>
* Docs MCP server (searchable docs, useful from Claude Code): `https://mcp.developer.meta.com/wearables`

## 0. The one-paragraph summary

Audio from the glasses is an **experimental extension of the camera stream**. You
create a `DeviceSession`, attach a `Camera` with a `StreamConfiguration` whose
`audioCodec` is `.pcm(sampleRate: .rate16000, numberOfChannels: 1)`, subscribe to
`stream.audioFramePublisher` **before** `stream.start()`, and you receive `AudioFrame`s
with `pcmBuffer` (Apple standard PCM, **Float32**, native endian) and a
`presentationTimeStamp`. The camera stream must be running for audio to flow. The
API is marked "Dev and beta only … not supported in production release channels", so
it is fine for the hackathon but cannot ship to the App Store. Speaker output is not
part of the SDK at all: it is plain Bluetooth (A2DP or HFP) via `AVAudioSession`.

## 1. Versions and prerequisites

| Thing | Requirement | Source |
|---|---|---|
| SDK | `meta-wearables-dat-ios` 1.0.x (tags on GitHub) | repo |
| Xcode / Swift / iOS | Xcode 26.4+, Swift 6.3+, deployment target iOS 17.2+ | getting-started skill |
| Meta AI app | v290+ for SDK 1.0.0, v292+ for 1.0.1 | version-dependencies |
| Ray-Ban Meta firmware | v128+ (Gen 1 and Gen 2 are not distinguished) | version-dependencies |
| Internet | Required during app registration | debugging skill |
| Developer Center | Camera **and** Audio Streaming approval on the app (Developer Mode lets you skip `MetaAppID` attestation for local testing) | audio-streaming skill, getting-started skill |

Check firmware in Meta AI: Devices tab → your glasses → Device settings → General → About → Version.

## 2. Spike checklist (≈2 h)

### Step 1 — Developer Mode in the Meta AI app (5 min)

1. Pair the glasses with the Meta AI app as usual.
2. Meta AI app → **Settings → App Info → tap the App version number five times** → a
   "Developer Mode" toggle appears → turn it on → confirm "Enable". (The SDK debugging
   skill describes it as Settings → your glasses → Developer Mode; depends on app
   version.) The glasses may restart.
3. Gotchas: Developer Mode is **per pair of glasses** and **switches itself off after a
   firmware update**. If a stream sits in `waitingForDevice`, re-check this first.
4. After registration, the app appears under Meta AI → Settings → App connections →
   Developer mode apps.

### Step 2 — Xcode project + package (10 min)

1. File → Add Package Dependencies… → `https://github.com/facebook/meta-wearables-dat-ios`
   → pick the latest 1.0.x tag → add **`MWDATCore`** and **`MWDATCamera`** to the app target.
2. Signing & Capabilities: real device only (Bluetooth Classic). Add **Hotspot
   Configuration** capability only if you want Wi-Fi high-bandwidth streaming (not needed
   for audio-only).
3. Set a custom URL scheme, e.g. `commonground`, in Info → URL Types, and keep it in sync
   with `MWDAT > AppLinkURLScheme`.

### Step 3 — Info.plist (10 min)

Exactly as in the getting-started skill, plus the audio entries at the end:

```xml
<key>CFBundleURLTypes</key>
<array><dict>
  <key>CFBundleTypeRole</key><string>Editor</string>
  <key>CFBundleURLName</key><string>$(PRODUCT_BUNDLE_IDENTIFIER)</string>
  <key>CFBundleURLSchemes</key><array><string>commonground</string></array>
</dict></array>

<key>MWDAT</key>
<dict>
  <key>AppLinkURLScheme</key><string>commonground://</string>
  <key>MetaAppID</key><string>0</string>          <!-- "0" / empty = Developer Mode, no attestation -->
  <key>ClientToken</key><string></string>
  <key>TeamID</key><string>$(DEVELOPMENT_TEAM)</string>
</dict>

<key>UIBackgroundModes</key>
<array>
  <string>processing</string>
  <string>bluetooth-central</string>
  <string>bluetooth-peripheral</string>
  <string>external-accessory</string>
  <string>audio</string>                          <!-- only because we record glasses audio -->
</array>
<key>UISupportedExternalAccessoryProtocols</key>
<array><string>com.meta.ar.wearable</string></array>
<key>NSBluetoothAlwaysUsageDescription</key><string>Connects to your Meta glasses.</string>
<key>NSLocalNetworkUsageDescription</key><string>Finds your glasses over Wi-Fi.</string>
<key>NSBonjourServices</key><array><string>_bonjour._tcp</string></array>
<key>NSMicrophoneUsageDescription</key><string>Listens to the conversation to coach you.</string>
<key>NSCameraUsageDescription</key><string>Required by the glasses audio stream.</string>
```

Bluetooth Classic (`external-accessory` + `com.meta.ar.wearable`) is what the camera /
audio stream uses; the others are needed for discovery. `NSSpeechRecognitionUsageDescription`
is not needed (we transcribe server-side).

### Step 4 — Configure, register, handle the callback (15 min)

`ios/CommonGround/CommonGroundApp.swift` already contains this behind `#if canImport(MWDATCore)`.

```swift
import MWDATCore
// at launch
try Wearables.configure()
// SwiftUI
.onOpenURL { url in
    guard let c = URLComponents(url: url, resolvingAgainstBaseURL: false),
          c.queryItems?.contains(where: { $0.name == "metaWearablesAction" }) == true else { return }
    Task { _ = try await Wearables.shared.handleUrl(url) }   // throws WearablesHandleURLError
}
// register (switches to the Meta AI app, which deep-links back)
try await Wearables.shared.startRegistration()
for await state in Wearables.shared.registrationStateStream() { /* .available .registering .registered .unavailable */ }
```

### Step 5 — Permissions (5 min)

```swift
let w = Wearables.shared
if try await w.checkPermissionStatus(.camera) != .granted {      // silent
    guard try await w.requestPermission(.camera) == .granted else { return }   // app-switches to Meta AI
}
if try await w.checkPermissionStatus(.microphone) != .granted {
    guard try await w.requestPermission(.microphone) == .granted else { return }
}
```

Tell the user the app will switch to Meta AI before calling `requestPermission`. Both
`.camera` and `.microphone` are required for audio streaming.

### Step 6 — Device session + camera stream with PCM audio (30 min)

This is what `GlassesMicSource` in `ios/CommonGround/Services/AudioInput.swift` does.

```swift
import MWDATCore
import MWDATCamera

let session = try Wearables.shared.createSession(deviceSelector: AutoDeviceSelector(wearables: Wearables.shared))
try session.start()                                   // typed throws(DeviceSessionError)
for await s in session.stateStream() { if s == .started { break } }

let config = StreamConfiguration(
    videoCodec: .raw,
    audioCodec: .pcm(sampleRate: .rate16000, numberOfChannels: 1),   // AudioCodec.pcm(AudioSampleRate, UInt32)
    resolution: .low,                                 // 360x640; lowest bandwidth
    frameRate: 2                                      // valid: 2, 7, 15, 24, 30
)
guard let camera = try session.addCamera(config: config) else { return }  // nil if session not .started
let stream = camera.stream

let tokens = ListenerTokenBag()
stream.audioFramePublisher.listen { frame in          // any Announcer<AudioFrame>
    consume(frame.pcmBuffer, at: frame.presentationTimeStamp)   // Float32 PCM; align with video via PTS
}.store(in: tokens)
stream.errorPublisher.listen { error in /* StreamError: .permissionDenied, .deviceNotConnected, .hingesClosed, .thermalHot, .batteryLow … */ }.store(in: tokens)
stream.statePublisher.listen { state in /* stopped → waitingForDevice → starting → streaming → paused */ }.store(in: tokens)
stream.start()
// teardown: tokens.clear(); camera.stop(); session.stop()
```

Verification targets for the spike:
- [ ] `stream.statePublisher` reaches `.streaming`.
- [ ] `audioFramePublisher` fires; log `pcmBuffer.format` (expect Float32, 16 000 Hz, 1 ch) and frame length.
- [ ] Converted Int16 chunks arrive at the server (`ws://HOST:8000/ws/session`) and the transcript appears.
- [ ] `presentationTimeStamp` is monotonic; record the first PTS to map to the server's session clock.

### Step 7 — Speaker output + echo masking (20 min)

See `CuePlayer.swift`. The SDK has no speaker API; output is normal Bluetooth audio:

```swift
let s = AVAudioSession.sharedInstance()
try s.setCategory(.playAndRecord, mode: .default, options: [.allowBluetoothA2DP])  // phone mic in, glasses out
// or, if you want the glasses' HFP mic: options: [.allowBluetoothHFP]  (.allowBluetooth on older SDKs)
try s.setActive(true)
```

Then `AVSpeechSynthesizer` speaks `Intervention.earpiece_cue`. The delegate's
`didStart` / `didFinish` timestamps are sent as `{"type":"cue_played","cue_window":[t0,t1]}`
so the server can mark turns with `coaching_overlap` and drop the cue's echo from the
transcript.

### Step 8 — Decide (5 min)

If Step 6 fails after 60 min (no `.streaming`, no frames, or frames but silence), switch
`SessionViewModel.inputMode` to `.phoneMic` (section 6) and move on. The rest of the app
is identical.

## 3. Audio API facts worth remembering

* `AudioCodec.pcm(AudioSampleRate, UInt32)` — "Apple Standard PCM (**Float32** native-endian)
  with specified sample rate and number of channels". Our server wants **Int16**, so convert
  (done in `AudioInput.swift`, `PCM16.data(from:)`).
* `AudioSampleRate`: `.rate16000`, `.rate44100`, `.rate48000` (`UInt`, `CaseIterable`).
* `Stream.audioFramePublisher: any Announcer<AudioFrame>`; `AudioFrame` has at least
  `pcmBuffer` and `presentationTimeStamp` (from the official skill; the public reference
  has no `AudioFrame` page, so confirm the concrete types with Xcode's jump-to-definition
  on day one — most likely `AVAudioPCMBuffer` and `CMTime`).
* Audio is **not beamformed** on the camera stream ("ambient noise stays at natural
  levels"). That is exactly what we want: the counterpart must be audible. The HFP mic,
  by contrast, is beamformed to the wearer's voice.
* Register the listener **before** `stream.start()`; retain the token (`ListenerTokenBag`)
  on a long-lived owner; cancel listeners before `camera.stop()`; stopping the camera
  invalidates the stream. Audio has no separate lifecycle: errors come via `stream.errorPublisher`.
* MockDeviceKit can negotiate an audio-enabled stream but **does not inject PCM frames**:
  you need real glasses to test audio.
* Status: "Audio Streaming is experimental. Apps can use it for development and beta
  testing, but cannot publish it to production release channels yet." (`StreamConfiguration`
  with `audioCodec:` and `audioFramePublisher` are both tagged "Dev and beta only".)

## 4. Known gotchas

1. **Camera session must be live for audio.** There is no audio-only capability; audio
   rides on `Camera.stream`. Use `.low` resolution and `frameRate: 2` to minimise
   Bluetooth bandwidth and battery; ignore `videoFramePublisher`. The privacy LED on the
   glasses will be on — say so to the counterpart (it also supports our consent story).
2. **One session per device; the stream pauses on gestures.** A single touchpad tap pauses
   an active stream (tap again to resume); streams started with glasses doffed pause when
   donned. Another app's session, folding the glasses, or "Hey Meta" also pauses/stops it.
   Keep the UI showing `paused` instead of restarting.
3. **Permissions are two-level.** App registration (Meta AI) and then per-capability
   permission (`.camera`, `.microphone`), each of which app-switches to Meta AI and deep-links
   back through our URL scheme. If the deep link does not return, the URL scheme and
   `MWDAT > AppLinkURLScheme` disagree.
4. **Bluetooth routing for TTS cues.** HFP and A2DP are mutually exclusive on the glasses.
   `.allowBluetoothA2DP` gives stereo 44.1/48 kHz output and keeps the phone's mic as
   input; `.allowBluetoothHFP` turns on the glasses' mic and drops output to mono voice
   quality. Meta's docs: "Starting the Device Access Toolkit stream before HFP is ready can
   cause the audio route to fail silently" and "If the route changes, the HFP sample rate
   can change and the engine stops" — so configure/activate `AVAudioSession` **before**
   starting the DAT stream, observe `AVAudioSession.routeChangeNotification`, and verify
   the current route contains a `.bluetoothA2DP` / `.bluetoothHFP` output port before
   relying on the glasses for playback. Whether the DAT audio stream coexists with an
   active HFP link is unverified — test it; if it conflicts, use A2DP for output (our
   default) because the DAT stream, not HFP, carries the mic.
5. **Mic echo of cues into the transcript.** The glasses mic will hear the glasses
   speaker. Mitigations, in order: keep cues ≤ 4 words (schema already enforces), send
   `cue_played` with the exact `[start, end]` window (server marks `Turn.coaching_overlap`
   and the ASR filters that span), and keep `min_seconds_between_cues` ≥ 20 s. Do not rely
   on `AVAudioSession.Mode.voiceChat` echo cancellation: it applies to the phone's own
   capture path, not to the DAT stream.
6. **Timebase.** Server timestamps (`Turn.t_start`, `cue_window`) are seconds on the
   session clock. The phone defines the clock as time since the `start` message; the
   server should derive audio time from sample count (16 000 samples = 1 s), so the two
   drift only by Bluetooth latency (~100–300 ms). Good enough for echo masking; keep the
   window generous (+0.5 s after `didFinish`).
7. **Swift 6 strict concurrency.** The SDK's publishers call back on arbitrary threads;
   hop to `@MainActor` with `Task { @MainActor in … }` before touching UI state. If the
   fresh Xcode project defaults to the Swift 6 language mode and you get stuck on
   isolation errors during the hackathon, set *Swift Language Version* to 5 for the app target.
8. **Thermal / battery.** `StreamError.thermalHot`, `.batteryLow`, and
   `DeviceSessionError.thermalCritical` etc. end streams; the glasses get warm after
   10–15 min of streaming. Keep demos short and charge the case.

## 5. What the SDK does *not* give us

* No speaker/TTS API — use `AVSpeechSynthesizer` over Bluetooth.
* No audio without video, no Int16 PCM (Float32 only), no beamforming control.
* No way to ship the audio feature to the App Store today (TestFlight/dev builds only).
* `MWDATSpeech` offers on-device transcription of the glasses mic (`TranscriptionResult`),
  but it is wearer-oriented and not what we need for counterpart analysis.

## 6. Fallback plan — phone mic in, glasses out

Zero SDK risk; ~15 min. `SessionViewModel.inputMode = .phoneMic` already does this:

1. `AVAudioSession`: `.playAndRecord`, `.default` mode, options `[.allowBluetoothA2DP]`.
   Input = iPhone built-in mic (place the phone on the table between the speakers; it
   hears both parties better than any glasses mic). Output = glasses over A2DP.
2. `PhoneMicSource` taps `AVAudioEngine.inputNode` and resamples to 16 kHz mono Int16
   with `AVAudioConverter`; `SessionSocket.send(pcm:)` ships 100 ms binary frames.
3. Cues still play through the glasses, and `cue_played` masking still works. The
   phone mic also hears the glasses speaker (quieter than the glasses mic does), so the
   masking stays useful.
4. If the glasses are not available at all, the phone speaker/AirPods work identically;
   only the "private earpiece" story is weaker.

Demo-only safety net: `demoMode` replays `Resources/demo_session.json` through the same
view model, so the UI can be shown with no server and no glasses.
