"""Half-open integer hour intervals: ``[from_hour, to_hour)``.

All interval arithmetic in this package works on integer clock hours, matching
the source tables.  A date is an ISO string and is never mixed into hour
arithmetic.
"""
from __future__ import annotations

Interval = tuple[int, int]


def normalize(interval: Interval) -> Interval:
    """Return an ordered interval, raising on an inverted one."""
    start, end = interval
    if end < start:
        raise ValueError(f"inverted interval: {interval!r}")
    return (start, end)


def merge(intervals: list[Interval]) -> list[Interval]:
    """Merge overlapping or touching half-open intervals."""
    ordered = sorted(normalize(i) for i in intervals)
    merged: list[Interval] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def overlap(left: Interval, right: Interval) -> int:
    """Hours shared by two half-open intervals."""
    a, b = normalize(left)
    c, d = normalize(right)
    return max(0, min(b, d) - max(a, c))


def intersect(intervals: list[Interval], window: Interval) -> list[Interval]:
    """Clip intervals to one window, then merge."""
    clipped = []
    for start, end in intervals:
        lo = max(start, window[0])
        hi = min(end, window[1])
        if hi > lo:
            clipped.append((lo, hi))
    return merge(clipped)


def intersect_lists(left: list[Interval], right: list[Interval]) -> list[Interval]:
    """Intersection of two merged interval lists, merged again."""
    left_merged = merge(left)
    right_merged = merge(right)
    pieces: list[Interval] = []
    for start, end in left_merged:
        pieces.extend(intersect(right_merged, (start, end)))
    return merge(pieces)


def subtract(window: Interval, cuts: list[Interval]) -> list[Interval]:
    """Remove merged cuts from a single window; returns remaining pieces."""
    remaining = [normalize(window)]
    for cut_start, cut_end in merge(cuts):
        pieces: list[Interval] = []
        for start, end in remaining:
            if cut_end <= start or cut_start >= end:
                pieces.append((start, end))
                continue
            if start < cut_start:
                pieces.append((start, cut_start))
            if cut_end < end:
                pieces.append((cut_end, end))
        remaining = pieces
    return remaining


def length(intervals: list[Interval]) -> int:
    """Total hours across merged intervals."""
    return sum(end - start for start, end in merge(intervals))
