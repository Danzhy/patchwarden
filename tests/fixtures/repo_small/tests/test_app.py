from app.clean import double
from app.shapes import Square, is_big
from app.utils import append_item, is_missing, total


def test_total():
    assert total([1, 2, 3]) == 6


def test_is_missing():
    assert is_missing(None)
    assert not is_missing(0)


def test_append_item():
    assert append_item(1, []) == [1]


def test_square():
    assert Square(3).area() == 9
    assert is_big(Square(11))
    assert Square(2).describe() == "Square with side 2"


def test_double():
    assert double(4) == 8
