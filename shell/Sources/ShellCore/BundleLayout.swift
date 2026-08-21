import Foundation

/// The one code-side name the bundle is made of.
///
/// The bundle's *identity* — its identifier, its name, its microphone usage
/// string, `LSUIElement` — lives in `shell/Resources/Info.plist`, which is the
/// single place #12 (the app bundle and signing pipeline) consumes or
/// supersedes. Repeating those strings here would give #12 two places to change
/// and one of them would go stale.
///
/// What is left is the thing a plist cannot hold: where the bundled engine's
/// interpreter sits. That name is #12's decision too — this is the one line that
/// changes when it makes it.
public enum BundleLayout {
    /// Where the bundled engine's interpreter sits under `Contents/Resources`.
    /// python-build-standalone's `install_only` layout puts it here.
    public static let engineInterpreterRelativePath = "engine/bin/python3"
}
