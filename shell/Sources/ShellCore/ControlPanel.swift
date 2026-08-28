import Foundation
import Observation

/// How an ask ended when it did not bring back an answer.
///
/// Three of the four failure kinds this surface must keep apart. The fourth —
/// there is no engine process at all — is process parenthood and lives in
/// ``EngineHealth``, because "the engine refused", "nothing answered" and "there
/// is no engine" are different sentences. So is "the engine answered in an
/// unsupported protocol", and a merged rendering would hide the one fact that
/// tells the user which side needs updating.
public enum ActionFailure: Equatable, Sendable {
    /// **(a)** Bridge Core answered, and answered no. Its own words, verbatim.
    case refused(Refusal)
    /// **(b)** Nothing answered. Raised here, never sent by the engine.
    case unreachable(String)
    /// **(c)** The engine answered in a protocol this shell cannot interpret.
    case protocolMismatch(String)
}

/// What the last `status` read found.
public enum StatusReading: Equatable, Sendable {
    case notYetRead
    case read(EngineStatus)
    case failed(ActionFailure)
}

/// The `status` reply, as far as this dropdown renders it. Every fact is read
/// from Bridge Core. The only projection is display order: a Child Process is
/// placed under the parent address the wire carries, never classified here.
public struct EngineStatus: Equatable, Sendable {
    public var switches: [SwitchReading]
    /// `null` when the system owns no call. The presence of the id is what the
    /// reply says; "up" is not a conclusion this surface draws from elsewhere.
    public var callID: String?
    /// Live rows shown by the roster. Ended rows remain engine history, not
    /// Sessions a person can see running now.
    public var sessions: Int
    public var sessionRows: [SessionRow]
    public var pendingRelays: Int
    public var pendingApprovals: Int

    public var callIsUp: Bool { callID != nil }
    public var emptyRosterMessage: String? {
        sessionRows.isEmpty ? "No live Sessions" : nil
    }

    public init(document: [String: JSONValue]) {
        switches = SwitchReading.canonicalOrder.compactMap { name in
            document["switches"]?[name]?.bool.map { SwitchReading(name: name, on: $0) }
        }
        callID = document["call_id"]?.string
        let rows = (document["sessions"]?.array ?? []).map(SessionRow.init).filter {
            $0.lifecycle == "live"
        }
        sessionRows = Self.parentsBeforeChildren(rows)
        sessions = sessionRows.count
        pendingRelays = document["pending_relays"]?.array?.count ?? 0
        pendingApprovals = document["pending_approvals"]?.array?.count ?? 0
    }

    /// A stable hierarchy projection over the wire's order.
    ///
    /// Main Sessions keep their relative order; every known child follows its
    /// parent and its siblings keep theirs. A Child Process whose parent is not
    /// present remains visible at the end. Nothing is ordered by state.
    private static func parentsBeforeChildren(_ rows: [SessionRow]) -> [SessionRow] {
        let mainRows = rows.filter { !$0.isChild }
        let mainTargets = Set(mainRows.map(\.target))
        let nested = mainRows.flatMap { parent in
            [parent] + rows.filter { $0.isChild && $0.parent == parent.target }
        }
        return nested
            + rows.filter {
                $0.isChild && $0.parent.map(mainTargets.contains) != true
            }
    }
}

/// The wire address that identifies one Session and links a Child Process to its parent.
public struct SessionAddress: CustomStringConvertible, Equatable, Hashable, Sendable {
    public var agent: String
    public var sessionID: String?
    public var pid: Int?

    public var description: String {
        let process = pid.map { ":\($0)" } ?? ""
        return "\(agent):\(sessionID ?? "")\(process)"
    }

    init(_ value: JSONValue) {
        agent = value["agent"]?.string ?? ""
        sessionID = value["session_id"]?.string
        pid = value["pid"]?.number.map(Int.init)
    }
}

