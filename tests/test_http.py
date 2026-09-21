import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from backend.db import DB
from backend.server import AppServer


class HTTPTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = AppServer(('127.0.0.1',0), DB(Path(self.tmp.name)/'http.db'), start_worker=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def request(self, path, body=None, method=None, headers=None):
        req = urllib.request.Request(self.url+path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={'Content-Type':'application/json',**(headers or {})},method=method)
        try:
            response=self.client.open(req)
        except urllib.error.HTTPError as e:
            response=e
        with response:
            raw=response.read()
            return response.status, json.loads(raw) if response.headers.get('Content-Type','').startswith('application/json') else raw, response.headers

    def test_auth_cookie_access_control_csrf_and_logout(self):
        self.assertEqual(self.request('/api/overview')[0],401)
        code,user,headers=self.request('/api/auth/setup',{'email':'admin@example.test','name':'Admin','password':'long-test-password'})
        self.assertEqual(code,200)
        self.assertEqual(user['role'],'admin')
        self.assertIn('HttpOnly',headers['Set-Cookie'])
        self.assertIn('SameSite=Strict',headers['Set-Cookie'])
        code,p,_=self.request('/api/demo',{})
        self.assertEqual(code,200)
        self.assertEqual(self.request('/api/projects/'+p['id'])[0],200)
        self.assertEqual(self.request('/api/demo',{},headers={'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('/api/settings')[0],200)
        self.assertEqual(self.request('/api/auth/setup',{'email':'x@y.z','name':'X','password':'long-test-password'})[0],400)
        self.assertEqual(self.request('/api/auth/logout',{})[0],200)
        self.assertEqual(self.request('/api/overview')[0],401)

    def test_static_paths_and_request_validation(self):
        self.assertEqual(self.request('/')[0],200)
        self.assertEqual(self.request('/../backend/db.py')[0],404)
        self.assertEqual(self.request('/api/health',headers={'Host':'attacker.example'})[0],403)
        code,_,_=self.request('/api/auth/login',['bad request'])
        self.assertEqual(code,400)


if __name__ == '__main__':
    unittest.main()
