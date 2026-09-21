class Square(object):
    def __init__(self, side):
        self.side = side

    def area(self):
        return self.side * self.side

    def describe(self):
        return "Square with side {}".format(self.side)


def is_big(sq):
    if sq.area() > 100:
        return True
    else:
        return False
