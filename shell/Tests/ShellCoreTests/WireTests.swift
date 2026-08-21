import Foundation
import Testing

@testable import ShellCore

/// The shell's side of the contract in `docs/control-plane.md`. One JSON object
/// per line, one reply per request, and a closed error set.
@Suite struct WireTests {
    @Test func aRequestWithoutAPayloadOmitsTheKey() throws {
        let line = try Request(action: "live").line()
        let document = try #require(
            try JSONSerialization.jsonObject(with: line) as? [String: Any])
        #expect(document["action"] as? String == "live")
        #expect(document["payload"] == nil)
    }

    @Test func aRequestCarriesItsPayload() throws {
        let request = Request(
            action: "switch", payload: ["name": .string("duty"), "on": .bool(true)])
        let line = try request.line()
        let document = try #require(
            try JSONSerialization.jsonObject(with: line) as? [String: Any])
        let payload = try #require(document["payload"] as? [String: Any])
        #expect(payload["name"] as? String == "duty")
        // A JSON boolean, never the string "false", which is truthy and would
        // turn on the master switch.
        #expect(payload["on"] as? Bool == true)
    }

    @Test func aRequestLineIsNewlineTerminatedExactlyOnce() throws {
        let line = try Request(action: "status").terminatedLine()
        #expect(line.last == UInt8(ascii: "\n"))
        #expect(line.dropLast().contains(UInt8(ascii: "\n")) == false)
    }

    @Test func anAnswerIsRead() throws {
        let reply = try Reply.of(
            Data(
                """
                {"ok": true, "action": "live", "protocol": 1,
                 "data": {"state": "up", "call_id": "call-1"}}
                """.utf8))
        #expect(reply.ok)
        #expect(reply.action == "live")
        #expect(reply.protocolVersion == 1)
        #expect(reply.data["state"]?.string == "up")
        #expect(reply.refusal == nil)
    }

    @Test func aRefusalKeepsItsOwnWords() throws {
        let reply = try Reply.of(
            Data(
                """
                {"ok": false, "action": "switch", "protocol": 1,
                 "error": {"code": "unknown_switch", "message": "unknown switch: 'sound'"}}
                """.utf8))
        #expect(!reply.ok)
        let refusal = try #require(reply.refusal)
        #expect(refusal.code == .unknownSwitch)
        // Rendered verbatim: rephrasing it would be a second voice deciding what
        // the user is told.
        #expect(refusal.message == "unknown switch: 'sound'")
    }

    @Test func anUnknownErrorCodeIsStillCarried() throws {
        // The set is closed today; an engine that grew one must not be rendered
        // as if it had said nothing.
        let reply = try Reply.of(
            Data(
                #"{"ok": false, "action": null, "error": {"code": "novel", "message": "hm"}}"#.utf8)
        )
        #expect(reply.refusal?.code == .other("novel"))
        #expect(reply.action == nil)
    }

    @Test func anAbsentProtocolIsNotAnEmptyOne() throws {
        // ADR 0003: absent means an engine too old to have been asked.
        let reply = try Reply.of(Data(#"{"ok": true, "action": "status", "data": {}}"#.utf8))
        #expect(reply.protocolVersion == nil)
    }

    @Test func anUnreadableAnswerIsNotAReply() {
        #expect(throws: ControlPlaneFailure.self) { try Reply.of(Data("not json".utf8)) }
        #expect(throws: ControlPlaneFailure.self) { try Reply.of(Data("[1, 2]".utf8)) }
    }

    @Test func engineUnreachableIsNeverSentByTheEngine() throws {
        // It is a surface-side condition. An engine that claimed it would be
        // reporting that it could not be reached, which it plainly could.
        let reply = try Reply.of(
            Data(
                #"{"ok": false, "action": null, "error": {"code": "engine_unreachable", "message": "x"}}"#
                    .utf8))
        #expect(reply.refusal?.code == .other("engine_unreachable"))
    }
}
