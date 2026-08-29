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
        case stopWatching
        case watch
    }

    private var preflightHeldTheEngine = false
    private var recoveryStartInFlight = false

    mutating func prepare(for state: TelegramCredentials.State) -> Action {
        guard !state.allowsEngineStart else { return .start }
        preflightHeldTheEngine = true
        return .watch
    }

    mutating func credentialChanged(
        to state: TelegramCredentials.State, health: EngineHealth
    ) -> Action {
        guard preflightHeldTheEngine, state.allowsEngineStart else { return .none }
        guard !recoveryStartInFlight else { return .none }
        switch health {
        case .notStarted, .cannotSpawn:
            recoveryStartInFlight = true
            return .start
        case .running:
            return finishRecovery()
        case .restarting, .stopped, .shutDown:
            return .none
        }
    }

    mutating func engineChanged(
        to health: EngineHealth, credentialState: TelegramCredentials.State
    ) -> Action {
        guard preflightHeldTheEngine else { return .none }
        switch health {
        case .running:
            return finishRecovery()
        case .cannotSpawn:
            guard recoveryStartInFlight else { return .none }
            recoveryStartInFlight = false
            guard credentialState.allowsEngineStart else { return .none }
            return finishRecovery()
        case .notStarted, .restarting, .stopped, .shutDown:
            return .none
        }
    }

    private mutating func finishRecovery() -> Action {
        preflightHeldTheEngine = false
        recoveryStartInFlight = false
        return .stopWatching
    }

    mutating func cancel() {
        preflightHeldTheEngine = false
        recoveryStartInFlight = false
    }
}
