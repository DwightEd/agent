"""Same-origin web server. Run behind a TLS reverse proxy for shared deployments."""
import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import sqlite3
import threading
import time
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .auth import create_user, login, session_user
from .db import DB, dumps
from .engine import Engine
from .service import APIError, ROOT, Service


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, db, start_worker=True):
        super().__init__(address, Handler)
        self.db, self.service = db, Service(db)
        self.engine = Engine(db)
        self.attempts, self.auth_lock = {}, threading.Lock()
        self.allowed_hosts = {'localhost', '127.0.0.1', '::1', *os.environ.get('APP_HOSTS', '').split(',')}
        if address[0] not in ('0.0.0.0', '::'):
            self.allowed_hosts.add(address[0])
        if start_worker:
            self.engine.start()

    def server_close(self):
        self.engine.stop()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = 'ResearchOps/1.0'

    def log_message(self, fmt, *args):
        # Do not log paths: URLs can contain user content.
        if args and str(args[1] if len(args) > 1 else '').startswith('5'):
            print('HTTP server error', self.client_address[0], flush=True)

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def do_PUT(self):
        self.dispatch('PUT')

    def do_PATCH(self):
        self.dispatch('PATCH')

    def do_DELETE(self):
        self.dispatch('DELETE')

    def send(self, status, payload, mime='application/json', cookie=None, filename=None):
        raw = dumps(payload).encode() if mime == 'application/json' and not isinstance(payload, bytes) else payload
        self.send_response(status)
        self.send_header('Content-Type', mime + ('; charset=utf-8' if mime.startswith('text/') or mime == 'application/json' else ''))
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        if cookie:
            self.send_header('Set-Cookie', cookie)
        if filename:
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + urllib.parse.quote(filename))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def session_token(self):
        cookies = SimpleCookie()
        cookies.load(self.headers.get('Cookie', ''))
        return cookies['research_session'].value if 'research_session' in cookies else ''

    def cookie(self, token, age=604800):
        return f'research_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={age}' + ('; Secure' if os.environ.get('COOKIE_SECURE') == '1' else '')

    def dispatch(self, method):
        try:
            host = urllib.parse.urlsplit('//' + self.headers.get('Host', '')).hostname
            if host not in self.server.allowed_hosts:
                raise APIError(403, 'Host 未获准；请在 APP_HOSTS 配置访问域名')
            origin = self.headers.get('Origin')
            if method != 'GET' and origin and urllib.parse.urlsplit(origin).netloc != self.headers.get('Host'):
                raise APIError(403, '跨来源写入请求被拒绝')
            url = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(url.path)
            if not path.startswith('/api/'):
                if method != 'GET':
                    raise APIError(405, '不支持的方法')
                static = (ROOT / 'frontend' / (path.lstrip('/') or 'index.html')).resolve()
                if not static.is_relative_to(ROOT / 'frontend') or not static.is_file():
                    raise APIError(404, '页面不存在')
                return self.send(200, static.read_bytes(), mimetypes.guess_type(static.name)[0] or 'application/octet-stream')
            if method == 'GET' and path == '/api/health':
                return self.send(200, {'status': 'ok', 'version': '1.0.0'})
            if method == 'GET' and path == '/api/bootstrap':
                return self.send(200, {'needs_setup': not bool(self.server.db.one('SELECT id FROM users LIMIT 1')),
                                       'registration_enabled': os.environ.get('ALLOW_REGISTRATION') == '1'})
            body = {}
            if method != 'GET':
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 <= size <= 2_000_000:
                    raise APIError(413, '请求最大 2 MB')
                if size:
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        raise APIError(415, '请使用 application/json')
                    body = json.loads(self.rfile.read(size), parse_constant=lambda _: (_ for _ in ()).throw(ValueError('JSON 数值无效')))
                    if not isinstance(body, dict):
                        raise ValueError('请求体必须为 JSON 对象')
            if method == 'POST' and path in ('/api/auth/login', '/api/auth/setup', '/api/auth/register'):
                return self.authenticate(path, body)
            user = session_user(self.server.db, self.session_token())
            if not user:
                raise APIError(401, '请先登录')
            if method == 'POST' and path == '/api/auth/logout':
                self.server.db.execute('DELETE FROM sessions WHERE token=?', (hashlib.sha256(self.session_token().encode()).hexdigest(),))
                return self.send(200, {'logged_out': True}, cookie=self.cookie('', 0))
            result = self.server.service.handle(method, path, body, user, urllib.parse.parse_qs(url.query))
            if isinstance(result, tuple):
                return self.send(200, result[1], result[0], filename=result[2])
            self.send(200, result)
        except APIError as exc:
            self.send(exc.status, {'error': exc.message})
        except sqlite3.IntegrityError:
            self.send(409, {'error': '记录已存在，或该项目已有活动任务'})
        except (ValueError, TypeError, KeyError) as exc:
            self.send(400, {'error': str(exc)[:500]})
        except Exception as exc:
            print('Request failed:', type(exc).__name__, flush=True)
            self.send(500, {'error': '服务发生错误，请检查服务端日志'})

    def authenticate(self, path, body):
        address, now = self.client_address[0], time.time()
        with self.server.auth_lock:
            self.server.attempts = {ip: times for ip, times in self.server.attempts.items() if times and times[-1] > now - 60}
            attempts = [t for t in self.server.attempts.get(address, []) if t > now - 60]
            if len(attempts) >= 12:
                raise APIError(429, '登录尝试过于频繁，请一分钟后再试')
            self.server.attempts[address] = attempts + [now]
        email, password = body.get('email', ''), body.get('password', '')
        if not isinstance(email, str) or not isinstance(password, str):
            raise ValueError('邮箱和密码必须是字符串')
        if path != '/api/auth/login':
            if path == '/api/auth/setup' and address not in ('127.0.0.1', '::1'):
                expected = os.environ.get('BOOTSTRAP_TOKEN', '')
                if not expected or not hmac.compare_digest(str(body.get('bootstrap_token', '')), expected):
                    raise APIError(403, '远程首次初始化需要服务端 BOOTSTRAP_TOKEN')
            if path == '/api/auth/register' and not self.server.db.one('SELECT id FROM users LIMIT 1'):
                raise APIError(403, '请由管理员完成首次初始化')
            create_user(self.server.db, email, str(body.get('name', '')), password,
                        first_only=path == '/api/auth/setup', allow_registration=os.environ.get('ALLOW_REGISTRATION') == '1')
        token, user = login(self.server.db, email, password)
        with self.server.auth_lock:
            self.server.attempts.pop(address, None)
        self.send(200, user, cookie=self.cookie(token))


def main():
    parser = argparse.ArgumentParser(description='ResearchOps agent workbench')
    parser.add_argument('--host', default=os.environ.get('HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8000')))
    parser.add_argument('--db', default=os.environ.get('DATABASE_PATH', 'data/researchops.db'))
    args = parser.parse_args()
    db = DB(args.db)
    # One coordinator per database. This prevents startup recovery from stealing live tasks.
    lock = open(db.path + '.lock', 'a+b')
    try:
        try:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            import msvcrt
            lock.write(b'0')
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        parser.error('此数据库已有服务进程；请使用现有服务或不同的 --db 路径')
    server = AppServer((args.host, args.port), db)
    print(f'ResearchOps ready: http://{args.host}:{server.server_port}', flush=True)
    print('首次访问创建管理员；Ctrl+C 退出。数据库：' + db.path, flush=True)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        lock.close()


if __name__ == '__main__':
    main()
