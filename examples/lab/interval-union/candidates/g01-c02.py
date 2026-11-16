def solve(intervals):
    """Return the closed-interval union without changing the input."""
    merged = []
    for start, end in sorted(intervals, key=lambda interval: interval[0]):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        elif end > merged[-1][1]:
            merged[-1][1] = end
    return merged
