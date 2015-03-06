import json
import datetime
import hmac
import hashlib
import base64
import six

from netaddr.ip import IPAddress, IPNetwork

from twisted.python import log
from twisted.web.resource import Resource
from twisted.web import server

from autobahn.wamp.types import PublishOptions


from autobahn.adapter.rest.common import _CommonResource

class CallerResource(_CommonResource):

    """
    A HTTP/POST to WAMP PubSub bridge.

    Config:

       "transports": [
          {
             "type": "web",
             "endpoint": {
                "type": "tcp",
                "port": 8080
             },
             "paths": {
                "/": {
                   "type": "static",
                   "directory": ".."
                },
                "ws": {
                   "type": "websocket"
                },
                "push": {
                   "type": "pusher",
                   "realm": "realm1",
                   "role": "anonymous",
                   "options": {
                      "key": "foobar",
                      "secret": "secret",
                      "post_body_limit": 8192,
                      "timestamp_delta_limit": 10,
                      "require_ip": ["192.168.1.1/255.255.255.0", "127.0.0.1"],
                      "require_tls": false
                   }
                }
             }
          }
       ]

    Test:

       curl -H "Content-Type: application/json" -d '{"topic": "com.myapp.topic1", "args": ["Hello, world"]}' http://127.0.0.1:8080/push
    """

    def _process(self, request, body):

        try:
            event = json.loads(body)
        except Exception as e:
            return self._deny_request(request, 400, "invalid request event - HTTP/POST body must be valid JSON: {0}".format(e))

        if not isinstance(event, dict):
            return self._deny_request(request, 400, "invalid request event - HTTP/POST body must be JSON dict")

        if 'procedure' not in event:
            return self._deny_request(request, 400, "invalid request event - missing 'procedure' in HTTP/POST body")

        procedure = event.pop('procedure')

        args = event.pop('args', [])
        kwargs = event.pop('kwargs', {})
        options = event.pop('options', {})

        d = self._session.call(procedure, *args, **kwargs)

        def on_call_ok(res):
            res = {'response': res}
            if self._debug:
                log.msg("CallerResource - request succeeded with result {0}".format(res))
            body = json.dumps(res, separators=(',', ':'))
            if six.PY3:
                body = body.encode('utf8')

                request.setHeader('content-type', 'application/json; charset=UTF-8')
                request.setHeader('cache-control', 'no-store, no-cache, must-revalidate, max-age=0')
                request.setResponseCode(200)
                request.write(body)
                request.finish()

        def on_call_error(err):
            emsg = "PusherResource - request failed with error {0}\n".format(err.value)
            if self._debug:
                log.msg(emsg)
            request.setResponseCode(400)
            request.write(emsg)
            request.finish()

            d.addCallbacks(on_call_ok, on_call_error)

        return server.NOT_DONE_YET
