"""Application API, independent of the HTTP server for integration testing."""
import base64
import hashlib
import io
import json
import os
import sqlite3
import time
import urllib.parse
import zipfile
from pathlib import Path

from .auth import create_user, password_hash
from .benchmark import parse_csv, tokens
from .db import dumps, uid
from .engine import default_config, enqueue
from .research_tools import add_source

ROOT = Path(__file__).resolve().parent.parent


class APIError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)


def unpack(row, *fields):
    if row:
        for field in fields:
            row[field] = json.loads(row[field])
    return row


def text_value(body, key, minimum=1, maximum=4000):
    value = body.get(key, '')
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise ValueError(f'{key} 长度必须为 {minimum}–{maximum} 字符')
    return value.strip()


class Service:
    def __init__(self, db):
        self.db = db

    def project(self, project_id, user):
        result = self.db.one('SELECT * FROM projects WHERE id=? AND owner_id=?', (project_id, user['id']))
        if not result:
            raise APIError(404, '项目不存在')
        return result

    def run(self, run_id, user):
        result = self.db.one('''SELECT r.* FROM runs r JOIN projects p ON p.id=r.project_id
                             WHERE r.id=? AND p.owner_id=?''', (run_id, user['id']))
        if not result:
            raise APIError(404, '任务不存在')
        return result

    def admin(self, user):
        if user['role'] != 'admin':
            raise APIError(403, '需要管理员权限')

    def create_project(self, user, name, question, description=''):
        now, project_id = time.time(), uid('prj')
        self.db.execute('INSERT INTO projects VALUES(?,?,?,?,?,?,?,?)',
                        (project_id, user['id'], name, question, description, 0, now, now))
        return self.project(project_id, user)

    def demo(self, user):
        project = self.create_project(user, '企业知识库 · 检索技术选型',
                                      '为企业知识库选择可解释的检索基线，比较 BM25、TF-IDF 与词项重叠，并核对证据与实测效果。',
                                      '示例项目 · 使用原创演示资料和合成 CSV；可完整体验 agent 闭环。')
        for source in json.loads((ROOT / 'examples/sources.json').read_text()):
            add_source(self.db, project['id'], kind='demo', **source)
        content = (ROOT / 'examples/retrieval.csv').read_text()
        dataset = self.add_dataset(project['id'], '演示检索候选集.csv', content)
        return {**project, 'dataset_id': dataset['id']}

    def add_dataset(self, project_id, name, content):
        rows, groups = parse_csv(content)
        metadata = {'rows': len(rows), 'queries': len(groups), 'sha256': hashlib.sha256(content.encode()).hexdigest()}
        dataset_id = uid('ds')
        self.db.execute('INSERT INTO datasets VALUES(?,?,?,?,?,?)',
                        (dataset_id, project_id, name, content, dumps(metadata), time.time()))
        return {'id': dataset_id, 'name': name, 'metadata': metadata}

    def handle(self, method, path, body, user, query):
        db = self.db
        parts = path.strip('/').split('/')[1:]
        if parts == ['me']:
            return user
        if parts == ['overview'] and method == 'GET':
            projects = db.all('''SELECT p.*, (SELECT COUNT(*) FROM sources s WHERE s.project_id=p.id) source_count,
                               (SELECT COUNT(*) FROM runs r WHERE r.project_id=p.id) run_count
                               FROM projects p WHERE owner_id=? ORDER BY updated DESC''', (user['id'],))
            runs = db.all('''SELECT r.id,r.project_id,p.name project_name,r.goal,r.mode,r.status,r.stage,r.created,r.updated,
                           r.calls,r.input_tokens,r.output_tokens,r.error FROM runs r JOIN projects p ON p.id=r.project_id
                           WHERE p.owner_id=? ORDER BY r.created DESC LIMIT 100''', (user['id'],))
            stats = db.one('''SELECT COUNT(*) total, SUM(CASE WHEN r.status='completed' THEN 1 ELSE 0 END) completed,
                             SUM(r.calls) calls,SUM(r.input_tokens+r.output_tokens) tokens FROM runs r
                             JOIN projects p ON p.id=r.project_id WHERE p.owner_id=?''', (user['id'],))
            return {'projects': projects, 'runs': runs, 'stats': stats,
                    'approvals': [r for r in runs if r['status'] == 'awaiting_approval']}
        if parts == ['settings']:
            if method == 'GET':
                return {**default_config(db), 'key_configured': bool(os.environ.get('LLM_API_KEY')),
                        'registration_enabled': os.environ.get('ALLOW_REGISTRATION') == '1', 'database': 'SQLite / WAL',
                        'pdf_available': self.pdf_available()}
            if method == 'PUT':
                self.admin(user)
                config = {key: body.get(key) for key in ('base_url', 'model', 'max_calls', 'max_output_tokens', 'allow_arxiv')}
                base = urllib.parse.urlparse(text_value(body, 'base_url', 8, 300))
                if base.scheme not in ('http', 'https') or not base.hostname or base.username or base.password or base.query or base.fragment:
                    raise ValueError('API Base URL 无效；凭据只允许通过服务端环境变量配置')
                if base.scheme == 'http' and base.hostname not in ('localhost', '127.0.0.1', '::1', 'host.docker.internal'):
                    raise ValueError('远端模型服务必须使用 HTTPS；本机服务可使用 HTTP')
                config['model'] = text_value(body, 'model', 1, 100)
                if type(config['max_calls']) is not int or not 8 <= config['max_calls'] <= 256:
                    raise ValueError('调用上限必须为 8–256')
                if type(config['max_output_tokens']) is not int or not 500 <= config['max_output_tokens'] <= 16000:
                    raise ValueError('输出 token 上限必须为 500–16000')
                if type(config['allow_arxiv']) is not bool:
                    raise ValueError('allow_arxiv 必须为布尔值')
                db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', ('model', dumps(config)))
                return {'saved': True}
        if parts == ['admin', 'users']:
            self.admin(user)
            if method == 'GET':
                return db.all('SELECT id,email,name,role,created FROM users ORDER BY created')
            if method == 'POST':
                return create_user(db, text_value(body, 'email'), text_value(body, 'name', 1, 80), text_value(body, 'password', 10, 256))
        if parts == ['profile'] and method == 'PATCH':
            name = text_value(body, 'name', 1, 80)
            db.execute('UPDATE users SET name=? WHERE id=?', (name, user['id']))
            return {**user, 'name': name}
        if parts == ['projects'] and method == 'POST':
            return self.create_project(user, text_value(body, 'name', 1, 120), text_value(body, 'question', 5, 4000), text_value(body, 'description', 0, 2000))
        if parts == ['demo'] and method == 'POST':
            return self.demo(user)
        if len(parts) >= 2 and parts[0] == 'projects':
            project = self.project(parts[1], user)
            pid = project['id']
            if len(parts) == 2:
                if method == 'GET':
                    return {**project,
                            'sources': [unpack(r, 'metadata') for r in db.all('SELECT * FROM sources WHERE project_id=? ORDER BY created', (pid,))],
                            'datasets': [unpack(r, 'metadata') for r in db.all('SELECT id,name,metadata,created FROM datasets WHERE project_id=? ORDER BY created', (pid,))],
                            'runs': db.all('SELECT id,goal,mode,status,stage,error,created,updated,calls,input_tokens,output_tokens FROM runs WHERE project_id=? ORDER BY created DESC', (pid,)),
                            'schedule': db.one('SELECT * FROM schedules WHERE project_id=?', (pid,))}
                if method == 'PATCH':
                    name = text_value(body, 'name', 1, 120) if 'name' in body else project['name']
                    question = text_value(body, 'question', 5, 4000) if 'question' in body else project['question']
                    archived = int(bool(body.get('archived', project['archived'])))
                    db.execute('UPDATE projects SET name=?,question=?,archived=?,updated=? WHERE id=?', (name, question, archived, time.time(), pid))
                    if archived:
                        db.execute('UPDATE schedules SET enabled=0 WHERE project_id=?', (pid,))
                    return self.project(pid, user)
                if method == 'DELETE':
                    if not project['archived'] or db.one("SELECT id FROM runs WHERE project_id=? AND status IN ('queued','running','awaiting_approval')", (pid,)):
                        raise ValueError('请先结束任务并归档项目，再删除')
                    db.execute('DELETE FROM projects WHERE id=?', (pid,))
                    return {'deleted': True}
            if len(parts) == 3 and parts[2] == 'sources' and method == 'POST':
                if db.one('SELECT COUNT(*) n FROM sources WHERE project_id=?', (pid,))['n'] >= 100:
                    raise ValueError('每个项目最多 100 条导入资料，请分项目管理')
                content = body.get('content', '')
                kind = 'note'
                if body.get('pdf_base64'):
                    if not self.pdf_available():
                        raise ValueError('请先安装 PDF 扩展：pip install -r requirements-pdf.txt')
                    import pymupdf
                    raw = base64.b64decode(body['pdf_base64'], validate=True)
                    if len(raw) > 1_400_000:
                        raise ValueError('PDF 最大 1.4 MB；较大文件请先提取文本')
                    with pymupdf.open(stream=raw, filetype='pdf') as doc:
                        if len(doc) > 80:
                            raise ValueError('PDF 最多 80 页')
                        content = '\n'.join(page.get_text() for page in doc)[:60000]
                    kind = 'pdf'
                return unpack(add_source(db, pid, text_value(body, 'title', 1, 300), content, kind,
                                         body.get('url', ''), {'coverage': 'user_import'}), 'metadata')
            if len(parts) == 4 and parts[2] == 'sources' and method == 'DELETE':
                db.execute('DELETE FROM sources WHERE id=? AND project_id=?', (parts[3], pid))
                return {'deleted': True, 'note': '历史任务仍保留资料快照'}
            if len(parts) == 3 and parts[2] == 'datasets' and method == 'POST':
                return self.add_dataset(pid, text_value(body, 'name', 1, 120), text_value(body, 'content', 1, 1_400_000))
            if len(parts) == 4 and parts[2] == 'datasets' and method == 'DELETE':
                db.execute('DELETE FROM datasets WHERE id=? AND project_id=?', (parts[3], pid))
                return {'deleted': True}
            if len(parts) == 3 and parts[2] == 'runs' and method == 'POST':
                return {'id': enqueue(db, project, body.get('mode', 'demo'), body.get('dataset_id'), body.get('goal'), body.get('methods'), body.get('k', 3))}
            if len(parts) == 3 and parts[2] == 'schedule' and method == 'PUT':
                hours, mode = body.get('interval_hours'), body.get('mode', 'demo')
                if type(hours) is not int or not 1 <= hours <= 720 or mode not in ('demo', 'live'):
                    raise ValueError('周期必须为 1–720 小时，模式为 demo/live')
                if project['archived']:
                    raise ValueError('归档项目不能启用定期研究')
                enabled = int(bool(body.get('enabled', True)))
                if enabled and mode == 'live' and 'api.openai.com' in default_config(db)['base_url'] and not os.environ.get('LLM_API_KEY'):
                    raise ValueError('请先配置模型服务密钥，再启用真实模式定期研究')
                db.execute('''INSERT INTO schedules VALUES(?,?,?,?,?,?,?) ON CONFLICT(project_id)
                           DO UPDATE SET mode=excluded.mode,interval_hours=excluded.interval_hours,next_at=excluded.next_at,enabled=excluded.enabled''',
                           (uid('schedule'), pid, mode, hours, time.time() + hours*3600, enabled, time.time()))
                return db.one('SELECT * FROM schedules WHERE project_id=?', (pid,))
            if len(parts) == 3 and parts[2] == 'messages':
                if method == 'GET':
                    return db.all('SELECT * FROM messages WHERE project_id=? ORDER BY created LIMIT 100', (pid,))
                if method == 'POST':
                    question = text_value(body, 'content', 2, 2000)
                    terms = set(tokens(question))
                    sources = db.all('SELECT * FROM sources WHERE project_id=?', (pid,))
                    ranked = sorted(sources, key=lambda s: len(terms & set(tokens(s['title'] + s['content']))), reverse=True)
                    ranked = [s for s in ranked if terms & set(tokens(s['title'] + s['content']))][:3]
                    answer = '资料定位（关键词检索，未调用模型）：\n\n' + '\n\n'.join(f"【{s['title']}】\n{s['content'][:700]}" for s in ranked) if ranked else '没有找到匹配资料。请导入相关原文，或发起真实研究任务进行检索与综合。'
                    for role, content in [('user', question), ('assistant', answer)]:
                        db.execute('INSERT INTO messages VALUES(?,?,?,?,?)', (uid('msg'), pid, role, content, time.time()))
                    return {'content': answer, 'source_ids': [s['id'] for s in ranked]}
        if len(parts) >= 2 and parts[0] == 'runs':
            run = self.run(parts[1], user)
            rid = run['id']
            if len(parts) == 2 and method == 'GET':
                unpack(run, 'state', 'config')
                if run['config'].get('dataset'):
                    run['config']['dataset'] = {k: v for k, v in run['config']['dataset'].items() if k != 'content'}
                return {**run,
                        'steps': [unpack(r, 'output') for r in db.all('SELECT * FROM steps WHERE run_id=? ORDER BY started', (rid,))],
                        'approval': unpack(db.one('SELECT * FROM approvals WHERE run_id=?', (rid,)), 'request'),
                        'artifacts': db.all('SELECT id,name,mime,created FROM artifacts WHERE run_id=? ORDER BY name', (rid,)),
                        'sources': db.sources(rid)}
            if len(parts) == 3 and parts[2] == 'events' and method == 'GET':
                after = max(0, int(query.get('after', ['0'])[0]))
                return [unpack(r, 'data') for r in db.all('SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT 500', (rid, after))]
            if len(parts) == 3 and parts[2] == 'cancel' and method == 'POST':
                with db.connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    status = conn.execute('SELECT status FROM runs WHERE id=?', (rid,)).fetchone()[0]
                    if status not in ('queued', 'running', 'awaiting_approval'):
                        raise ValueError('此任务已结束')
                    new_status = 'cancelled' if status != 'running' else 'running'
                    conn.execute('UPDATE runs SET cancel_requested=1,status=?,updated=? WHERE id=?', (new_status, time.time(), rid))
                db.event(rid, 'cancel_requested', '', '已请求取消；正在执行的外部请求会在超时或返回后终止')
                return {'cancel_requested': True}
            if len(parts) == 3 and parts[2] == 'retry' and method == 'POST':
                if run['status'] not in ('failed', 'cancelled'):
                    raise ValueError('仅失败或取消的任务可重试')
                if self.project(run['project_id'], user)['archived']:
                    raise ValueError('请先恢复归档项目')
                db.execute("UPDATE runs SET status='queued',cancel_requested=0,error='',updated=? WHERE id=?", (time.time(), rid))
                db.event(rid, 'retry', '', '从最后一个已完成阶段继续；模型调用额度不重置')
                return {'queued': True}
            if len(parts) == 3 and parts[2] == 'approval' and method == 'POST':
                decision = body.get('decision')
                if decision not in ('approved', 'rejected'):
                    raise ValueError('decision 必须为 approved/rejected')
                note = text_value(body, 'note', 0, 1000)
                with db.connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    current = conn.execute('SELECT status FROM runs WHERE id=?', (rid,)).fetchone()[0]
                    if current != 'awaiting_approval':
                        raise ValueError('任务当前不等待审批')
                    changed = conn.execute("UPDATE approvals SET status=?,decision=?,decided_by=?,decided=? WHERE run_id=? AND status='pending'",
                                           (decision, note, user['id'], time.time(), rid)).rowcount
                    if changed != 1:
                        raise ValueError('此审批已处理')
                    conn.execute("UPDATE runs SET status='queued',updated=? WHERE id=?", (time.time(), rid))
                db.event(rid, 'approval_decision', 'experimenter', '实验' + ('已批准' if decision == 'approved' else '已拒绝'), {'decision': decision, 'note': note})
                return {'queued': True}
            if len(parts) == 3 and parts[2] == 'export' and method == 'GET':
                artifacts = db.all('SELECT * FROM artifacts WHERE run_id=?', (rid,))
                if not artifacts:
                    raise ValueError('任务尚无可导出的产物')
                data = io.BytesIO()
                with zipfile.ZipFile(data, 'w', zipfile.ZIP_DEFLATED) as archive:
                    for artifact in artifacts:
                        archive.writestr(artifact['name'], artifact['content'])
                return ('application/zip', data.getvalue(), rid + '.zip')
        if len(parts) == 2 and parts[0] == 'artifacts' and method == 'GET':
            artifact = db.one('SELECT * FROM artifacts WHERE id=?', (parts[1],))
            if not artifact:
                raise APIError(404, '文件不存在')
            self.run(artifact['run_id'], user)
            return (artifact['mime'], artifact['content'].encode(), artifact['name'])
        raise APIError(404, '接口不存在')

    @staticmethod
    def pdf_available():
        import importlib.util
        return importlib.util.find_spec('pymupdf') is not None
