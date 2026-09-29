#!/usr/bin/env python3
"""Check backend test coverage against threshold."""

import sys
import xml.etree.ElementTree as ET


def check_coverage():
    """Parse coverage.xml and enforce minimum threshold."""
    try:
        tree = ET.parse("coverage.xml")
        root = tree.getroot()
        line_rate = float(root.get("line-rate", "0"))
        threshold = 0.60

        coverage_percent = line_rate * 100
        threshold_percent = threshold * 100

        print(f"Backend coverage: {coverage_percent:.2f}% (threshold {threshold_percent:.0f}%)")

        if line_rate < threshold:
            print("❌ Coverage below threshold", file=sys.stderr)
            sys.exit(1)
        else:
            print("✅ Coverage meets threshold")

    except FileNotFoundError:
        print("❌ coverage.xml not found", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"❌ Error checking coverage: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    check_coverage()
