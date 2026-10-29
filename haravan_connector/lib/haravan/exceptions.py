class Error(Exception):
    pass

class TokenError(Error):
    pass

class DataReadError(Error):
    pass

class JSONReadError(Error):
    pass

class NotFoundError(Error):
    pass

class RequestError(Error):
    pass

class ServerError(Error):
    """Raised when the server responds with a 5xx status code and no body"""

    pass

class TooManyRequestsError(Error):
    pass