/// One Session roster row, carrying only facts Bridge Core already reported.
public struct SessionRow: Equatable, Identifiable, Sendable {
    public var target: SessionAddress
    public var name: String?
    public var lifecycle: String
    public var state: String
    public var lastActivity: Date?
    public var waitingKind: String?
    public var isChild: Bool
    public var parent: SessionAddress?

    public var id: SessionAddress { target }
    public var title: String {
        if isChild { return "Child Process" }
        return name ?? target.description
    }
    public var waitingMessage: String? {
        guard state == "waiting", let waitingKind else { return nil }
        switch waitingKind {
        case "question", "permission": return "Waiting for \(waitingKind)"
        default: return nil
        }
    }

    init(_ value: JSONValue) {
        target = SessionAddress(value["target"] ?? .null)
        name = value["name"]?.string
        lifecycle = value["lifecycle"]?.string ?? ""
        state = value["state"]?.string ?? ""
        lastActivity = Self.readDate(value["last_activity"]?.string)
        waitingKind = value["waiting_for"]?["kind"]?.string
        isChild = value["child"]?["kind"]?.string == "child"
        if let parentValue = value["child"]?["parent"], !parentValue.isNull {
            parent = SessionAddress(parentValue)
        } else {
            parent = nil
        }
    }

    private static func readDate(_ text: String?) -> Date? {
        guard let text else { return nil }
        if let date = try? Date.ISO8601FormatStyle(includingFractionalSeconds: true).parse(text) {
            return date
        }
        return try? Date.ISO8601FormatStyle().parse(text)
    }
}

public struct SwitchReading: Equatable, Sendable, Identifiable {
    /// The three the engine registers, in the order the Language lists them:
    /// Duty is the master, and the other two are effective only while it is on.
    public static let canonicalOrder = ["duty", "voice", "message"]

    public var name: String
    public var on: Bool
    public var id: String { name }

    /// The Language's own words, so the dropdown never invents a second name for
    /// a switch that already has one.
    public var title: String {
        switch name {
        case "duty": return "Duty Switch"
        case "voice": return "Voice Switch"
        case "message": return "Message Switch"
        default: return name
        }
    }
}

/// What the Live Toggle last reported. `state` is rendered as the engine sent it.
public struct LiveReading: Equatable, Sendable {
    public var state: String
    public var callID: String?
}

/// The Control Panel: in v0 this dropdown *is* it, beside `bridgectl`.
///
/// It holds no policy and no state of its own. Every value it shows is read from
/// Bridge Core over the same JSON-over-UDS control plane every other surface
/// speaks, and every action it offers is one the control plane already
/// publishes — there is no private protocol here, and no second path to
/// anything.
///
/// It reads on demand: when the dropdown opens, while it stays open, and after
/// every action. No background timer — a poll nobody is reading is a permanent
/// metronome in a bounded log whose value is measured in signal.
@MainActor
@Observable
public final class ControlPanel {
    public private(set) var reading: StatusReading = .notYetRead
    /// How the last thing the user asked for ended, or nil when it worked. Held
    /// apart from ``reading`` so the re-read that follows an action cannot erase
    /// the refusal that action earned.
    public private(set) var lastFailure: ActionFailure?
    public private(set) var live: LiveReading?
    /// The last `verify` answer, when the user asked for one. Never on a timer:
    /// it asks every seam about itself, which is a question, not a heartbeat.
    public private(set) var seams: [SeamReading]?
    public private(set) var busy = false

    /// Whether the system owns a call, or nil when nothing has said.
    ///
    /// `status` wins whenever there is one, because every toggle is followed by a
    /// re-read and because *another* surface may have ended the call since. A
    /// panel that kept preferring its own last Live Toggle answer would be
    /// holding call state — the thing that once let two toggles open two calls —
    /// and it would go stale silently, which is worse than being wrong loudly.
    ///
    /// The Live Toggle's own reply is used only before any status has landed. Its
    /// `state` is still rendered verbatim beside the button, which is where
    /// `connecting` — a thing `call_id` cannot express — remains visible.
    public var callIsUp: Bool? {
        if case .read(let status) = reading { return status.callIsUp }
        if let live { return live.state == "up" }
        return nil
    }

