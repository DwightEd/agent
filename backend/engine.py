"""Durable workflow coordinator: tool loops, checkpoints, approval and review cycles."""
import hashlib
import json
import os
import sqlite3
import threading
import time

from .agents import Agent
from .db import dumps, uid
from .providers import Provider
from .reports import build_report
from .research_tools import Toolset, verify_claims

ROLES = ['planner', 'researcher', 'analyst', 'experimenter', 'reviewer', 'writer']


class Cancelled(Exception):
    pass


def default_config(db):
    return db.setting('model', {
        'base_url': os.environ.get('LLM_BASE_URL', 'https://api.openai.com/v1'),
        'model': os.environ.get('LLM_MODEL', 'gpt-4.1-mini'),
        'max_calls': 64, 'max_output_tokens': 2500, 'allow_arxiv': True,
    })


def enqueue(db, project, mode='demo', dataset_id=None, goal=None, methods=None, k=3):
    if mode not in ('demo', 'live'):
        raise ValueError('mode 必须为 demo 或 live')
    if project['archived']:
        raise ValueError('归档项目不能创建任务')
    goal = goal or project['question']
    if not isinstance(goal, str) or not 5 <= len(goal.strip()) <= 4000:
        raise ValueError('研究目标必须为 5–4000 字符')
    methods = methods or ['overlap', 'tfidf', 'bm25']
    if not isinstance(methods, list) or not methods or any(m not in ('overlap', 'tfidf', 'bm25') for m in methods):
        raise ValueError('实验方法必须为 overlap/tfidf/bm25')
    if type(k) is not int or not 1 <= k <= 20:
        raise ValueError('k 必须为 1–20')
    dataset = db.one('SELECT * FROM datasets WHERE id=? AND project_id=?', (dataset_id, project['id'])) if dataset_id else None
    if dataset_id and not dataset:
        raise ValueError('数据集不存在或不属于此项目')
    config = {**default_config(db), 'dataset': dataset, 'methods': list(dict.fromkeys(methods)), 'k': k}
    if mode == 'live' and 'api.openai.com' in config['base_url'] and not os.environ.get('LLM_API_KEY'):
        raise ValueError('真实模式需要在服务端设置 LLM_API_KEY，或配置本地兼容服务')
    run_id, now = uid('run'), time.time()
    try:
        with db.connect() as conn:
            conn.execute('INSERT INTO runs(id,project_id,goal,mode,status,config,created,updated) VALUES(?,?,?,?,?,?,?,?)',
                         (run_id, project['id'], goal.strip(), mode, 'queued', dumps(config), now, now))
            sources = conn.execute('SELECT * FROM sources WHERE project_id=? ORDER BY created LIMIT 100', (project['id'],)).fetchall()
            for source in sources:
                conn.execute('INSERT INTO run_sources VALUES(?,?,?)', (run_id, source['id'], dumps(dict(source))))
    except sqlite3.IntegrityError:
        raise ValueError('该项目已有运行中或等待审批的任务；请先完成或取消') from None
    db.event(run_id, 'queued', '', '任务已排队；资料和数据集已快照', {'mode': mode})
    return run_id


