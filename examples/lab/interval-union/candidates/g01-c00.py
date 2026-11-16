"""Union of closed intervals, preserving separated integer neighbors."""


def solve(intervals):
    """Return sorted independent interval lists without modifying the input."""
    merged = []
    for start, end in sorted(intervals, key=lambda interval: (interval[0], interval[1])):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    return merged
