import Foundation
import Testing

@testable import ShellCore

/// A control plane that answers from a script, and remembers what it was asked.
final class ScriptedControlPlane: ControlPlaneDialing, @unchecked Sendable {
    private let lock = NSLock()
    private var answers: [Action: Result<String, ControlPlaneFailure>]
    private(set) var asked: [String] = []

    init(_ answers: [Action: Result<String, ControlPlaneFailure>]) {
        self.answers = answers
    }

    func ask(_ request: Request) async throws -> Reply {
        lock.withLock { asked.append(request.action.rawValue) }
        let answer =
            lock.withLock { answers[request.action] }
            ?? .failure(.engineUnreachable("nothing scripted for \(request.action.rawValue)"))
        switch answer {
        case .success(let line): return try Reply.of(Data(line.utf8))
        case .failure(let failure): throw failure
        }
    }

    func requests() -> [String] { lock.withLock { asked } }
}

private let allSwitchesOff = """
    {"ok": true, "action": "status", "protocol": 1, "data": {
      "switches": {"duty": false, "voice": false, "message": false},
      "sessions": [], "call_id": null, "pending_relays": [], "pending_approvals": []}}
    """

@MainActor
@Suite struct ControlPanelTests {
    @Test func itRendersWhatBridgeCoreHolds() async {
        let panel = ControlPanel(client: ScriptedControlPlane([.status: .success(allSwitchesOff)]))
        await panel.refresh()

        guard case .read(let status) = panel.reading else {
            Issue.record("expected a reading")
            return
        }
        #expect(status.switches.map(\.name) == ["duty", "voice", "message"])
        #expect(status.switches.allSatisfy { !$0.on })
        #expect(!status.callIsUp)
    }

    @Test func theControlPlaneIsNeverGated() async {
        // Every action answers with Duty, Voice and Message all off. The dropdown
        // shows status and flips switches from exactly that machine — ADR 0002.
        let engine = ScriptedControlPlane([
            .status: .success(allSwitchesOff),
            .switch: .success(
                #"{"ok": true, "action": "switch", "protocol": 1, "data": {"name": "duty", "on": true, "previous": false}}"#
            ),
        ])
        let panel = ControlPanel(client: engine)
        await panel.refresh()
        await panel.flip("duty", on: true)

        #expect(engine.requests() == ["status", "switch", "status"])
    }

    @Test func aRefusalIsRenderedInItsOwnWords() async {
        let panel = ControlPanel(
            client: ScriptedControlPlane([
                .switch: .success(
                    #"{"ok": false, "action": "switch", "protocol": 1, "error": {"code": "unknown_switch", "message": "unknown switch: 'sound'"}}"#
                ),
                .status: .success(allSwitchesOff),
            ]))
        await panel.flip("sound", on: true)

        // The refusal is what the user is owed; the re-read that follows must not
        // erase it by succeeding.
        guard case .refused(let refusal) = panel.lastFailure else {
            Issue.record(
                "expected the engine's refusal, got \(String(describing: panel.lastFailure))")
            return
        }
        #expect(refusal.message == "unknown switch: 'sound'")
        #expect(refusal.code == .unknownSwitch)
        // And the status beside it is still the status.
        guard case .read = panel.reading else {
            Issue.record("expected the re-read to have landed")
            return
        }
    }

    @Test func aSucceedingActionClearsTheLastRefusal() async {
        let panel = ControlPanel(
            client: ScriptedControlPlane([
                .switch: .success(
                    #"{"ok": true, "action": "switch", "protocol": 1, "data": {"name": "duty", "on": true, "previous": false}}"#
                ),
                .status: .success(allSwitchesOff),
            ]))
        await panel.flip("duty", on: true)
        #expect(panel.lastFailure == nil)
    }

    @Test func nothingAnsweringIsADifferentSentenceFromARefusal() async {
        let panel = ControlPanel(
            client: ScriptedControlPlane([
                .status: .failure(.engineUnreachable("no engine listening on /tmp/x.sock"))
            ]))
        await panel.refresh()

        guard case .failed(.unreachable(let detail)) = panel.reading else {
            Issue.record("expected a surface-side failure, got \(panel.reading)")
            return
        }
        #expect(detail.contains("no engine listening"))
    }

    @Test func theLiveToggleStartsACallWhenNoneIsUp() async {
        let panel = ControlPanel(
            client: ScriptedControlPlane([
                .live: .success(
                    #"{"ok": true, "action": "live", "protocol": 1, "data": {"state": "up", "call_id": "call-1"}}"#
                ),
                .status: .success(allSwitchesOff),
            ]))
        await panel.toggleLive()

        #expect(panel.live == LiveReading(state: "up", callID: "call-1"))
    }