    private let client: ControlPlaneDialing

    public init(client: ControlPlaneDialing) {
        self.client = client
    }

    /// Read `status`. Cheap, and never gated by any switch (ADR 0002) — the
    /// dropdown works from a machine with Duty off, which is the whole point.
    public func refresh() async {
        switch await ask(Request(action: .status), { EngineStatus(document: $0) }) {
        case .answered(let status): reading = .read(status)
        case .failed(let failure): reading = .failed(failure)
        }
    }

    /// Flip one switch, then re-read, so what is shown is what Bridge Core holds
    /// rather than what this surface just asked for.
    public func flip(_ name: String, on: Bool) async {
        let outcome = await ask(
            Request(action: .switch, payload: ["name": .string(name), "on": .bool(on)])
        ) { $0 }
        await refresh()
        // Recorded after the re-read, never before: a switch Bridge Core refused
        // is news the user is owed even when the next read succeeds.
        lastFailure = outcome.failure
    }

    /// The Live Toggle — one action, the same one `bridgectl live` calls.
    ///
    /// Bridge Core owns the policy: it ends the call the system owns, or starts
    /// one if none is up. This surface holds no call state and never decides
    /// which of the two is happening; a surface that did is how two toggles once
    /// opened two calls.
    public func toggleLive() async {
        let outcome = await ask(Request(action: .live)) {
            LiveReading(state: $0["state"]?.string ?? "", callID: $0["call_id"]?.string)
        }
        // Only what the engine sent. A failed toggle leaves the last reading
        // alone rather than guessing which way the call went.
        if let answer = outcome.answer { live = answer }
        await refresh()
        lastFailure = outcome.failure
    }

    /// What the engine actually loaded, per ADR 0003. Asked because a person
    /// asked.
    public func verify() async {
        let outcome = await ask(Request(action: .verify)) { document in
            (document["seams"]?.array ?? []).map(SeamReading.init)
        }
        if let answer = outcome.answer { seams = answer }
        lastFailure = outcome.failure
    }

    /// One request, and the three ways it can end without an answer.
    private func ask<T>(
        _ request: Request, _ read: ([String: JSONValue]) -> T
    ) async -> Outcome<T> {
        busy = true
        defer { busy = false }
        do {
            let reply = try await client.ask(request)
            if let refusal = reply.refusal {
                // Its own words, carried, not rephrased.
                return .failed(.refused(refusal))
            }
            return .answered(read(reply.data))
        } catch let failure as ControlPlaneFailure {
            switch failure {
            case .protocolMismatch:
                return .failed(.protocolMismatch(failure.detail))
            case .engineUnreachable, .unreadable:
                return .failed(.unreachable(failure.detail))
            }
        } catch {
            return .failed(.unreachable("\(error)"))
        }
    }
}

/// An answer, or the reason there is none.
enum Outcome<T> {
    case answered(T)
    case failed(ActionFailure)

    var answer: T? {
        if case .answered(let value) = self { return value }
        return nil
    }

    var failure: ActionFailure? {
        if case .failed(let failure) = self { return failure }
        return nil
    }
}

/// One row of `verify`: what configuration named, what the adapter says about
/// itself, and which of the three outcomes that is.
public struct SeamReading: Equatable, Sendable, Identifiable {
    public var seam: String
    public var outcome: String
    public var configured: String
    public var loaded: String
    public var detail: String
    public var id: String { seam }

    public init(_ value: JSONValue) {
        seam = value["seam"]?.string ?? ""
        outcome = value["outcome"]?.string ?? ""
        configured = value["configured"]?.string ?? ""
        loaded = value["loaded"]?.string ?? ""
        detail = value["detail"]?.string ?? ""
    }
}
