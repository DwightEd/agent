import io
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from backend.agents import Agent
from backend.auth import create_user
from backend.benchmark import evaluate, parse_csv
from backend.db import DB
from backend.engine import Engine, enqueue
from backend.research_tools import verify_claims
from backend.service import APIError, ROOT, Service


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = DB(Path(self.tmp.name) / 'app.db')
        self.user = create_user(self.db, 'owner@example.test', 'Owner', 'long-test-password')
        self.svc = Service(self.db)
        self.project = self.svc.demo(self.user)
        self.engine = Engine(self.db)

    def tearDown(self):
        self.engine.stop()
        self.tmp.cleanup()

    def get(self, path):
        return self.svc.handle('GET', path, {}, self.user, {})

    def post(self, path, data=None):
        return self.svc.handle('POST', path, data or {}, self.user, {})

    def start_run(self, dataset=True):
        rid = enqueue(self.db, self.project, dataset_id=self.project['dataset_id'] if dataset else None)
        self.process(rid)
        return rid

    def process(self, rid):
        self.db.execute("UPDATE runs SET status='running' WHERE id=?", (rid,))
        self.engine.process(rid)

    def test_full_approval_revision_and_reproducible_delivery(self):
        rid = self.start_run()
        run = self.get('/api/runs/' + rid)
        self.assertEqual(run['status'], 'awaiting_approval')
        self.assertNotIn('benchmark', run['state'])
        self.assertEqual(run['approval']['request']['methods'], ['overlap', 'tfidf', 'bm25'])
        self.post(f'/api/runs/{rid}/approval', {'decision': 'approved'})
        self.process(rid)
        run = self.get('/api/runs/' + rid)
        self.assertEqual(run['status'], 'completed', run['error'])
        self.assertEqual(run['state']['_revision'], 1)
        self.assertEqual({s['role'] for s in run['steps']}, {'planner','researcher','analyst','experimenter','reviewer','writer'})
        self.assertEqual(run['state']['benchmark'], evaluate((ROOT / 'examples/retrieval.csv').read_text()))
        self.assertEqual(run['calls'], 0)  # Demo never claims a model ran.
        kind, raw, name = self.get(f'/api/runs/{rid}/export')
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            self.assertTrue({'report.md','manifest.json','evidence.json','metrics.csv','benchmark.json','dataset.csv'} <= set(z.namelist()))
            self.assertIn('演示', z.read('report.md').decode())
            self.assertEqual(evaluate(z.read('dataset.csv').decode()), json.loads(z.read('benchmark.json')))
        tool_events = self.db.all("SELECT * FROM events WHERE run_id=? AND kind='tool_call'", (rid,))
        self.assertIn('run_benchmark', [e['message'] for e in tool_events])

    def test_rejection_skips_experiment_and_completes(self):
        rid = self.start_run()
        self.post(f'/api/runs/{rid}/approval', {'decision': 'rejected', 'note': '只需要调研'})
        self.process(rid)
        run = self.get('/api/runs/' + rid)
        self.assertEqual(run['status'], 'completed', run['error'])
        self.assertNotIn('benchmark', run['state'])
        self.assertIn('只需要调研', run['state']['experiment_skipped'])
        with self.assertRaises(ValueError):
            self.post(f'/api/runs/{rid}/approval', {'decision': 'approved'})

    def test_failure_then_retry_preserves_completed_stages(self):
        original = Agent.run_role
        def fail_researcher(agent, role, iteration=0):
            if role == 'researcher':
                raise RuntimeError('temporary upstream failure')
            return original(agent, role, iteration)
        with patch.object(Agent, 'run_role', fail_researcher):
            rid = self.start_run(dataset=False)
        run = self.get('/api/runs/' + rid)
        self.assertEqual(run['status'], 'failed')
        planner = next(s for s in run['steps'] if s['role'] == 'planner')
        self.post(f'/api/runs/{rid}/retry')
        self.process(rid)
        run = self.get('/api/runs/' + rid)
        self.assertEqual(run['status'], 'completed', run['error'])
        self.assertEqual(planner['started'], next(s for s in run['steps'] if s['role'] == 'planner')['started'])

    def test_restart_recovers_running_and_preserves_approval(self):
        rid = self.start_run()
        self.engine.start()
        self.assertEqual(self.db.one('SELECT status FROM runs WHERE id=?', (rid,))['status'], 'awaiting_approval')
        self.engine.stop()
        self.post(f'/api/runs/{rid}/approval', {'decision': 'approved'})
        self.db.execute("UPDATE runs SET status='running' WHERE id=?", (rid,))
        self.engine = Engine(self.db)
        self.engine.start()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status = self.db.one('SELECT status FROM runs WHERE id=?', (rid,))['status']
            if status in ('completed', 'failed'):
                break
            time.sleep(.05)
        self.assertEqual(status, 'completed')

    def test_snapshot_and_owner_isolation(self):
        rid = self.start_run()
        db_sources = self.db.sources(rid)
        self.db.execute('DELETE FROM sources WHERE project_id=?', (self.project['id'],))
        self.assertEqual(db_sources, self.db.sources(rid))
        other = create_user(self.db, 'other@example.test', 'Other', 'long-test-password')
        for path in (f'/api/runs/{rid}', f'/api/projects/{self.project["id"]}', f'/api/runs/{rid}/events'):
            with self.assertRaises(APIError) as ctx:
                self.svc.handle('GET', path, {}, other, {})
            self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(APIError):
            self.svc.handle('PUT', '/api/settings', {}, other, {})
        self.post(f'/api/runs/{rid}/approval', {'decision': 'approved'})
        self.process(rid)
        self.assertEqual(self.get('/api/runs/'+rid)['status'], 'completed')

    def test_cancel_no_overlapping_run_and_budget_not_reset(self):
        rid = self.start_run()
        with self.assertRaises(ValueError):
            enqueue(self.db, self.project)
        self.post(f'/api/runs/{rid}/cancel')
        self.assertEqual(self.get('/api/runs/'+rid)['status'], 'cancelled')
        self.db.execute('UPDATE runs SET calls=12 WHERE id=?', (rid,))
        self.post(f'/api/runs/{rid}/retry')
        self.assertEqual(self.get('/api/runs/'+rid)['calls'], 12)
        self.process(rid)
        self.assertEqual(self.get('/api/runs/'+rid)['status'], 'awaiting_approval')

    def test_schedule_does_not_overlap(self):
        self.svc.handle('PUT',f'/api/projects/{self.project["id"]}/schedule',{'interval_hours':1,'mode':'demo'},self.user,{})
        self.db.execute('UPDATE schedules SET next_at=0')
        self.engine.schedule_due()
        self.db.execute('UPDATE schedules SET next_at=0')
        self.engine.schedule_due()
        self.assertEqual(self.db.one('SELECT COUNT(*) n FROM runs')['n'],1)

    def test_invalid_quotes_and_csv_rejected(self):
        self.assertTrue(verify_claims([{'text':'made up','source_id':'missing','quote':'This is an invented quote'}], []))
        for content in ('bad,columns\nx,y', 'query,document,relevant\na,b,2', 'query,document,relevant\na,b,1'):
            with self.assertRaises(ValueError):
                parse_csv(content)


class MetricTest(unittest.TestCase):
    def test_known_metrics_and_row_order_invariance(self):
        text = 'query,document,relevant\nalpha,alpha,1\nalpha,unrelated,0\nbeta,beta,1\nbeta,unrelated,0\n'
        for r in evaluate(text, k=1)['results']:
            self.assertEqual((r['recall'],r['mrr'],r['ndcg']), (1,1,1))
        reversed_rows = '\n'.join([text.splitlines()[0]] + list(reversed(text.splitlines()[1:])))
        a, b = evaluate(text), evaluate(reversed_rows)
        for x, y in zip(a['results'], b['results']):
            self.assertEqual(x['ndcg'],y['ndcg'])
            self.assertEqual(x['mrr'],y['mrr'])


if __name__ == '__main__':
    unittest.main()
