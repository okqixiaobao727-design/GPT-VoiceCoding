import Darwin
import Dispatch
import Foundation

/// Watches both the credential's directory and the file itself.
///
/// The directory watch sees a missing file appear and follows atomic replacement;
/// the file watch sees in-place edits and permission repairs. Either may report
/// the same change, so callers must treat notifications as hints and re-read the
/// credential source of truth.
final class CredentialFileObserver: @unchecked Sendable {
    private let path: String
    private let changed: @Sendable () -> Void
    private let queue = DispatchQueue(label: "nz.simonqi.gpt-voicecoding.credential-file")
    private let queueKey = DispatchSpecificKey<UUID>()
    private let queueID = UUID()
    private var directorySource: DispatchSourceFileSystemObject?
    private var fileSource: DispatchSourceFileSystemObject?
    private var isCancelled = false

    init(path: String, changed: @escaping @Sendable () -> Void) throws {
        self.path = path
        self.changed = changed
        queue.setSpecific(key: queueKey, value: queueID)

        let directory = URL(fileURLWithPath: path).deletingLastPathComponent().path
        let descriptor = open(directory, O_EVTONLY)
        guard descriptor >= 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: descriptor,
            eventMask: [.write, .rename, .delete, .revoke],
            queue: queue)
        source.setEventHandler { [weak self] in self?.noticedChange() }
        source.setCancelHandler { close(descriptor) }
        directorySource = source
        source.resume()

        queue.sync { replaceFileSource() }
    }

    deinit {
        cancel()
    }

    func cancel() {
        if DispatchQueue.getSpecific(key: queueKey) == queueID {
            cancelOnQueue()
        } else {
            queue.sync { cancelOnQueue() }
        }
    }

    private func noticedChange() {
        guard !isCancelled else { return }
        replaceFileSource()
        changed()
    }

    private func replaceFileSource() {
        fileSource?.cancel()
        fileSource = nil

        let descriptor = open(path, O_EVTONLY)
        guard descriptor >= 0 else { return }
        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: descriptor,
            eventMask: [.write, .extend, .attrib, .rename, .delete, .revoke],
            queue: queue)
        source.setEventHandler { [weak self] in self?.noticedChange() }
        source.setCancelHandler { close(descriptor) }
        fileSource = source
        source.resume()
    }

    private func cancelOnQueue() {
        guard !isCancelled else { return }
        isCancelled = true
        fileSource?.cancel()
        fileSource = nil
        directorySource?.cancel()
        directorySource = nil
    }
}
