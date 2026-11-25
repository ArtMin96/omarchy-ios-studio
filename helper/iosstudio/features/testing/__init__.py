"""Host unit tests: `swift test` on this computer for a project's Foundation-only targets.

results.py is the shared TestResult shape, xctest.py parses XCTest's console output line by line and
events.py follows swift-testing's JSON event stream (version 0) while the run writes it.
"""
