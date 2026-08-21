import Foundation

/// The one code-side name the bundle is made of.
///
/// The bundle's *identity* — its identifier, its name, its microphone usage
/// string, `LSUIElement` — lives in `shell/Resources/Info.plist`, and the
/// app-bundle pipeline (`app_bundle/`) reads that file rather than repeating any
/// of it. Repeating those strings here would give the pipeline two places to
/// change and one of them would go stale.
///
/// What is left is the thing a plist cannot hold: where the bundled engine's
/// interpreter sits. That one is stated in both languages, because the pipeline
/// puts the interpreter there and this resolver looks for it there — and a
/// disagreement would be silent, since the shell would simply fall through to
/// the developer path and run an interpreter outside the bundle. A test holds
/// the two literals to each other (`tests/test_app_bundle.py`).
public enum BundleLayout {
    /// Where the bundled engine's interpreter sits under `Contents/Resources`.
    /// python-build-standalone's `install_only` layout puts it here.
    public static let engineInterpreterRelativePath = "engine/bin/python3"
}
