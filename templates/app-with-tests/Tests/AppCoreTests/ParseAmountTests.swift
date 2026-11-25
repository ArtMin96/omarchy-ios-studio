// Swift Testing: @Test functions and #expect, here with arguments (one test case per value).
import AppCore
import Foundation
import Testing

@Test(arguments: [("42", "42"), ("42.5", "42.5"), ("42,50", "42.5"), (" 7.05 ", "7.05"), ("0", "0")])
func parsesAmounts(text: String, expected: String) {
    #expect(TipCalculator.parseAmount(text) == Decimal(string: expected))
}

@Test(arguments: ["", "abc", "4.2.0", "-3", "1.234", "12.", ".5", "1e3"])
func rejectsWhatIsNotAnAmount(text: String) {
    #expect(TipCalculator.parseAmount(text) == nil)
}

@Test func noPeopleCountsAsOne() {
    #expect(TipCalculator(bill: 20, tipPercent: 10, people: 0).perPerson == 22)
}
