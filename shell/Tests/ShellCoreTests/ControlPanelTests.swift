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
    {"ok": true, "action": "status", "protocol": 3, "data": {
      "switches": {"duty": false, "voice": false, "message": false, "auto_hangup": false},
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
        #expect(status.switches.map(\.name) == ["duty", "voice", "message", "auto_hangup"])
        #expect(
            status.switches.map(\.title) == [
                "Duty Switch", "Voice Switch", "Message Switch", "Auto Hang-up Switch",
            ])
        #expect(status.switches.allSatisfy { !$0.on })
        #expect(!status.callIsUp)
    }

    @Test func theAutoHangupSwitchIsFlippedLikeTheOthers() async {
        // Its own row, sent under its own name — the panel holds no second path
        // for a switch that is not under Duty.
        let engine = ScriptedControlPlane([
            .status: .success(allSwitchesOff),
            .switch: .success(
                #"{"ok": true, "action": "switch", "protocol": 3, "data": {"name": "auto_hangup", "on": false, "previous": true}}"#
            ),
        ])
        let panel = ControlPanel(client: engine)
        await panel.flip("auto_hangup", on: false)

        #expect(engine.requests() == ["switch", "status"])
        #expect(panel.lastFailure == nil)
    }

    @Test func aSwitchTheShellDoesNotKnowIsNotRendered() async {
        // Additive on the wire: the panel maps `status` over its own canonical
        // order, so a key it has no row for is ignored rather than guessed at.
        let engine = ScriptedControlPlane([
            .status: .success(
                """
                {"ok": true, "action": "status", "protocol": 3, "data": {
                  "switches": {"duty": true, "voice": false, "message": false,
                               "auto_hangup": true, "sound": true},
                  "sessions": [], "call_id": null, "pending_relays": [],
                  "pending_approvals": []}}
                """)
        ])
        let panel = ControlPanel(client: engine)
        await panel.refresh()

        guard case .read(let status) = panel.reading else {
            Issue.record("expected a reading")
            return
        }
        #expect(status.switches.map(\.name) == ["duty", "voice", "message", "auto_hangup"])
    }

    @Test func aChildProcessFollowsItsParentAndAnUnknownChildIsLast() async {
        let status = """
            {"ok": true, "action": "status", "protocol": 3, "data": {
              "switches": {"duty": true, "voice": true, "message": true},
              "sessions": [
                {
                  "target": {"agent": "codex", "session_id": "ended-1", "pid": null},
                  "name": "GPT-VoiceCoding · Already ended",
                  "lifecycle": "ended",
                  "state": "idle",
                  "child": {"kind": "main", "parent": null}
                },
                {
                  "target": {"agent": "codex", "session_id": "child-2", "pid": null},
                  "name": null,
                  "lifecycle": "live",
                  "state": "idle",
                  "child": {
                    "kind": "child",
                    "parent": {"agent": "codex", "session_id": "parent-1", "pid": null}
                  }
                },
                {
                  "target": {"agent": "codex", "session_id": "child-1", "pid": null},
                  "name": null,
                  "lifecycle": "live",
                  "state": "running",
                  "child": {
                    "kind": "child",
                    "parent": {"agent": "codex", "session_id": "parent-1", "pid": null}
                  }
                },
                {
                  "target": {"agent": "claude", "session_id": "orphan-1", "pid": 303},
                  "name": null,
                  "lifecycle": "live",
                  "state": "idle",
                  "child": {"kind": "child", "parent": null}
                },
                {
                  "target": {"agent": "codex", "session_id": "parent-1", "pid": null},
                  "name": "GPT-VoiceCoding · Control Panel roster",
                  "lifecycle": "live",
                  "state": "waiting",
                  "child": {"kind": "main", "parent": null}
                }
              ],
              "call_id": null, "pending_relays": [], "pending_approvals": []}}
            """
        let panel = ControlPanel(client: ScriptedControlPlane([.status: .success(status)]))

        await panel.refresh()

        guard case .read(let reading) = panel.reading else {
            Issue.record("expected a reading")
            return
        }
        #expect(reading.sessions == 1)
        #expect(reading.childProcesses == 3)
        #expect(
            reading.sessionRows.map(\.target.sessionID)
                == ["parent-1", "child-2", "child-1", "orphan-1"])
        #expect(reading.sessionRows.map(\.state) == ["waiting", "idle", "running", "idle"])
        #expect(reading.sessionRows.map(\.isChild) == [false, true, true, true])
        #expect(reading.sessionRows[0].title == "GPT-VoiceCoding · Control Panel roster")
        #expect(reading.sessionRows[1].title == "Child Process")
        #expect(reading.sessionRows[1].parent?.sessionID == "parent-1")
        #expect(reading.sessionRows[3].parent == nil)
    }

    @Test func aWaitingRowCarriesWhatItWaitsForAndWhenItLastMoved() async {
        let status = """
            {"ok": true, "action": "status", "protocol": 3, "data": {
              "switches": {"duty": true, "voice": true, "message": true},
              "sessions": [
                {
                  "target": {"agent": "claude", "session_id": "session-1", "pid": 404},
                  "name": "GPT-VoiceCoding · Pick a test seam",
                  "lifecycle": "live",
                  "state": "waiting",
                  "last_activity": "1970-01-01T00:02:03+00:00",
                  "waiting_for": {
                    "kind": "question",
                    "caught_up": true,
                    "prompt": "Which seam?",
                    "options": [
                      {
                        "text": "public behavior",
                        "description": "Exercise the adapter event",
                        "recommended": true
                      }
                    ]
                  },
                  "progress": {
                    "availability": "readable",
                    "has_history": true,
                    "omission": "status_summary",
                    "read_at": "1970-01-01T00:02:03+00:00",
                    "recent": []
                  },
                  "child": {"kind": "main", "parent": null}
                },
                {
                  "target": {"agent": "claude", "session_id": "session-2", "pid": 405},
                  "name": "GPT-VoiceCoding · Approve a tool",
                  "lifecycle": "live",
                  "state": "waiting",
                  "last_activity": "1970-01-01T00:02:03.500000+00:00",
                  "waiting_for": {"kind": "permission"},
                  "child": {"kind": "main", "parent": null}
                }
              ],
              "call_id": null, "pending_relays": [], "pending_approvals": []}}
            """
        let panel = ControlPanel(client: ScriptedControlPlane([.status: .success(status)]))

        await panel.refresh()

        guard case .read(let reading) = panel.reading else {
            Issue.record("expected a reading")
            return
        }
        #expect(reading.sessionRows[0].waitingKind == "question")
        #expect(reading.sessionRows[0].waitingMessage == "Waiting for question")
        #expect(reading.sessionRows[0].lastActivity == Date(timeIntervalSince1970: 123))
        #expect(reading.sessionRows[1].waitingKind == "permission")
        #expect(reading.sessionRows[1].waitingMessage == "Waiting for permission")
        #expect(reading.sessionRows[1].lastActivity == Date(timeIntervalSince1970: 123.5))
    }

    @Test func anEmptyRosterSaysSoInWords() async {
        let panel = ControlPanel(client: ScriptedControlPlane([.status: .success(allSwitchesOff)]))

        await panel.refresh()

        guard case .read(let reading) = panel.reading else {
            Issue.record("expected a reading")
            return
        }
        #expect(reading.emptyRosterMessage == "No live Sessions")
    }

    @Test func theRosterScrollsWithinItsSingleHeightBound() throws {
        let sourceURL = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()  // ShellCoreTests
            .deletingLastPathComponent()  // Tests
            .deletingLastPathComponent()  // shell
            .appendingPathComponent("Sources/GPTVoiceCodingShell/ControlPanelView.swift")
        let source = try String(contentsOf: sourceURL, encoding: .utf8)
        let rosterSource =
            source.components(separatedBy: "private struct SessionRoster: View").last?
            .components(separatedBy: "private struct SessionRosterRow: View").first ?? ""

        #expect(source.contains("private let rosterMaxHeight: CGFloat = 220"))
        #expect(rosterSource.contains("ScrollView"))
        #expect(rosterSource.contains(".frame(maxHeight: rosterMaxHeight)"))
    }

    @Test func theControlPlaneIsNeverGated() async {
        // Every action answers with Duty, Voice and Message all off. The dropdown
        // shows status and flips switches from exactly that machine — ADR 0002.
        let engine = ScriptedControlPlane([
            .status: .success(allSwitchesOff),
            .switch: .success(
                #"{"ok": true, "action": "switch", "protocol": 3, "data": {"name": "duty", "on": true, "previous": false}}"#
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
                    #"{"ok": false, "action": "switch", "protocol": 3, "error": {"code": "unknown_switch", "message": "unknown switch: 'sound'"}}"#
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
                    #"{"ok": true, "action": "switch", "protocol": 3, "data": {"name": "duty", "on": true, "previous": false}}"#
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

    @Test func aProtocolMismatchIsNotAnUnreachableEngineOrARefusal() async {
        let panel = ControlPanel(
            client: ScriptedControlPlane([
                .status: .failure(.protocolMismatch(received: 4, supported: 3))
            ]))

        await panel.refresh()

        guard case .failed(.protocolMismatch) = panel.reading else {
            Issue.record("expected a protocol mismatch, got \(panel.reading)")
            return
        }
    }

    @Test func theLiveToggleStartsACallWhenNoneIsUp() async {
        let panel = ControlPanel(
            client: ScriptedControlPlane([
                .live: .success(
                    #"{"ok": true, "action": "live", "protocol": 3, "data": {"state": "up", "call_id": "call-1"}}"#
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
                    #"{"ok": true, "action": "live", "protocol": 3, "data": {"state": "down", "call_id": null}}"#
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
                #"{"ok": true, "action": "live", "protocol": 3, "data": {"state": "up", "call_id": "call-1"}}"#
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
                    {"ok": true, "action": "verify", "protocol": 3, "data": {"seams": [
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
                    #"{"ok": true, "action": "live", "protocol": 3, "data": {"state": "up", "call_id": "call-1"}}"#
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
                #"{"ok": true, "action": "live", "protocol": 3, "data": {"state": "up", "call_id": "call-1"}}"#
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
