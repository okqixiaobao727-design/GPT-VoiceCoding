import Foundation

/// Wait for an asynchronous test observation without copying timeout loops into
/// every fake that publishes from another queue or actor.
public func waitUntil(
    isolation: isolated (any Actor)? = #isolation,
    within seconds: TimeInterval = 3,
    pollingEvery interval: Duration = .milliseconds(10),
    _ condition: () -> Bool
) async -> Bool {
    let deadline = Date().addingTimeInterval(seconds)
    while Date() < deadline {
        if condition() { return true }
        try? await Task.sleep(for: interval)
    }
    return false
}
