class ReviewError(Exception):
    """Only controlled public messages may cross the worker boundary."""
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)
