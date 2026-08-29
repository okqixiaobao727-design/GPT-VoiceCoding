import ShellCore

/// The one-shot decision between credential preflight and engine supervision.
///
/// A blocked launch stays eligible until the same credential source becomes
/// usable. Once consumed, later file observations cannot become an implicit
/// restart path for a running engine.
struct CredentialStartRecovery {
    enum Action: Equatable {
        case none
        case start
        case watch
    }

    private var preflightHeldTheEngine = false

    mutating func prepare(for state: TelegramCredentials.State) -> Action {
        guard !state.allowsEngineStart else { return .start }
        preflightHeldTheEngine = true
        return .watch
    }

    mutating func credentialChanged(
        to state: TelegramCredentials.State, health: EngineHealth
    ) -> Action {
        guard preflightHeldTheEngine, state.allowsEngineStart else { return .none }
        preflightHeldTheEngine = false
        guard health == .notStarted else { return .none }
        return .start
    }

    mutating func cancel() {
        preflightHeldTheEngine = false
    }
}
