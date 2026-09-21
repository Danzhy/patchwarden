import os
import sys
from typing import List


def total(items: List[int]) -> int:
    unused = 0
    return sum(items)


def is_missing(value):
    if value == None:
        return True
    return False


def append_item(item, bucket=[]):
    bucket.append(item)
    return bucket
