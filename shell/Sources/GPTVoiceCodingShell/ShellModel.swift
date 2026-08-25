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

    let panel: ControlPanel
    let loginItem = LoginItem()

    private let supervisor: EngineSupervisor

    init() {
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
        location = resolved
        locationFailure = failure
        panel = ControlPanel(client: UnixSocketControlPlane(path: resolved.socketPath))

        let configPath = resolved.configPath
        let resources = Bundle.main.resourceURL
        let supervisor = EngineSupervisor(
            launcher: ProcessLauncher(),
            socketPath: resolved.socketPath,
            resolveCommand: {
                try EngineCommand.resolve(resources: resources, configPath: configPath)
            })
        self.supervisor = supervisor

        Task { await self.begin() }
    }

    private func begin() async {
        await reconcileInstallation()
        await supervisor.observe { [weak self] health in
            Task { @MainActor in await self?.healthChanged(health) }
        }
        await supervisor.start()
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
        engineOutput = await supervisor.lines()
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
            engineOutput = await supervisor.lines()
            try? await Task.sleep(for: Self.readInterval)
        }
    }

    func retryEngine() async {
        await supervisor.retry()
    }

    /// Whether the child has already been asked to stop, so the terminate hook
    /// does not ask twice.
    private(set) var stopping = false

    /// Stop the engine in order. `SIGTERM` leaves no socket debris for the next
    /// start to trip over.
    func stopEngine() async {
        stopping = true
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
