import AppKit
import Foundation
import Observation
import ShellCore

/// What the views read: the child's health on one side, the control plane on the
/// other, and nothing that mixes them.
///
/// Process parenthood and the control plane answer different questions. A process
/// being alive says nothing about whether its seams are filled, and a `status`
/// reply says nothing about whether the thing that answered is this shell's
/// child. Both are shown; neither is derived from the other.
@MainActor
@Observable
final class ShellModel {
    private(set) var health: EngineHealth = .notStarted
    private(set) var engineOutput: [String] = []
    private(set) var location: EngineLocation
    private(set) var locationFailure: String?
    /// What the launch reconcile said, when it did not go as asked. Shown, not
    /// acted on: the panel reports installation, it does not edit it.
    private(set) var installationFailure: String?
    /// Why the engine may be running on a `PATH` that finds no coding agent.
    ///
    /// It is the *last* spawn's outcome and not an accumulated grievance: set by
    /// a spawn whose login shell ran out of time, and cleared by the next spawn
    /// that ended any other way. It describes the child running now, so a stale
    /// warning over a child that was never the one it complained about is a state
    /// this cannot reach. Its own field beside the two above rather than an
    /// `EngineHealth` case: that type is process parenthood and nothing inferred
    /// about the engine, and an engine on the wrong `PATH` is a perfectly healthy
    /// process.
    private(set) var pathFailure: String?
    /// The shell's pre-spawn answer about its write-only Telegram credential.
    /// Kept apart from `EngineHealth`, which is process parenthood only.
    private(set) var credentialState: TelegramCredentials.State
    private(set) var credentialSaveFailure: String?

    let panel: ControlPanel
    let loginItem = LoginItem()

    private let supervisor: EngineSupervisor
    private let pathOutcomes: PathOutcomes
    private let credentials: TelegramCredentials
    private var credentialStartRecovery = CredentialStartRecovery()
    private var credentialFileObserver: CredentialFileObserver?
    /// Installation reconcile and initial preflight, in their required order.
    /// A token saved immediately after app launch waits for this rather than
    /// starting the engine ahead of installation.
    private var preparation: Task<Void, Never>?

    convenience init() {
        // The socket path is read from the same configuration the engine is
        // spawned with, never computed from the state path.
        var resolved = EngineLocation(
            configPath: EngineLocation.defaultConfigPath(),
            socketPath: EngineLocation.defaultSocketPath())
        var failure: String?
        do {
            resolved = try EngineLocation.resolve()
        } catch let problem as ConfigurationFailure {
            failure = problem.detail
        } catch {
            failure = "\(error)"
        }
        let credentials = TelegramCredentials(configPath: resolved.configPath)
        let panel = ControlPanel(client: UnixSocketControlPlane(path: resolved.socketPath))

        let configPath = resolved.configPath
        let resources = Bundle.main.resourceURL
        // Built here, before `self` exists to capture, so what the launcher
        // learns is left in a box this model reads afterwards rather than pushed
        // through a callback it cannot yet form.
        let outcomes = PathOutcomes()
        let supervisor = EngineSupervisor(
            launcher: ProcessLauncher(
                report: { outcomes.record($0) }, credentials: credentials),
            socketPath: resolved.socketPath,
            resolveCommand: {
                try EngineCommand.resolve(resources: resources, configPath: configPath)
            })
        self.init(
            location: resolved,
            locationFailure: failure,
            credentials: credentials,
            panel: panel,
            supervisor: supervisor,
            pathOutcomes: outcomes)

        preparation = Task { await self.begin() }
    }

    private init(
        location: EngineLocation,
        locationFailure: String?,
        credentials: TelegramCredentials,
        panel: ControlPanel,
        supervisor: EngineSupervisor,
        pathOutcomes: PathOutcomes
    ) {
        self.location = location
        self.locationFailure = locationFailure
        self.credentials = credentials
        credentialState = Self.preflight(credentials: credentials)
        credentialSaveFailure = nil
        self.panel = panel
        self.supervisor = supervisor
        self.pathOutcomes = pathOutcomes
        preparation = nil
    }

    /// The shell assembly seam. Production supplies the concrete process and
    /// socket adapters above; focused tests supply a supervised inert child.
    convenience init(
        location: EngineLocation,
        credentials: TelegramCredentials,
        panel: ControlPanel,
        supervisor: EngineSupervisor
    ) {
        self.init(
            location: location,
            locationFailure: nil,
            credentials: credentials,
            panel: panel,
            supervisor: supervisor,
            pathOutcomes: PathOutcomes())
    }

    private func begin() async {
        await reconcileInstallation()
        await startEngineAfterInstallation()
    }

    /// Start immediately or hold one recovery watch, after Installation has run.
    /// Split at this lifecycle boundary so tests never reconcile the real machine.
    func startEngineAfterInstallation() async {
        await supervisor.observe { [weak self] health in
            Task { @MainActor in await self?.healthChanged(health) }
        }
        await applyCredentialAction(credentialStartRecovery.prepare(for: credentialState))
    }

    /// The credential gate used at assembly and tested without starting the app.
    static func preflight(credentials: TelegramCredentials) -> TelegramCredentials.State {
        credentials.load().state
    }

