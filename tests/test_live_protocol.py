"""A real local HTTP provider exercises the live tool loop; no paid API is claimed."""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from backend.auth import create_user
from backend.db import DB, dumps
from backend.engine import Engine, enqueue
from backend.service import Service


class FixtureProvider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.server.requests.append(request)
        messages = request['messages']
        role = messages[0]['content'].split('the ')[1].split(' agent')[0]
        context = json.loads(messages[1]['content'])['context']
        ids = [s['id'] for s in context['sources'][:2]]
        observations = [json.loads(m['content']) for m in messages if m['role'] == 'tool']
        calls = []

        def tool(name, args):
            calls.append({'id': 'call_'+str(len(calls)), 'type':'function',
                          'function': {'name':name,'arguments':json.dumps(args)}})

        if self.server.malformed_once:
            self.server.malformed_once = False
            message = {'role':'assistant','content':'This is not valid JSON.'}
        else:
            if role == 'planner':
                output = {'objective':'Compare retrieval baselines','queries':['all:retrieval'],
                          'criteria':['Relevant method evidence'],'experiment':context['dataset_available'],
                          'rationale':'Benchmark the supplied fixed candidate dataset.'}
            elif role == 'researcher':
                if not observations:
                    tool('search_sources', {'query':'retrieval','remote':False,'limit':2})
                output = {'selected_source_ids':ids,'summary':'Two imported sources selected.'}
            elif role == 'analyst':
                if not observations:
                    for source_id in ids:
                        tool('read_source', {'source_id':source_id})
                output = {'claims':[{'text':'The imported note describes the method.', 'source_id':s['id'],
                                     'quote':s['content'][:100]} for s in observations if 'id' in s],
                          'comparisons':[{'method':'Imported retrieval baseline','strength':'Has explicit implementation notes.',
                                          'weakness':'External generalization has not been evaluated.','source_ids':ids}],
                          'gaps':['Need production evaluation data.']}
            elif role == 'experimenter':
                if not observations:
                    tool('run_benchmark', {})
                output = {'summary':'Fixed candidate retrieval metrics were calculated with the approved tool.'}
            elif role == 'reviewer':
                if not observations:
                    tool('check_evidence', {})
                revise = not context.get('review_feedback')
                output = {'verdict':'revise' if revise else 'pass',
                          'issues':['Clarify evidence scope.'] if revise else [],
                          'summary':'Review exact quotes and generalization limits.'}
            else:
                output = {'title':'Protocol integration report','summary':'Imported evidence and local measurements.',
                          'sections':[{'heading':'Evidence','text':'The source notes describe retrieval baselines.','source_ids':ids}],
                          'recommendation':'Evaluate on a representative dataset.',
                          'limitations':['Small fixed candidate benchmark only.']}
            message = {'role':'assistant','content':None,'tool_calls':calls} if calls else {'role':'assistant','content':dumps(output)}
        raw = dumps({'choices':[{'message':message}],'usage':{'prompt_tokens':20,'completion_tokens':10}}).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class LiveProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = ThreadingHTTPServer(('127.0.0.1',0), FixtureProvider)
        self.server.requests = []
        self.server.malformed_once = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.db = DB(Path(self.tmp.name)/'test.db')
        self.user = create_user(self.db,'owner@example.test','Owner','long-test-password')
        self.service = Service(self.db)
        self.project = self.service.demo(self.user)
        self.config = {'base_url':f'http://127.0.0.1:{self.server.server_port}/v1',
                       'model':'local-http-fixture','max_calls':64,'max_output_tokens':1000,'allow_arxiv':False}
        self.db.execute('INSERT INTO settings VALUES(?,?)',('model',dumps(self.config)))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def test_live_http_tools_json_repair_review_and_usage(self):
        rid = enqueue(self.db,self.project,mode='live',dataset_id=self.project['dataset_id'])
        engine = Engine(self.db)
        engine.process(rid)
        self.assertEqual(self.db.one('SELECT status FROM runs WHERE id=?',(rid,))['status'],'awaiting_approval')
        self.service.handle('POST',f'/api/runs/{rid}/approval',{'decision':'approved'},self.user,{})
        engine.process(rid)
        run = self.service.handle('GET',f'/api/runs/{rid}',{},self.user,{})
        self.assertEqual(run['status'],'completed',run['error'])
        self.assertEqual(run['calls'],len(self.server.requests))
        self.assertEqual(run['input_tokens'],20*run['calls'])
        self.assertEqual(run['output_tokens'],10*run['calls'])
        self.assertGreater(run['calls'],10)
        self.assertEqual(run['state']['_revision'],1)
        self.assertTrue(self.db.one("SELECT id FROM events WHERE run_id=? AND kind='validation'",(rid,)))
        for req in self.server.requests:
            self.assertEqual(req['model'],'local-http-fixture')
            self.assertIn('tools',req)

    def test_hard_call_budget_prevents_another_request(self):
        self.config['max_calls']=8
        self.db.execute('UPDATE settings SET value=? WHERE key=?',(dumps(self.config),'model'))
        rid=enqueue(self.db,self.project,mode='live')
        Engine(self.db).process(rid)
        run=self.db.one('SELECT * FROM runs WHERE id=?',(rid,))
        self.assertEqual(run['status'],'failed')
        self.assertIn('调用上限',run['error'])
        self.assertEqual(run['calls'],8)
        self.assertEqual(len(self.server.requests),8)


if __name__ == '__main__':
    unittest.main()
