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

    private enum Phase {
        case inactive
        case watching
        case startInFlight
    }

    private var phase = Phase.inactive

    mutating func prepare(for state: TelegramCredentials.State) -> Action {
        guard !state.allowsEngineStart else { return .start }
        phase = .watching
        return .watch
    }

    mutating func credentialChanged(
        to state: TelegramCredentials.State, health: EngineHealth
    ) -> Action {
        guard phase != .inactive, state.allowsEngineStart else { return .none }
        guard phase != .startInFlight else { return .none }
        switch health {
        case .notStarted, .cannotSpawn(.credentials):
            return beginStart()
        case .cannotSpawn(.command), .cannotSpawn(.launch):
            return finishRecovery()
        case .running:
            return finishRecovery()
        case .restarting, .stopped, .shutDown:
            return .none
        }
    }

    mutating func engineChanged(
        to health: EngineHealth, credentialState: TelegramCredentials.State
    ) -> Action {
        guard phase != .inactive else { return .none }
        switch health {
        case .running:
            return finishRecovery()
        case .cannotSpawn(.credentials):
            phase = .watching
            guard credentialState.allowsEngineStart else { return .none }
            return beginStart()
        case .cannotSpawn(.command), .cannotSpawn(.launch):
            return finishRecovery()
        case .notStarted, .restarting, .stopped, .shutDown:
            return .none
        }
    }

    private mutating func beginStart() -> Action {
        phase = .startInFlight
        return .start
    }

    private mutating func finishRecovery() -> Action {
        phase = .inactive
        return .stopWatching
    }

    mutating func cancel() {
        phase = .inactive
    }
}
