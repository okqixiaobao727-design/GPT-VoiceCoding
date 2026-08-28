import ShellCore
import SwiftUI

/// About five idle rows at the default font; waiting rows are taller, so height
/// rather than row count is the one bound that keeps the roster honest.
private let rosterMaxHeight: CGFloat = 220

/// The v0 Control Panel: this dropdown, beside `bridgectl`. Runtime state, switch
/// flips and the shell-owned Companion Channel credential — it is not an editor
/// for installation settings.
struct ControlPanelView: View {
    @Bindable var shell: ShellModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            // Above the health row, because it is the one thing that explains a
            // green engine that still finds no coding agent. The row underneath
            // will say "running", truthfully, and be no help at all.
            if let failure = shell.pathFailure {
                LoginShellPathPanel(detail: failure)
            }

            TelegramCredentialRow(shell: shell)

            EngineHealthRow(health: shell.health)

            // A configuration that could not be read comes first. It is the
            // reason a socket may be the wrong one, and left underneath, it
            // would read as an engine that is missing rather than a file that is
            // wrong.
            if let failure = shell.locationFailure {
                ConfigurationPanel(detail: failure)
            }

            // And an installation that did not land, for the same reason: a
            // Session that cannot be reached looks like a broken engine until
            // you know the hook never got installed into its config directory.
            // Reported, never edited here — see this file's own first line.
            if let failure = shell.installationFailure {
                ConfigurationPanel(detail: failure)
            }

            // The Live Toggle is rendered whatever `status` did. It needs no
            // status to press — it is one action, and Bridge Core decides what
            // it means — and a control-plane action that disappears when a read
            // fails is ADR 0002's gated control plane in visual form. Pressing
            // it with no engine there is exactly the third state this ticket
            // asks to be verifiable: an honest failure.
            LiveToggle(callIsUp: shell.panel.callIsUp, panel: shell.panel)

            switch shell.panel.reading {
            case .notYetRead:
                ProgressView().controlSize(.small)
            case .read(let status):
                StatusSection(status: status, panel: shell.panel)
            case .failed(let failure):
                FailurePanel(failure: failure)
            }

            // Only when it is telling the user something the panel above did
            // not. When an action and the re-read after it failed the same way,
            // that is one piece of news, and saying it twice reads as two.
            if let failure = shell.panel.lastFailure, .failed(failure) != shell.panel.reading {
                Divider()
                FailurePanel(failure: failure)
            }

            if !shell.engineOutput.isEmpty {
                Divider()
                EngineOutputPanel(lines: shell.engineOutput)
            }

            if let seams = shell.panel.seams {
                Divider()
                SeamsPanel(seams: seams)
            }

            Divider()
            Footer(shell: shell)
        }
        .padding(14)
        .frame(width: 340)
        // Read on open, and keep reading only while this is on screen. The task
        // is cancelled when the dropdown closes, which is the whole of the
        // polling policy.
        .task { await shell.readWhileOpen() }
    }
}

private struct TelegramCredentialRow: View {
    @Bindable var shell: ShellModel
    @State private var editing = false
    @State private var saving = false
    @State private var token = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Telegram bot token: \(status)")
                Spacer()
                Button("Set…") {
                    shell.clearCredentialSaveFailure()
                    editing = true
                }
                .disabled(saving)
            }

            if editing {
                SecureField("Telegram bot token", text: $token)
                    .textFieldStyle(.roundedBorder)
                    .disabled(saving)
                HStack {
                    Button("Save") {
                        Task {
                            saving = true
                            let saved = await shell.saveTelegramToken(token)
                            saving = false
                            if saved {
                                token = ""
                                editing = false
                            }
                        }
                    }
                    .disabled(saving)
                    Button("Cancel") {
                        token = ""
                        shell.clearCredentialSaveFailure()
                        editing = false
                    }
                    .disabled(saving)
                }
            }

            if let failure {
                Text(failure)
                    .font(.caption).foregroundStyle(.red).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private var status: String {
        switch shell.credentialState {
        case .ready: return "set"
        case .notConfigured: return "not configured"
        case .missing: return "not set"
        case .unsafe: return "unsafe"
        case .unreadable: return "unreadable"
        }
    }

    private var failure: String? {
        shell.credentialSaveFailure ?? shell.credentialState.failureDetail
    }
}

