"""Authenticated event export client with connection reuse."""
import http.client
import io
import json
import threading
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit

class EventClient:
    def __init__(self, key, base_url='http://receiver:8080', connection_factory=None):
        url = urlsplit(base_url)
        if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ('', '/'):
            raise ValueError('EVENT_BASE_URL must be an HTTP(S) origin')
        self.key = key
        self.base_url = base_url.rstrip('/')
        self.host, self.port = url.hostname, url.port
        self.connection_factory = connection_factory or (
            http.client.HTTPSConnection if url.scheme == 'https' else http.client.HTTPConnection
        )
        self.connection = None
        self.lock = threading.Lock()

    def close(self):
        if self.connection is not None:self.connection.close();self.connection=None

    def get(self,params):
        path='/wardogs/v1/events?'+urlencode(params)
        with self.lock:
            try:
                if self.connection is None:self.connection=self.connection_factory(self.host,self.port,timeout=5)
                self.connection.request('GET',path,headers={'Authorization':'Bearer '+self.key,
                    'Accept':'application/json','User-Agent':'WardogsQueryClient/1.1'})
                response=self.connection.getresponse()
                raw=response.read(4194305)
                if len(raw)>4194304:raise ValueError('response_too_large')
                if not 200<=response.status<300:
                    raise HTTPError(self.base_url+path,response.status,response.reason,response.headers,io.BytesIO(raw))
                if response.will_close:self.close()
                return json.loads(raw)
            except Exception:
                self.close()
                raise
