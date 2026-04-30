#!/usr/bin/env python3
"""
web_server.py -- Hub homepage server.

Serves /etc/hub/web/ as static files.  Mount streams.json into that
directory at runtime; the browser page fetches it at /streams.json.

Environment variables:
  WEB_PORT  HTTP listening port (default: 8080)
  WEB_DIR   Static file root   (default: /etc/hub/web)
"""
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler

WEB_DIR = os.environ.get('WEB_DIR', '/etc/hub/web')


class Router(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=WEB_DIR, **kwargs)

    def translate_path(self, path):
        if not path.split('?', 1)[0].rstrip('/'):
            path = '/index.html'
        return super().translate_path(path)

    def log_message(self, fmt, *args):
        pass


if __name__ == '__main__':
    port = int(os.environ.get('WEB_PORT', '8080'))
    HTTPServer(('', port), Router).serve_forever()
