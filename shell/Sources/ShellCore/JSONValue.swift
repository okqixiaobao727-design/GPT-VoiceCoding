import Foundation

/// A JSON value, only as far as this shell needs one.
///
/// The control plane's replies are documents, not Swift types: `data` differs per
/// action and the shell renders a handful of fields out of it. Modelling each
/// action's reply as a struct would make the shell hold a second description of
/// Bridge Core's data, which is exactly the duplicated knowledge ADR 0001 keeps
/// out of surfaces.
public enum JSONValue: Equatable, Sendable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    public var string: String? {
        if case .string(let value) = self { return value }
        return nil
    }

    public var bool: Bool? {
        if case .bool(let value) = self { return value }
        return nil
    }

    public var number: Double? {
        if case .number(let value) = self { return value }
        return nil
    }

    public var array: [JSONValue]? {
        if case .array(let value) = self { return value }
        return nil
    }

    public var object: [String: JSONValue]? {
        if case .object(let value) = self { return value }
        return nil
    }

    public var isNull: Bool { self == .null }

    public subscript(key: String) -> JSONValue? { object?[key] }

    /// Read a value out of what `JSONSerialization` produced.
    public static func of(_ raw: Any) -> JSONValue {
        switch raw {
        case is NSNull:
            return .null
        case let number as NSNumber:
            // `NSNumber` does not distinguish a boolean from a 0 or 1 by type,
            // only by its ObjC encoding. Booleans matter here — `on` is one.
            if CFGetTypeID(number) == CFBooleanGetTypeID() { return .bool(number.boolValue) }
            return .number(number.doubleValue)
        case let text as String:
            return .string(text)
        case let items as [Any]:
            return .array(items.map(JSONValue.of))
        case let fields as [String: Any]:
            return .object(fields.mapValues(JSONValue.of))
        default:
            return .null
        }
    }

    /// Back into what `JSONSerialization` accepts.
    public var raw: Any {
        switch self {
        case .null: return NSNull()
        case .bool(let value): return value
        case .number(let value): return value
        case .string(let value): return value
        case .array(let values): return values.map(\.raw)
        case .object(let fields): return fields.mapValues(\.raw)
        }
    }
}
