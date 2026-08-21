import Foundation
import Testing

@testable import ShellCore

/// The engine names what was missing on stderr and exits 2, and that happens
/// *before* it adopts its own log (ADR 0004) — so stderr is the only place that
/// reason exists, and the shell is the only thing holding it.
@Suite struct StderrRingTests {
    @Test func itKeepsTheLinesVerbatim() {
        var ring = StderrRing()
        ring.ingest(Data("gpt-voicecoding-engine: [adapters] call names nothing\n".utf8))
        // Not rephrased, not trimmed, not prefixed. Same law as `error.message`.
        #expect(ring.lines == ["gpt-voicecoding-engine: [adapters] call names nothing"])
    }

    @Test func aChunkSplitMidLineIsStillOneLine() {
        var ring = StderrRing()
        ring.ingest(Data("a seam with nothing".utf8))
        ring.ingest(Data(" behind it\n".utf8))
        #expect(ring.lines == ["a seam with nothing behind it"])
    }

    @Test func anUnterminatedTailIsShownRatherThanWithheld() {
        // A process that died mid-line still said something, and that half-line
        // is often the whole explanation.
        var ring = StderrRing()
        ring.ingest(Data("Traceback (most recent".utf8))
        #expect(ring.lines == ["Traceback (most recent"])
    }

    @Test func itHoldsTheLastFiftyLines() {
        var ring = StderrRing()
        for number in 1...120 { ring.ingest(Data("line \(number)\n".utf8)) }
        #expect(ring.lines.count == StderrRing.capacity)
        #expect(ring.lines.first == "line 71")
        #expect(ring.lines.last == "line 120")
    }

    @Test func fiftyIsTheCapacity() {
        #expect(StderrRing.capacity == 50)
    }

    @Test func aRestartStartsANewRing() {
        // The ring captures from spawn: the lines that explain a death are the
        // ones before it, and the previous run's are not this run's.
        var ring = StderrRing()
        ring.ingest(Data("old news\n".utf8))
        ring.clear()
        #expect(ring.lines.isEmpty)
    }

    @Test func carriageReturnsDoNotBecomeLines() {
        var ring = StderrRing()
        ring.ingest(Data("one\r\ntwo\n".utf8))
        #expect(ring.lines == ["one", "two"])
    }

    @Test func itIsEmptyUntilSomethingIsSaid() {
        #expect(StderrRing().lines.isEmpty)
    }
}
