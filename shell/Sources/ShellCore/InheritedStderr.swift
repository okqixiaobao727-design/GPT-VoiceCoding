import Foundation

/// The stderr pipe the engine inherited from this shell.
///
/// Before the engine owns its log, its words arrive through fd 2. At adoption it
/// keeps a duplicate of this descriptor and points fd 2 at the log (ADR 0004);
/// after that, only the single final refusal sentence is mirrored here.
///
/// The engine drops the last writer when it exits. Before #137 adoption itself
/// also ended the pipe; keeping the duplicate open is what lets a later startup
/// refusal reach the shell without reading the engine's log.
///
/// **Legacy: adapted.** GPT-VoiceCoding-legacy at `1d32845` had no menu-bar
/// shell or Retry panel; `scripts/launch-agent.py:60-67` sent inherited stderr
/// to launchd's log. This reader preserves the rewrite's shell seam (#33).
///
/// `read()` is the pipe's only reader. It forwards chunks through one consumer,
/// so their bytes stay in source order and deliveries never overlap. `finish()`
/// waits for the pipe's end and for every delivery to return. Its caller can then
/// report the exit knowing none of this run's explanation can arrive afterward
/// (#33).
final class InheritedStderr: @unchecked Sendable {
    /// The child's end. `Process.standardError` is given the whole pipe rather
    /// than its write handle, so that Foundation closes *this* process's copy of
    /// the write end at spawn. Without that, this process would itself be a
    /// writer, and the pipe would never reach the end this type exists to
    /// notice.
    let pipe = Pipe()

    private let deliver: @Sendable (Data) async -> Void
    private let chunks: AsyncStream<Data>
    private let chunkContinuation: AsyncStream<Data>.Continuation
    private let readerQueue = DispatchQueue(label: "GPTVoiceCoding.InheritedStderr")
    private var monitoring = false
    private var deliveryTask: Task<Void, Never>?

    init(deliver: @escaping @Sendable (Data) async -> Void) {
        self.deliver = deliver
        (chunks, chunkContinuation) = AsyncStream.makeStream(of: Data.self)
    }

    /// Whether this is still watching the pipe.
    var isMonitoring: Bool { readerQueue.sync { monitoring } }

    /// Begin forwarding what the engine says.
    func read() {
        let chunks = chunks
        let deliver = deliver
        let deliveryTask = Task {
            for await chunk in chunks {
                await deliver(chunk)
            }
        }
        readerQueue.sync {
            monitoring = true
            self.deliveryTask = deliveryTask
        }
        pipe.fileHandleForReading.readabilityHandler = { [self] handle in
            readerQueue.sync {
                guard monitoring else { return }
                let chunk = handle.availableData
                guard chunk.isEmpty else {
                    chunkContinuation.yield(chunk)
                    return
                }
                // A descriptor at the end of a pipe is permanently readable.
                // Take the watch down in the same serialized turn that observes
                // it, or Foundation can enqueue another empty read in between.
                handle.readabilityHandler = nil
                monitoring = false
                chunkContinuation.finish()
            }
        }
    }

    /// Wait until the one reader has reached EOF and every delivery has returned.
    ///
    /// Called when the process is known to be gone, so its last writer is already
    /// closed and the reader is guaranteed to reach the end it awaits. A
    /// descendant that still owns the inherited writer intentionally keeps this
    /// pending too: reporting the run's exit before that writer closes would make
    /// the complete-before-exit promise false.
    func finish() async {
        let task: Task<Void, Never>? = readerQueue.sync { self.deliveryTask }
        await task?.value
    }

    /// Give up on a pipe that will never end by itself.
    ///
    /// `finish()`'s drain waits for the end of the pipe, and after a launch that
    /// never spawned there is no end coming: Foundation closes this process's
    /// copy of the write end when it hands the pipe to a child, so a child that
    /// was never made leaves this process holding it, waiting on itself. There
    /// is nothing to drain either — nothing ever wrote. This path runs only after
    /// a spawn that never happened, so its readability handler never fires.
    func abandon() {
        readerQueue.sync {
            monitoring = false
            chunkContinuation.finish()
            pipe.fileHandleForReading.readabilityHandler = nil
        }
        try? pipe.fileHandleForWriting.close()
        try? pipe.fileHandleForReading.close()
    }
}
