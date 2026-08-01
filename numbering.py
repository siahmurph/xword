"""Standard crossword numbering algorithm, shared by main.py and pdf_import.py."""
from typing import List, Set, Tuple


def compute_numbering(width: int, height: int, black: Set[int]) -> List[int]:
    numbering = [0] * (width * height)
    num = 1
    for r in range(height):
        for c in range(width):
            i = r * width + c
            if i in black:
                continue
            starts_across = (c == 0 or (r * width + c - 1) in black) and (
                c + 1 < width and (i + 1) not in black
            )
            starts_down = (r == 0 or ((r - 1) * width + c) in black) and (
                r + 1 < height and (i + width) not in black
            )
            if starts_across or starts_down:
                numbering[i] = num
                num += 1
    return numbering


def numbering_directions(width: int, height: int, black: Set[int], numbering: List[int]) -> Tuple[Set[int], Set[int]]:
    """Which clue numbers start an across entry vs. a down entry — a number
    starts both if its cell begins both directions."""
    across_nums, down_nums = set(), set()
    for r in range(height):
        for c in range(width):
            i = r * width + c
            if i in black or not numbering[i]:
                continue
            starts_across = (c == 0 or (r * width + c - 1) in black) and (
                c + 1 < width and (i + 1) not in black
            )
            starts_down = (r == 0 or ((r - 1) * width + c) in black) and (
                r + 1 < height and (i + width) not in black
            )
            if starts_across:
                across_nums.add(numbering[i])
            if starts_down:
                down_nums.add(numbering[i])
    return across_nums, down_nums
