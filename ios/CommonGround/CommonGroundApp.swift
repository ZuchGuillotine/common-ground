import SwiftUI

#if canImport(MWDATCore)
import MWDATCore
#endif

@main
struct CommonGroundApp: App {
    @State private var model = SessionViewModel()

    init() {
        #if canImport(MWDATCore)
        // Must run once at launch, before any other Wearables call. Reads the MWDAT dict in Info.plist.
        do {
            try Wearables.configure()
        } catch {
            assertionFailure("Wearables.configure failed: \(error)")
        }
        #endif
    }

    var body: some Scene {
        WindowGroup {
            RootView(model: model)
                .onOpenURL { url in
                    #if canImport(MWDATCore)
                    // Meta AI deep-links back after registration / permission prompts.
                    guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
                          components.queryItems?.contains(where: { $0.name == "metaWearablesAction" }) == true
                    else { return }
                    Task {
                        do {
                            _ = try await Wearables.shared.handleUrl(url)
                        } catch {
                            print("Wearables.handleUrl failed: \(error)")
                        }
                    }
                    #endif
                }
        }
    }
}

struct RootView: View {
    @Bindable var model: SessionViewModel

    var body: some View {
        NavigationStack {
            switch model.phase {
            case .setup:
                SetupView(model: model)
            case .live:
                LiveView(model: model)
            }
        }
    }
}
