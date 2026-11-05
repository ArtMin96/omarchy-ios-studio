struct Forecast {
    var days: [Int]

    func first() -> Int {
        let d: Int? = days.isEmpty ? nil : days[0]
        return d!
    }
}

@main
struct Crashy {
    static func main() {
        let f = Forecast(days: [])
        print(f.first())
    }
}
