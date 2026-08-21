import Foundation

/// One key out of one table, and deliberately no more.
///
/// The shell needs exactly one value from the engine's configuration —
/// `[engine] socket_path`, because the socket path is not derivable from the
/// state path — and the engine reads that file properly, with `tomllib`, as the
/// only thing that reads it. A TOML library in the shell would be a second
/// reader of a file this shell does not own; a scanner that answers one question
/// and refuses everything else cannot grow into one.
///
/// It understands what a path assignment looks like: table headers, `#`
/// comments, basic and literal strings. Anything else it reports as unreadable
/// rather than guessing, because a misread here would hand the shell the wrong
/// socket and it would report an engine missing that was running all along.
enum MinimalTOML {
    /// The raw text of `key` in `table`, or nil when the table or key is absent.
    static func string(forKey key: String, inTable table: String, of text: String) throws
        -> String?
    {
        var currentTable = ""
        for rawLine in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.isEmpty || line.hasPrefix("#") { continue }
            if line.hasPrefix("[") {
                guard let end = line.firstIndex(of: "]") else { continue }
                currentTable = String(line[line.index(after: line.startIndex)..<end])
                    .trimmingCharacters(in: .whitespaces)
                continue
            }
            guard currentTable == table, let separator = line.firstIndex(of: "=") else { continue }
            let name = line[line.startIndex..<separator].trimmingCharacters(in: .whitespaces)
            guard name == key else { continue }
            let value = line[line.index(after: separator)...].trimmingCharacters(in: .whitespaces)
            return try unquote(value, key: key, table: table)
        }
        return nil
    }

    private static func unquote(_ value: String, key: String, table: String) throws -> String {
        // A literal string is taken as written; a basic string is unescaped only
        // as far as a path can need.
        if value.hasPrefix("'") {
            guard let end = value.dropFirst().firstIndex(of: "'") else {
                throw ConfigurationFailure.unreadable("[\(table)] \(key) is not a closed string")
            }
            return String(value[value.index(after: value.startIndex)..<end])
        }
        guard value.hasPrefix("\"") else {
            throw ConfigurationFailure.unreadable("[\(table)] \(key) must be a path")
        }
        var unescaped = ""
        var index = value.index(after: value.startIndex)
        while index < value.endIndex {
            let character = value[index]
            if character == "\"" { return unescaped }
            if character == "\\" {
                index = value.index(after: index)
                guard index < value.endIndex else { break }
                switch value[index] {
                case "n": unescaped.append("\n")
                case "t": unescaped.append("\t")
                case "r": unescaped.append("\r")
                case "b": unescaped.append("\u{08}")
                case "f": unescaped.append("\u{0C}")
                case "\\": unescaped.append("\\")
                case "\"": unescaped.append("\"")
                case "u", "U":
                    // TOML's own escapes, and `tomllib` reads them. A path this
                    // shell refused while the engine accepted it would leave the
                    // two dialling different sockets, and the dropdown would
                    // report a healthy engine unreachable for as long as it ran.
                    let digits = value[index] == "u" ? 4 : 8
                    let (scalar, next) = try Self.scalar(
                        in: value, after: index, digits: digits, key: key, table: table)
                    unescaped.append(Character(scalar))
                    index = next
                default:
                    throw ConfigurationFailure.unreadable(
                        "[\(table)] \(key) uses an escape this shell does not read")
                }
            } else {
                unescaped.append(character)
            }
            index = value.index(after: index)
        }
        throw ConfigurationFailure.unreadable("[\(table)] \(key) is not a closed string")
    }

    /// One `\u`/`\U` escape, and where reading resumes after it.
    private static func scalar(
        in value: String, after backslash: String.Index, digits: Int, key: String, table: String
    ) throws -> (Unicode.Scalar, String.Index) {
        let start = value.index(after: backslash)
        guard let end = value.index(start, offsetBy: digits, limitedBy: value.endIndex),
            let code = UInt32(value[start..<end], radix: 16),
            let scalar = Unicode.Scalar(code)
        else {
            throw ConfigurationFailure.unreadable(
                "[\(table)] \(key) has an escape that is not \(digits) hexadecimal digits")
        }
        return (scalar, value.index(before: end))
    }
}

public enum ConfigurationFailure: Error, Equatable, Sendable {
    case unreadable(String)

    public var detail: String {
        switch self {
        case .unreadable(let detail): return detail
        }
    }
}
