// XCTest: classes of test methods, the classic way.
import AppCore
import XCTest

final class TipCalculatorTests: XCTestCase {
    func testTipAndTotal() {
        let bill = TipCalculator(bill: 40, tipPercent: 15)
        XCTAssertEqual(bill.tip, 6)
        XCTAssertEqual(bill.total, 46)
    }

    func testTipRoundsToCents() {
        let bill = TipCalculator(bill: Decimal(string: "10.05")!, tipPercent: 15)
        XCTAssertEqual(bill.tip, Decimal(string: "1.51"))
    }

    func testSharesNeverFallShortOfTheTotal() {
        let bill = TipCalculator(bill: 100, tipPercent: 0, people: 3)
        XCTAssertEqual(bill.perPerson, Decimal(string: "33.34"))
        XCTAssertGreaterThanOrEqual(bill.perPerson * 3, bill.total)
    }

    func testFormatsAsMoney() {
        XCTAssertEqual(TipCalculator.format(Decimal(string: "12.5")!, currency: "USD",
                                            locale: Locale(identifier: "en_US")), "$12.50")
    }
}
