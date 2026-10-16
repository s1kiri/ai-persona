"""Loopback page navigation is public; private reads and commands stay same-origin."""


def local_request(request):
    origin = 'http://127.0.0.1:' + str(request.server.server_port)
    return (request.headers.get('Host') == origin[7:] and
            request.headers.get('Origin', origin) == origin and
            request.headers.get('Sec-Fetch-Site', 'none') not in ('cross-site', 'same-site'))


def local_navigation(request):
    """A click from the other local port must be able to establish its own cookie."""
    return (request.path == '/' and
            request.headers.get('Host') == '127.0.0.1:' + str(request.server.server_port) and
            request.headers.get('Sec-Fetch-Mode', 'navigate') == 'navigate' and
            request.headers.get('Sec-Fetch-Dest', 'document') == 'document')