/// Process parenthood, stated as itself. Never merged with what the control
/// plane said: "there is no engine" and "the engine refused" are different news.
private struct EngineHealthRow: View {
    let health: EngineHealth

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(colour).frame(width: 8, height: 8)
            Text(sentence).font(.headline)
        }
    }

    private var sentence: String {
        switch health {
        case .notStarted: return "The engine has not been started"
        case .running(let pid): return "The engine is running (pid \(pid))"
        case .restarting(let after, let attempt):
            return "The engine died; restarting in \(Int(after))s (attempt \(attempt))"
        case .stopped(.repeatedFailures(let attempts)):
            return "The engine keeps failing to start (\(attempts) attempts)"
        case .stopped(.anotherEngineIsListening):
            return "Another engine is already running; this shell did not start it"
        case .cannotSpawn: return "There is no engine to start"
        case .shutDown: return "The engine was stopped"
        }
    }

    private var colour: Color {
        switch health {
        case .running: return .green
        case .restarting: return .orange
        case .stopped, .cannotSpawn: return .red
        case .notStarted, .shutDown: return .secondary
        }
    }
}

/// The Live Toggle: one action, the same one `bridgectl live` calls. Bridge Core
/// decides whether pressing it starts or ends a call; nothing here holds call
/// state, which is how two toggles once opened two calls.
private struct LiveToggle: View {
    /// What Bridge Core last said, or nil when it has not been asked. The label
    /// follows the reading; it never guesses from something else.
    let callIsUp: Bool?
    let panel: ControlPanel

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Button {
                Task { await panel.toggleLive() }
            } label: {
                Label(title, systemImage: callIsUp == true ? "phone.down.fill" : "phone.fill")
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .disabled(panel.busy)

            if let live = panel.live {
                // Rendered as the engine sent it, not as this surface concluded.
                Text("live: \(live.state)\(live.callID.map { " · \($0)" } ?? "")")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }

    private var title: String {
        switch callIsUp {
        case true: return "End the Live Call"
        case false: return "Start a Live Call"
        // Unknown, because nothing has answered. Saying "start" would be this
        // surface deciding what the engine is going to do with the press.
        case nil: return "Live Toggle"
        }
    }
}

private struct StatusSection: View {
    let status: EngineStatus
    let panel: ControlPanel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            // Never gated by any switch, including the master (ADR 0002).
            ForEach(status.switches) { reading in
                Toggle(
                    reading.title,
                    isOn: Binding(
                        get: { reading.on },
                        set: { wanted in Task { await panel.flip(reading.name, on: wanted) } })
                )
                .toggleStyle(.switch)
                .disabled(panel.busy)
            }

            SessionRoster(status: status)

            Text(
                "\(status.sessions) sessions · \(status.pendingRelays) pending relays · "
                    + "\(status.pendingApprovals) pending approvals"
            )
            .font(.caption).foregroundStyle(.secondary)
        }
    }
}

/// The view-only roster: state read from Bridge Core, and no route back into it.
private struct SessionRoster: View {
    let status: EngineStatus

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Sessions").font(.caption).foregroundStyle(.secondary)
            if let empty = status.emptyRosterMessage {
                Text(empty).font(.callout)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 8) {
                        ForEach(status.sessionRows) { row in
                            SessionRosterRow(row: row)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: rosterMaxHeight)
            }
        }
    }
}

private struct SessionRosterRow: View {
    let row: SessionRow

    var body: some View {
        HStack(alignment: .top, spacing: 6) {
            if row.isChild {
                Image(systemName: "arrow.turn.down.right")
                    .font(.caption).foregroundStyle(.secondary)
            }
            VStack(alignment: .leading, spacing: 2) {
                Text(row.title).font(.callout).lineLimit(1)
                HStack(spacing: 4) {
                    Text(row.state)
                    Text("·")
                    if let lastActivity = row.lastActivity {
                        Text("active")
                        Text(lastActivity, style: .relative)
                    } else {
                        Text("no activity yet")
                    }
                }
                .font(.caption).foregroundStyle(.secondary)
                if let waiting = row.waitingMessage {
                    Text(waiting)
                        .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                }
            }
        }
        .padding(.leading, row.isChild ? 12 : 0)
    }
}