    /// First launch is the install (ADR 0012), and every launch after it is a
    /// reconcile that writes nothing when the machine already agrees.
    ///
    /// It runs **before** the engine so that a Session started right after the
    /// app opens finds the hook already there. It does not gate the engine: a
    /// reconcile that failed costs reach into Sessions, and refusing to start
    /// over it would cost the control plane and the Live Call too.
    private func reconcileInstallation() async {
        let resources = Bundle.main.resourceURL
        let command: EngineCommand
        do {
            command = try EngineCommand.resolveInstallation(
                resources: resources, verb: Installation.reconcileVerb)
        } catch let problem as EngineCommandFailure {
            installationFailure = problem.detail
            return
        } catch {
            installationFailure = "\(error)"
            return
        }

        // Not `Task.detached`: the runner waits on a subprocess, and a detached
        // task doing that holds a cooperative-pool thread — one of about as many
        // as this machine has cores — for the whole run. The runner owns its own
        // threads and hands this one back; see its note.
        installationFailure = await InstallationRunner().run(command).failure
    }

    private func healthChanged(_ health: EngineHealth) async {
        self.health = health
        await readWhatTheLauncherLearned()
        await applyCredentialAction(
            credentialStartRecovery.engineChanged(
                to: health, credentialState: credentialState))
    }

    /// What the last spawn left behind: the engine's own words, and what asking
    /// the login shell came to.
    ///
    /// Read rather than pushed, and read at exactly the two moments the panel is
    /// about to be looked at — a health change, and each pass of the open
    /// dropdown. That is already how `engineOutput` reaches this model, and one
    /// mechanism read twice is easier to be right about than two.
    private func readWhatTheLauncherLearned() async {
        engineOutput = await supervisor.lines()
        pathFailure = pathOutcomes.latest?.reason
        credentialState = Self.preflight(credentials: credentials)
    }

    private func credentialSourceChanged() async {
        let state = Self.preflight(credentials: credentials)
        credentialState = state
        await applyCredentialAction(
            credentialStartRecovery.credentialChanged(to: state, health: health))
    }

    private func applyCredentialAction(_ action: CredentialStartRecovery.Action) async {
        switch action {
        case .none:
            return
        case .start:
            await supervisor.start()
        case .stopWatching:
            stopCredentialObservation()
        case .watch:
            do {
                credentialFileObserver = try CredentialFileObserver(
                    path: credentials.environmentPath,
                    changed: { [weak self] in
                        Task { @MainActor in await self?.credentialSourceChanged() }
                    })
            } catch {
                return
            }
            // Close the gap between the preflight read and arming the observer.
            await credentialSourceChanged()
        }
    }

    private func stopCredentialObservation() {
        credentialFileObserver?.cancel()
        credentialFileObserver = nil
    }

    /// How often the open dropdown re-reads. Slow enough that it is not a
    /// metronome, fast enough that a switch flipped elsewhere shows up while the
    /// user is still looking.
    static let readInterval: Duration = .seconds(1)

    /// Read on open, and keep reading only while the dropdown is open. No
    /// background timer: a poll nobody is looking at is a permanent entry in a
    /// bounded log whose value is measured in signal.
    func readWhileOpen() async {
        while !Task.isCancelled {
            await panel.refresh()
            await readWhatTheLauncherLearned()
            await applyCredentialAction(
                credentialStartRecovery.credentialChanged(
                    to: credentialState, health: health))
            try? await Task.sleep(for: Self.readInterval)
        }
    }

    func retryEngine() async {
        await supervisor.retry()
    }

    func clearCredentialSaveFailure() {
        credentialSaveFailure = nil
    }

    /// Replace the write-only token, then replace the child that inherited the
    /// old environment. Stopping completes before retry starts, so two engines
    /// never overlap on the one Telegram `getUpdates` consumer.
    func saveTelegramToken(_ token: String) async -> Bool {
        await preparation?.value
        do {
            let reading = try credentials.save(token: token)
            credentialState = reading.state
            credentialSaveFailure = nil
            credentialStartRecovery.cancel()
            stopCredentialObservation()
        } catch let failure as TelegramCredentialSaveFailure {
            credentialSaveFailure = failure.detail
            return false
        } catch {
            credentialSaveFailure = "Telegram credentials could not be saved: \(error)"
            return false
        }
        await supervisor.shutDown()
        await supervisor.retry()
        return true
    }

    /// Whether the child has already been asked to stop, so the terminate hook
    /// does not ask twice.
    private(set) var stopping = false

    /// Stop the engine in order. `SIGTERM` leaves no socket debris for the next
    /// start to trip over.
    func stopEngine() async {
        stopping = true
        credentialStartRecovery.cancel()
        stopCredentialObservation()
        await supervisor.shutDown()
    }

    /// Quit. The engine is stopped on the way out by the terminate hook, which
    /// is also what catches a quit that did not come from this menu.
    func quit() {
        NSApplication.shared.terminate(nil)
    }

    /// What the menu bar shows between opens — coarse, and driven by parenthood
    /// rather than by a poll.
    var symbol: String {
        switch health {
        case .running: return "waveform"
        case .restarting: return "arrow.triangle.2.circlepath"
        case .stopped, .cannotSpawn: return "exclamationmark.triangle"
        case .notStarted, .shutDown: return "waveform.slash"
        }
    }
}

/// The launcher's last word on the `PATH`, carried from whatever thread spawned
/// to this `@MainActor` model.
///
/// A box rather than a callback for two reasons: the launcher is built inside
/// `ShellModel.init`, before there is a `self` to hop back to, and the model
/// already reads the engine's stderr off the supervisor this way. Last one wins
/// and nothing accumulates — this answers "what is the child running now on",
/// which has exactly one answer.
private final class PathOutcomes: @unchecked Sendable {
    private let lock = NSLock()
    private var last: LoginShellPath.Outcome?

    func record(_ outcome: LoginShellPath.Outcome) { lock.withLock { last = outcome } }
    var latest: LoginShellPath.Outcome? { lock.withLock { last } }
}
