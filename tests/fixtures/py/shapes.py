"""Shapes module."""
import math
from .base import Shape

DEFAULT_SIDES = 4


@register("circle")
def make_circle(radius):
    """Create a circle."""
    return Circle(radius)


class Circle(Shape):
    """A round shape."""

    kind = "circle"

    def __init__(self, radius):
        self.radius = radius

    @property
    def area(self):
        return math.pi * self.radius ** 2

    class Builder:
        def build(self):
            return Circle(1)


if __name__ == "__main__":
    print(make_circle(2).area)
