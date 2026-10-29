import json
import requests

try:
    import urlparse
except ImportError:
    from urllib import parse as urlparse

from .exceptions import JSONReadError, NotFoundError, RequestError, TokenError, TooManyRequestsError

GET = "GET"
POST = "POST"
DELETE = "DELETE"
PUT = "PUT"
PATCH = "PATCH"
HARAVAN_ACCESS_TOKEN_HEADER = "Authorization"

class BaseAPI:
    def __init__(self, token, base_url, **kwargs):
        self._token = token
        self._base_url = base_url
        self._session = requests.Session()
        self._token_required = True

    def without_token(self):
        self._token_required = False
        return self

    def get(self, end_point, params=dict()):
        return self.__call_api(end_point, GET, params)

    def patch(self, end_point, params=dict()):
        return self.__call_api(end_point, PATCH, params)

    def post(self, end_point, params=dict()):
        return self.__call_api(end_point, POST, params)

    def put(self, end_point, params=dict()):
        return self.__call_api(end_point, PUT, params)

    def delete(self, end_point, params=dict()):
        return self.__call_api(end_point, DELETE, params)

    def __call_api(self, end_point, type=GET, params=dict()):
        req = self.__perform_request(end_point, type, params)
        if req.status_code == 204:
            return True

        if req.status_code == 404:
            raise NotFoundError()

        if req.status_code == 429:
            raise TooManyRequestsError()

        try:
            data = req.json()
        except ValueError as e:
            raise JSONReadError("JSON could not be parsed: %s" % e)

        if not req.ok:
            raise RequestError(data)

        return data

    def __perform_request(self, end_point, type=GET, params=dict()):
        if not self.token and self.token_required:
            raise TokenError("No token provided")

        url = urlparse.urljoin(self.base_url, end_point)

        def identity(x):
            return x

        def json_dumps(x):
            return json.dumps(x)

        lookup = {
            GET: (
                self._session.get,
                {"Content-type": "application/json"},
                "params",
                identity,
            ),
            PATCH: (
                requests.patch,
                {"Content-type": "application/json"},
                "data",
                json_dumps,
            ),
            POST: (
                requests.post,
                {"Content-type": "application/json"},
                "data",
                json_dumps,
            ),
            PUT: (
                self._session.put,
                {"Content-type": "application/json"},
                "data",
                json_dumps,
            ),
            DELETE: (
                self._session.delete,
                {"content-type": "application/json"},
                "data",
                json_dumps,
            ),
        }

        requests_method, headers, payload, transform = lookup[type]
        kwargs = {"headers": headers, payload: transform(params)}

        if self.token_required:
            headers.update({HARAVAN_ACCESS_TOKEN_HEADER: self.token})

        req = requests_method(url, **kwargs)
        return req

    @property
    def token(self):
        return self._token

    @token.setter
    def token(self, token):
        self._token = token

    @property
    def base_url(self):
        return self._base_url

    @base_url.setter
    def base_url(self, base_url):
        self._base_url = base_url

    @property
    def token_required(self):
        return self._token_required

    @token_required.setter
    def token_required(self, token_required):
        self._token_required = token_required
