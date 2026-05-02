"""Minimal module fixture for CodeCapability tests."""

import helper


def add_one(value: int) -> int:
    return helper.helper(value)


class Calculator:
    def compute(self, value: int) -> int:
        return add_one(value)