    @Test func theLiveToggleEndsTheCallThatIsUp() async {
        let panel = ControlPanel(
            client: ScriptedControlPlane([
                .live: .success(
                    #"{"ok": true, "action": "live", "protocol": 1, "data": {"state": "down", "call_id": null}}"#
                ),
                .status: .success(allSwitchesOff),
            ]))
        await panel.toggleLive()

        #expect(panel.live == LiveReading(state: "down", callID: nil))
    }

    @Test func theLiveToggleFailsHonestlyWhenThereIsNoEngine() async {
        let panel = ControlPanel(
            client: ScriptedControlPlane([
                .live: .failure(.engineUnreachable("no engine listening on /tmp/x.sock"))
            ]))
        await panel.toggleLive()

        // No call state was invented, and the failure is the surface's own kind —
        // not a refusal, because the engine said nothing at all.
        #expect(panel.live == nil)
        guard case .unreachable = panel.lastFailure else {
            Issue.record(
                "expected engine_unreachable, got \(String(describing: panel.lastFailure))")
            return
        }
    }

    @Test func theToggleIsOneActionAndTheShellHoldsNoCallState() async {
        // Two presses, two `live` requests, and nothing in between that decided
        // which of start-or-end was happening. That decision is Bridge Core's.
        let engine = ScriptedControlPlane([
            .live: .success(
                #"{"ok": true, "action": "live", "protocol": 1, "data": {"state": "up", "call_id": "call-1"}}"#
            ),
            .status: .success(allSwitchesOff),
        ])
        let panel = ControlPanel(client: engine)
        await panel.toggleLive()
        await panel.toggleLive()

        #expect(engine.requests() == ["live", "status", "live", "status"])
    }

    @Test func verifyReportsWhatTheEngineActuallyLoaded() async {
        let panel = ControlPanel(
            client: ScriptedControlPlane([
                .verify: .success(
                    """
                    {"ok": true, "action": "verify", "protocol": 1, "data": {"seams": [
                      {"seam": "call", "outcome": "pass", "configured": "a:b", "loaded": "RealtimeCall", "detail": ""},
                      {"seam": "companion_channel", "outcome": "fail", "configured": "c:d", "loaded": "", "detail": "the far side would not open"}]}}
                    """)
            ]))
        await panel.verify()

        #expect(panel.seams?.map(\.seam) == ["call", "companion_channel"])
        #expect(panel.seams?.last?.outcome == "fail")
        #expect(panel.seams?.last?.detail == "the far side would not open")
    }

    @Test func theToggleHasNoOpinionUntilSomethingHasAnswered() async {
        // The label follows what Bridge Core said. With nothing said, the shell
        // does not get to decide whether pressing it will start or end a call —
        // and it must still be pressable, which is what makes the ticket's third
        // state reachable at all.
        let panel = ControlPanel(
            client: ScriptedControlPlane([
                .status: .failure(.engineUnreachable("no engine listening on /tmp/x.sock"))
            ]))
        #expect(panel.callIsUp == nil)
        await panel.refresh()
        #expect(panel.callIsUp == nil)
    }

    @Test func theToggleUsesItsOwnReplyOnlyUntilAStatusLands() async {
        let panel = ControlPanel(
            client: ScriptedControlPlane([
                .live: .success(
                    #"{"ok": true, "action": "live", "protocol": 1, "data": {"state": "up", "call_id": "call-1"}}"#
                ),
                .status: .failure(.engineUnreachable("no engine listening on /tmp/x.sock")),
            ]))
        await panel.toggleLive()
        // Nothing else has answered, so the toggle's own reply is all there is.
        #expect(panel.callIsUp == true)
    }

    @Test func aCallEndedByAnotherSurfaceIsNoticed() async {
        // The engine reports a call up, then somebody else ends it. The next read
        // says so, and this panel must follow it rather than its own last toggle
        // — a surface that preferred its own answer would be holding call state.
        let engine = ScriptedControlPlane([
            .live: .success(
                #"{"ok": true, "action": "live", "protocol": 1, "data": {"state": "up", "call_id": "call-1"}}"#
            ),
            .status: .success(allSwitchesOff),
        ])
        let panel = ControlPanel(client: engine)

        await panel.toggleLive()
        // `allSwitchesOff` carries `call_id: null` — the call this shell just
        // started is already gone as far as Bridge Core is concerned.
        #expect(panel.callIsUp == false)

        await panel.refresh()
        #expect(panel.callIsUp == false)
        // The toggle's own words are still shown; they are just not the state.
        #expect(panel.live?.state == "up")
    }

    @Test func nothingIsReadUntilSomethingAsks() {
        // No background timer: a poll nobody is reading is a permanent metronome
        // in a bounded log.
        let engine = ScriptedControlPlane([:])
        _ = ControlPanel(client: engine)
        #expect(engine.requests().isEmpty)
    }
}
