"""Request-level error types."""


class BodyTooLarge(Exception):
    def __init__(self, length):
        super().__init__(length)
        self.length = length


class BadJSON(Exception):
    pass
