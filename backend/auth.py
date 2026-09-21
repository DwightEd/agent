import hashlib
import hmac
import secrets
import time

from .db import uid


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ':' + digest


def create_user(db, email, name, password, first_only=False, allow_registration=True):
    email = email.strip().lower()
    if '@' not in email or len(email) > 200 or not 10 <= len(password) <= 256 or not name.strip():
        raise ValueError('邮箱格式不正确、姓名为空，或密码长度不在 10–256 字符之间')
    # Serialize setup to avoid concurrent first-user admin creation.
    with db.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        count = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        if count and (first_only or not allow_registration):
            raise ValueError('管理员已创建；当前不允许注册')
        user_id = uid('usr')
        conn.execute('INSERT INTO users VALUES(?,?,?,?,?,?)',
                     (user_id, email, name.strip()[:80], password_hash(password),
                      'admin' if count == 0 else 'member', time.time()))
    return db.one('SELECT id,email,name,role FROM users WHERE id=?', (user_id,))


def login(db, email, password):
    if len(password) > 256:
        raise ValueError('邮箱或密码不正确')
    user = db.one('SELECT * FROM users WHERE email=?', (email.strip().lower(),))
    # Perform a hash even for nonexistent users.
    stored = user['password'] if user else '0' * 32 + ':' + '0' * 128
    if not hmac.compare_digest(password_hash(password, stored.split(':')[0]), stored):
        raise ValueError('邮箱或密码不正确')
    token = secrets.token_urlsafe(32)
    db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
    db.execute('INSERT INTO sessions VALUES(?,?,?)',
               (hashlib.sha256(token.encode()).hexdigest(), user['id'], time.time() + 86400 * 7))
    return token, {k: user[k] for k in ('id', 'email', 'name', 'role')}


def session_user(db, token):
    return db.one('''SELECT u.id,u.email,u.name,u.role FROM users u JOIN sessions s ON s.user_id=u.id
                     WHERE s.token=? AND s.expires>?''',
                  (hashlib.sha256(token.encode()).hexdigest(), time.time()))
