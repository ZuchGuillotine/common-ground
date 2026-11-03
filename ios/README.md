# Common Ground — iOS companion app

SwiftUI (iOS 17+) phone app for the Ray-Ban Meta coaching demo. Talks to the Python
backend over a WebSocket (`ws://HOST:8000/ws/session`): binary frames are raw PCM16
mono 16 kHz audio, text frames are JSON `ClientMessage` / `ServerMessage`
(see `backend/common_ground/schema.py`). There is no `.xcodeproj` checked in; create
one in Xcode and drop these files in.

## Create the project (5 min)

1. Xcode → File → New → Project → iOS **App**. Product name `CommonGround`,
   Interface SwiftUI, Language Swift, no Core Data / tests needed.
   Deployment target **iOS 17.2** (required by the Meta SDK).
2. Delete the generated `ContentView.swift` and `CommonGroundApp.swift`.
3. Drag the `ios/CommonGround` folder's contents into the project navigator
   (check *Copy items if needed* and *Create groups*):

   ```
   CommonGround/
     CommonGroundApp.swift
     Models/Protocol.swift
     Services/SessionSocket.swift
     Services/AudioInput.swift
     Services/CuePlayer.swift
     ViewModels/SessionViewModel.swift
     Views/SetupView.swift
     Views/LiveView.swift
     Views/TurnAnalysisView.swift
     Resources/demo_session.json     ← make sure it is in "Copy Bundle Resources"
   ```

4. Info (target → Info tab): add
   `NSMicrophoneUsageDescription` = "Listens to the conversation to coach you."
   Add Background Modes capability with **Audio** so the socket and mic survive the
   screen locking.
5. Target → Build Settings → *App Transport Security*: for a LAN backend over plain
   `ws://`, add `NSAppTransportSecurity > NSAllowsLocalNetworking = YES`
   (or `NSAllowsArbitraryLoads` for a hackathon).
6. If the project was created in Swift 6 language mode and strict-concurrency errors
   get in the way, set *Swift Language Version* to **Swift 5** for the target.

Build and run on a real iPhone (the simulator has no Bluetooth; the phone-mic path does
work in the simulator with the Mac's mic). Toggle **Demo mode** on the setup screen to
replay the bundled session without a server.

## Add the Meta Wearables SDK (only for the glasses-mic path)

Follow `docs/WEARABLES_SDK.md`. In short:

1. File → Add Package Dependencies… → `https://github.com/facebook/meta-wearables-dat-ios`
   → latest 1.0.x tag → add `MWDATCore` and `MWDATCamera` to the target.
2. Paste the Info.plist block from the doc (URL scheme `commonground`, `MWDAT` dict,
   background modes, `UISupportedExternalAccessoryProtocols`, Bluetooth / local network /
   camera strings).
3. All SDK code is wrapped in `#if canImport(MWDATCore)` so the app compiles with or
   without the package. `GlassesMicSource` in `Services/AudioInput.swift` has the
   `TODO(SDK)` markers; `CommonGroundApp.swift` has configure/registration/URL handling.
4. Enable Developer Mode in the Meta AI app, run, tap **Register with Meta AI**, grant
   camera + microphone, pick input **Glasses** on the setup screen.

## Pointing at the backend

Setup screen → *Server* field, default `localhost:8000`. On a phone use the Mac's LAN
IP, e.g. `192.168.1.20:8000`. The app sends `start` with the `SessionConfig` as soon
as the socket opens, re-sends it after a reconnect, and sends `stop` when you end.
