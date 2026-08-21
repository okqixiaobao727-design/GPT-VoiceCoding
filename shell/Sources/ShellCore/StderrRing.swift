import Foundation

/// The last few things the engine said on stderr, held in memory and nowhere else.
///
/// Bounded because an engine in a crash loop can say a great deal, and the shell
/// is a menu, not a log. Nothing here is written to disk: the engine owns its own
/// log (ADR 0004), and a second file kept by a surface would be a second account
/// of the same run.
public struct StderrRing: Sendable {
    /// Enough to carry a short traceback and the line that explains it.
    public static let capacity = 50

    private var complete: [String] = []
    /// The bytes that have arrived since the last newline. A process that died
    /// mid-line still said something.
    ///
    /// Bytes rather than a `String`, because a chunk may split a UTF-8 sequence
    /// and because `"\r\n"` is one `Character` in Swift — searching a `String`
    /// for a newline would miss it.
    private var partial: [UInt8] = []

    public init() {}

    /// Every line held, oldest first, exactly as the engine wrote it.
    public var lines: [String] {
        partial.isEmpty ? complete : complete + [Self.text(of: partial)]
    }

    public mutating func ingest(_ chunk: Data) {
        partial.append(contentsOf: chunk)
        while let newline = partial.firstIndex(of: UInt8(ascii: "\n")) {
            var line = Array(partial[partial.startIndex..<newline])
            if line.last == UInt8(ascii: "\r") { line.removeLast() }
            complete.append(Self.text(of: line))
            partial.removeFirst(newline + 1)
        }
        // Held to the last `capacity` lines, counting the unterminated tail.
        let overflow = complete.count - (Self.capacity - (partial.isEmpty ? 0 : 1))
        if overflow > 0 { complete.removeFirst(overflow) }
    }

    /// A new run gets a new ring: the previous run's words are not this one's.
    public mutating func clear() {
        complete = []
        partial = []
    }

    private static func text(of bytes: [UInt8]) -> String {
        String(decoding: bytes, as: UTF8.self)
    }
}