/// The three control-plane failure kinds, kept apart in the words themselves. A
/// refusal is Bridge Core speaking; unreachable and protocol mismatch are this
/// shell speaking about what happened, and each says so.
private struct FailurePanel: View {
    let failure: ActionFailure

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            switch failure {
            case .refused(let refusal):
                Text("Bridge Core refused (\(String(describing: refusal.code)))")
                    .font(.caption).foregroundStyle(.secondary)
                // Verbatim, and whole: a refusal truncated to one line is a
                // refusal the user was not actually told.
                Text(refusal.message)
                    .font(.callout).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            case .unreachable(let detail):
                Text("This shell could not reach the engine")
                    .font(.caption).foregroundStyle(.secondary)
                Text(detail)
                    .font(.callout).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            case .protocolMismatch(let detail):
                Text("Control-plane protocol mismatch")
                    .font(.caption).foregroundStyle(.secondary)
                Text(detail)
                    .font(.callout).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// The engine's own stderr, held in memory by the shell and shown as written.
///
/// The engine keeps its inherited stderr descriptor when it adopts its own log
/// (ADR 0004), and mirrors only the final exit-2 refusal sentence back to it.
private struct EngineOutputPanel: View {
    let lines: [String]

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("What the engine said").font(.caption).foregroundStyle(.secondary)
            ScrollView {
                Text(lines.joined(separator: "\n"))
                    .font(.system(.caption, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // A `ScrollView` has no height of its own, so without a floor this
            // panel renders as a heading over nothing — which reads as "the
            // engine said something and the shell will not show you what".
            .frame(minHeight: 44, maxHeight: 140)
        }
    }
}

private struct SeamsPanel: View {
    let seams: [SeamReading]

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("What the engine loaded").font(.caption).foregroundStyle(.secondary)
            ForEach(seams) { seam in
                HStack(alignment: .firstTextBaseline) {
                    Text(seam.seam).font(.caption)
                    Spacer()
                    Text(seam.outcome).font(.caption).foregroundStyle(.secondary)
                }
                if !seam.detail.isEmpty {
                    Text(seam.detail).font(.caption2).foregroundStyle(.secondary)
                }
            }
        }
    }
}

/// The user's own `PATH`, which the engine is running without.
///
/// Its own panel rather than an `EngineHealth` case: that type is process
/// parenthood and nothing inferred about the engine, and an engine on launchd's
/// truncated `PATH` is running perfectly well as a process. Before #118 this
/// went to the unified log alone, which is to say nowhere a user looks.
private struct LoginShellPathPanel: View {
    let detail: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("The engine is running without your PATH")
                .font(.caption).foregroundStyle(.secondary)
            Text(detail)
                .font(.callout).foregroundStyle(.red).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            // Not "press Retry": the state this panel exists for is a login
            // shell that timed out and an engine that then started anyway, and
            // in that state `Footer` renders no Retry button — it appears only
            // for `.stopped` and `.cannotSpawn` — and `EngineSupervisor.retry()`
            // would return at its own `guard !supervising` if it were pressed.
            // Telling somebody to press a button that is not on screen is worse
            // than telling them nothing.
            Text("The login shell is asked again at every engine start.")
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// A configuration the shell could not read. Its own kind of bad news: the
/// engine is not at fault and may not even have been asked.
private struct ConfigurationPanel: View {
    let detail: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("The engine's configuration could not be read")
                .font(.caption).foregroundStyle(.secondary)
            Text(detail)
                .font(.callout).foregroundStyle(.red).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Text("Falling back to the default socket, which may not be the one in use.")
                .font(.caption2).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct Footer: View {
    @Bindable var shell: ShellModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Button("Verify") { Task { await shell.panel.verify() } }
                if needsRetry {
                    Button("Retry") { Task { await shell.retryEngine() } }
                }
                Spacer()
                Button("Quit") { shell.quit() }
            }

            Toggle(
                "Launch at login",
                isOn: Binding(
                    get: { shell.loginItem.enabled },
                    set: { shell.loginItem.set($0) })
            )
            .toggleStyle(.checkbox)
            if let failure = shell.loginItem.failure {
                Text(failure).font(.caption).foregroundStyle(.red)
            }

            Text(shell.location.socketPath)
                .font(.system(.caption2, design: .monospaced))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
        }
    }

    /// The manual way out of a state the shell stopped in on purpose.
    private var needsRetry: Bool {
        switch shell.health {
        case .stopped, .cannotSpawn: return true
        default: return false
        }
    }
}
