import Darwin
import Foundation
import Testing

@testable import ShellCore

@Suite struct ControlPlaneClientTests {
    @Test func oneRequestGetsOneReply() async throws {
        let engine = try FakeEngineSocket(
            behaviour: .answer(
                #"{"ok": true, "action": "live", "protocol": 3, "data": {"state": "up", "call_id": "call-1"}}"#
            ))
        defer { engine.stop() }

        let reply = try await UnixSocketControlPlane(path: engine.path).ask(Request(action: .live))

        #expect(reply.ok)
        #expect(reply.data["state"]?.string == "up")
        #expect(engine.requests() == [#"{"action":"live"}"#])
    }

    @Test func aRefusalIsAnAnswer() async throws {
        let engine = try FakeEngineSocket(
            behaviour: .answer(
                #"{"ok": false, "action": "switch", "protocol": 3, "error": {"code": "unknown_switch", "message": "unknown switch: 'sound'"}}"#
            ))
        defer { engine.stop() }

        let reply = try await UnixSocketControlPlane(path: engine.path).ask(
            Request(action: .switch, payload: ["name": .string("sound"), "on": .bool(true)]))

        // The engine answering "no" is not the engine being unreachable.
        #expect(reply.refusal?.message == "unknown switch: 'sound'")
    }

    @Test func anUnsupportedProtocolVersionIsAProtocolMismatch() async throws {
        let engine = try FakeEngineSocket(
            behaviour: .answer(
                #"{"ok": true, "action": "status", "protocol": 4, "data": {}}"#
            ))
        defer { engine.stop() }

        let failure = await failure(of: UnixSocketControlPlane(path: engine.path))

        #expect(failure == .protocolMismatch(received: 4, supported: 3))
    }

    @Test func aMissingProtocolVersionKeepsTheAbsentDistinction() async throws {
        let engine = try FakeEngineSocket(
            behaviour: .answer(#"{"ok": true, "action": "status", "data": {}}"#))
        defer { engine.stop() }

        let failure = await failure(of: UnixSocketControlPlane(path: engine.path))

        #expect(failure == .protocolMismatch(received: nil, supported: 3))
    }

    @Test func nothingListeningIsEngineUnreachable() async throws {
        let engine = try FakeEngineSocket(behaviour: .hangUp)
        let path = engine.path
        engine.stop()

        await #expect(throws: ControlPlaneFailure.self) {
            try await UnixSocketControlPlane(path: path).ask(Request(action: .status))
        }
    }

    @Test func aSocketOpenToOthersIsRefused() async throws {
        let engine = try FakeEngineSocket(behaviour: .answer("{}"), mode: 0o666)
        defer { engine.stop() }

        let failure = await failure(of: UnixSocketControlPlane(path: engine.path))
        #expect(failure?.detail.contains("not private to this user") == true)
    }

    @Test func aPathThatIsNotASocketIsRefused() async throws {
        let file = URL(fileURLWithPath: "/tmp/gvc-not-a-socket-\(UUID().uuidString.prefix(8))")
        try Data().write(to: file)
        defer { try? FileManager.default.removeItem(at: file) }

        let failure = await failure(of: UnixSocketControlPlane(path: file.path))
        #expect(failure?.detail.contains("not a Unix socket") == true)
    }

    @Test func aPathTooLongToDialSaysSoInWords() async throws {
        let long = "/tmp/" + String(repeating: "a", count: SocketOwnership.maxSocketPathBytes)
        let failure = await failure(of: UnixSocketControlPlane(path: long))
        // Named, rather than arriving as an errno from inside the socket layer.
        #expect(failure?.detail.contains("may not exceed") == true)
    }

    @Test func hangingUpWithoutAWordIsEngineUnreachable() async throws {
        let engine = try FakeEngineSocket(behaviour: .hangUp)
        defer { engine.stop() }

        let failure = await failure(of: UnixSocketControlPlane(path: engine.path))
        #expect(failure?.detail.contains("closed without replying") == true)
    }

    @Test func anAnswerPastTheByteBoundIsRefused() async throws {
        let engine = try FakeEngineSocket(behaviour: .flood)
        defer { engine.stop() }

        let failure = await failure(of: UnixSocketControlPlane(path: engine.path))
        #expect(failure?.detail.contains("\(maxRequestBytes) bytes") == true)
    }

    @Test func silenceBecomesAFailureInBoundedTime() async throws {
        let engine = try FakeEngineSocket(behaviour: .silence)
        defer { engine.stop() }

        let started = Date()
        let failure = await failure(of: UnixSocketControlPlane(path: engine.path, timeout: 0.5))
        #expect(failure?.detail.contains("did not answer in time") == true)
        #expect(Date().timeIntervalSince(started) < 3)
    }

    @Test func aLiveSocketIsConnectableAndADeadOneIsNot() throws {
        let engine = try FakeEngineSocket(behaviour: .answer("{}"))
        #expect(SocketOwnership.isConnectable(engine.path))
        let path = engine.path
        engine.stop()
        #expect(!SocketOwnership.isConnectable(path))
    }

    private func failure(of client: UnixSocketControlPlane) async -> ControlPlaneFailure? {
        do {
            _ = try await client.ask(Request(action: .status))
            return nil
        } catch let failure as ControlPlaneFailure {
            return failure
        } catch {
            return nil
        }
    }
}
