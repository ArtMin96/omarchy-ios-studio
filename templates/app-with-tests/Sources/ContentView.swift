import AppCore
import SwiftUI

struct ContentView: View {
    @State private var billText = ""
    @State private var tipPercent = 15
    @State private var people = 1

    private var amount: Decimal? { TipCalculator.parseAmount(billText) }
    private var bill: TipCalculator { TipCalculator(bill: amount ?? 0, tipPercent: tipPercent, people: people) }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("Amount", text: $billText)
                        .keyboardType(.decimalPad)
                    if !billText.isEmpty && amount == nil {
                        Text("Type an amount like 42.50")
                            .foregroundStyle(.red)
                    }
                } header: {
                    Text("Bill")
                }
                Section {
                    Stepper("Tip: \(tipPercent)%", value: $tipPercent, in: 0...50, step: 5)
                    Stepper(people == 1 ? "1 person" : "\(people) people", value: $people, in: 1...20)
                }
                Section {
                    LabeledContent("Tip", value: TipCalculator.format(bill.tip))
                    LabeledContent("Total", value: TipCalculator.format(bill.total))
                    LabeledContent("Each pays", value: TipCalculator.format(bill.perPerson))
                        .bold()
                }
            }
            .navigationTitle("{{NAME}}")
        }
    }
}
