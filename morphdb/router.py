"""A tiny path-template router with no dependencies.

Templates use ``{name}`` segments, e.g. ``/objects/{type}/{guid}``. A trailing
slash is optional. Handlers receive a :class:`Request` and return either a
payload (rendered as 200) or a ``(status, payload)`` tuple.
"""

import re


class Request:
    def __init__(self, method, path, params, query, body, headers):
        self.method = method
        self.path = path
        self.params = params      # dict from path template
        self.query = query        # dict, single (last) value per key
        self.body = body          # parsed JSON (dict/list) or {}
        self.headers = headers

    def query_bool(self, key, default=False):
        val = self.query.get(key)
        if val is None:
            return default
        return str(val).strip().lower() in ("1", "true", "yes", "y", "on")


class Router:
    def __init__(self):
        self._routes = []  # (method, compiled_regex, handler)

    def add(self, method, template, handler):
        pattern = re.sub(r"{(\w+)}", r"(?P<\1>[^/]+)", template)
        regex = re.compile("^" + pattern + "/?$")
        self._routes.append((method.upper(), regex, handler))

    def route(self, method, template):
        def deco(fn):
            self.add(method, template, fn)
            return fn
        return deco

    def match(self, method, path):
        """Return ``(handler, params, path_matched)``.

        ``path_matched`` distinguishes a 404 (no path matched) from a 405
        (path matched but not for this method).
        """
        # HEAD is served by the GET handler (the server omits the body).
        match_method = "GET" if method == "HEAD" else method
        path_matched = False
        for m, regex, handler in self._routes:
            mo = regex.match(path)
            if mo:
                path_matched = True
                if m == match_method:
                    return handler, mo.groupdict(), True
        return None, None, path_matched
