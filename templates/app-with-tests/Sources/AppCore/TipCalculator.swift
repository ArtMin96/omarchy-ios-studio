// AppCore holds the app's logic. Its tests (Tests/AppCoreTests) run on this computer with
// swift test, without an iPhone, so code here may import Foundation but not UIKit or SwiftUI.
// Screens stay in the app target and call into this module.
import Foundation

/// Splits a bill: the tip, the total and what each person pays, rounded to cents.
public struct TipCalculator: Equatable, Sendable {
    public var bill: Decimal
    public var tipPercent: Int
    public var people: Int

    public init(bill: Decimal, tipPercent: Int = 15, people: Int = 1) {
        self.bill = bill
        self.tipPercent = tipPercent
        self.people = people
    }

    public var tip: Decimal { Self.cents(bill * Decimal(tipPercent) / 100) }
    public var total: Decimal { bill + tip }
    /// Each person's share, rounded up to the next cent so the shares never fall short of the total.
    public var perPerson: Decimal { Self.cents(total / Decimal(max(people, 1)), .up) }

    /// The amount someone typed ("42", "42.5", "42,50", " 7.05 "), or nil when it isn't a plain amount
    /// with at most two decimals.
    public static func parseAmount(_ text: String) -> Decimal? {
        let parts = text.trimmingCharacters(in: .whitespaces)
            .replacingOccurrences(of: ",", with: ".")
            .split(separator: ".", omittingEmptySubsequences: false)
        guard (1...2).contains(parts.count),
              let whole = parts.first, !whole.isEmpty, whole.allSatisfy(\.isASCIIDigit),
              parts.count == 1 || (1...2).contains(parts[1].count) && parts[1].allSatisfy(\.isASCIIDigit)
        else { return nil }
        return Decimal(string: parts.joined(separator: "."), locale: Locale(identifier: "en_US_POSIX"))
    }

    /// An amount as money in the given currency and locale (the person's own by default).
    public static func format(_ amount: Decimal, currency: String = Locale.current.currency?.identifier ?? "USD",
                              locale: Locale = .current) -> String {
        amount.formatted(.currency(code: currency).locale(locale))
    }

    private static func cents(_ value: Decimal, _ mode: NSDecimalNumber.RoundingMode = .plain) -> Decimal {
        var input = value
        var result = Decimal()
        NSDecimalRound(&result, &input, 2, mode)
        return result
    }
}

private extension Character {
    var isASCIIDigit: Bool { isASCII && isNumber }
}