class Engine:
    def __init__(self, db):
        self.db = db
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        self.db.execute("UPDATE runs SET status='queued',updated=? WHERE status='running'", (time.time(),))
        self.thread = threading.Thread(target=self.loop, name='research-worker', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=3)

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self.schedule_due()
                with self.db.connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    row = conn.execute("SELECT id FROM runs WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
                    if row:
                        conn.execute("UPDATE runs SET status='running',updated=? WHERE id=?", (time.time(), row['id']))
                if row:
                    self.process(row['id'])
                    continue
            except Exception as exc:
                # Keep the worker alive for other projects. Task errors are persisted by process().
                print('Worker maintenance error:', type(exc).__name__, flush=True)
            self.stop_event.wait(0.5)

    def schedule_due(self):
        for schedule in self.db.all('SELECT * FROM schedules WHERE enabled=1 AND next_at<=?', (time.time(),)):
            project = self.db.one('SELECT * FROM projects WHERE id=?', (schedule['project_id'],))
            try:
                enqueue(self.db, project, mode=schedule['mode'])
            except ValueError:
                # Do not create overlapping jobs; retry due schedule in a minute.
                self.db.execute('UPDATE schedules SET next_at=? WHERE id=?', (time.time() + 60, schedule['id']))
            else:
                self.db.execute('UPDATE schedules SET next_at=? WHERE id=?',
                                (time.time() + schedule['interval_hours'] * 3600, schedule['id']))

    def process(self, run_id):
        run = self.db.one('SELECT * FROM runs WHERE id=?', (run_id,))
        run['config'], state = json.loads(run['config']), json.loads(run['state'])

        def check_cancel():
            if self.stop_event.is_set():
                raise Cancelled('服务正在停止；下次启动可重试任务')
            row = self.db.one('SELECT cancel_requested FROM runs WHERE id=?', (run_id,))
            if row['cancel_requested']:
                raise Cancelled('用户取消任务')

        def usage(value):
            if value is None:
                with self.db.connect() as conn:
                    conn.execute('BEGIN IMMEDIATE')
                    calls = conn.execute('SELECT calls FROM runs WHERE id=?', (run_id,)).fetchone()[0]
                    if calls >= run['config']['max_calls']:
                        raise ValueError('已达到任务模型调用上限；请创建新任务并调整调用预算')
                    conn.execute('UPDATE runs SET calls=calls+1,updated=? WHERE id=?', (time.time(), run_id))
            else:
                self.db.execute('UPDATE runs SET input_tokens=input_tokens+?,output_tokens=output_tokens+? WHERE id=?',
                                (max(0, int(value.get('prompt_tokens', 0))), max(0, int(value.get('completion_tokens', 0))), run_id))

        tools = Toolset(self.db, run, state, check_cancel)
        agent = Agent(self.db, run, state, tools, Provider(run['config'], usage, check_cancel))

        def save():
            self.db.execute('UPDATE runs SET state=?,updated=? WHERE id=?', (dumps(state), time.time(), run_id))

        def step(role, iteration=0):
            check_cancel()
            if role in state:
                return state[role]
            self.db.execute('UPDATE runs SET stage=?,updated=? WHERE id=?', (role, time.time(), run_id))
            self.db.execute('''INSERT INTO steps VALUES(?,?,?,?,?,?,?,NULL)
                              ON CONFLICT(run_id,role,iteration) DO UPDATE SET status='running',started=excluded.started,finished=NULL''',
                            (uid('step'), run_id, role, iteration, 'running', '{}', time.time()))
            self.db.event(run_id, 'stage_start', role, '开始执行 ' + role, {'iteration': iteration})
            output = agent.run_role(role, iteration)
            check_cancel()
            state[role] = output
            # Output and checkpoint commit atomically: finished stages survive process restarts.
            with self.db.connect() as conn:
                conn.execute('UPDATE steps SET status=?,output=?,finished=? WHERE run_id=? AND role=? AND iteration=?',
                             ('completed', dumps(output), time.time(), run_id, role, iteration))
                conn.execute('UPDATE runs SET state=?,updated=? WHERE id=?', (dumps(state), time.time(), run_id))
            self.db.event(run_id, 'stage_complete', role, role + ' 已完成', {'iteration': iteration, 'output': output})
            return output

        try:
            self.db.event(run_id, 'running', '', '工作进程接管任务，从检查点继续')
            step('planner')
            step('researcher')
            if not self.db.sources(run_id):
                raise ValueError('没有检索到可用证据，请导入资料或调整检索词')
            iteration = state.get('_revision', 0)
            step('analyst', iteration)
            wants_experiment = state['planner']['experiment'] and bool(run['config'].get('dataset'))
            if wants_experiment and 'experimenter' not in state and 'experiment_skipped' not in state:
                approval = self.db.one('SELECT * FROM approvals WHERE run_id=?', (run_id,))
                if not approval:
                    ds = run['config']['dataset']
                    request = {'dataset': ds['name'], 'dataset_sha256': hashlib.sha256(ds['content'].encode()).hexdigest(),
                               'methods': run['config']['methods'], 'k': run['config']['k'],
                               'scope': '本地 CPU 固定候选检索实验；不执行生成代码，不调用付费实验服务。'}
                    with self.db.connect() as conn:
                        conn.execute('BEGIN IMMEDIATE')
                        if conn.execute('SELECT cancel_requested FROM runs WHERE id=?', (run_id,)).fetchone()[0]:
                            raise Cancelled('用户取消任务')
                        conn.execute('INSERT INTO approvals(id,run_id,status,request,created) VALUES(?,?,?,?,?)',
                                     (uid('approval'), run_id, 'pending', dumps(request), time.time()))
                        conn.execute("UPDATE runs SET status='awaiting_approval',stage='experimenter',updated=? WHERE id=?",
                                     (time.time(), run_id))
                    self.db.event(run_id, 'approval_required', 'experimenter', '实验方案已就绪，等待人工审批', request)
                    return
                if approval['status'] == 'pending':
                    self.db.execute("UPDATE runs SET status='awaiting_approval' WHERE id=?", (run_id,))
                    return
                if approval['status'] == 'rejected':
                    state['experiment_skipped'] = '用户拒绝实验：' + approval['decision']
                    save()
                elif approval['status'] == 'approved':
                    step('experimenter')
                    if not state.get('benchmark'):
                        raise ValueError('实验 agent 未产出实测指标')
                else:
                    raise ValueError('审批状态不允许继续')
            elif not wants_experiment:
                state['experiment_skipped'] = '未选择数据集或研究计划不包含检索基准实验'
                save()
            while True:
                review = step('reviewer', iteration)
                issues = verify_claims(state['analyst']['claims'], self.db.sources(run_id))
                if review['verdict'] == 'pass' and not issues:
                    break
                if iteration >= 2:
                    raise ValueError('评审在两次返工后仍未通过：' + '; '.join(issues + review['issues']))
                state['review_feedback'] = issues + review['issues']
                state.setdefault('review_history', []).append(review)
                iteration += 1
                state['_revision'] = iteration
                state.pop('analyst', None)
                state.pop('reviewer', None)
                save()
                self.db.event(run_id, 'revision', 'reviewer', '评审退回分析阶段', {'issues': state['review_feedback'], 'iteration': iteration})
                step('analyst', iteration)
            step('writer')
            check_cancel()
            build_report(self.db, run, state)
            changed = self.db.execute("UPDATE runs SET status='completed',stage='writer',error='',updated=? WHERE id=? AND cancel_requested=0", (time.time(), run_id))
            if not changed:
                raise Cancelled('用户取消任务')
            self.db.event(run_id, 'completed', '', '报告、证据与实验产物已交付')
        except Cancelled as exc:
            self.db.execute("UPDATE runs SET status='cancelled',error=?,updated=? WHERE id=?", (str(exc), time.time(), run_id))
            self.db.execute("UPDATE steps SET status='cancelled',finished=? WHERE run_id=? AND status='running'", (time.time(), run_id))
            self.db.event(run_id, 'cancelled', '', str(exc))
        except Exception as exc:
            self.db.execute("UPDATE runs SET status='failed',error=?,updated=? WHERE id=?", (str(exc)[:1000], time.time(), run_id))
            self.db.execute("UPDATE steps SET status='failed',finished=? WHERE run_id=? AND status='running'", (time.time(), run_id))
            self.db.event(run_id, 'failed', '', str(exc)[:1000])
