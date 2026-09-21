"""Run a complete local workflow in a separate database, with explicit demo approval."""
import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.auth import create_user
from backend.db import DB
from backend.engine import Engine, enqueue
from backend.service import Service


def main():
    parser = argparse.ArgumentParser(description='Offline ResearchOps acceptance run, isolated from the app database')
    parser.add_argument('--output', default='data/demo-output')
    args = parser.parse_args()
    import secrets
    with tempfile.TemporaryDirectory() as folder:
        db = DB(Path(folder)/'demo.db')
        user = create_user(db,'demo@example.test','Demo',secrets.token_urlsafe(24))
        service, engine = Service(db), Engine(db)
        project = service.demo(user)
        rid = enqueue(db,project,dataset_id=project['dataset_id'])
        print('[1/3] Planning, evidence search and analysis', flush=True)
        engine.process(rid)
        run = service.handle('GET',f'/api/runs/{rid}',{},user,{})
        if run['status'] != 'awaiting_approval':
            raise RuntimeError(run['error'] or 'Expected experiment approval')
        print('[2/3] Approving the bundled demo CSV benchmark (this CLI explicitly opts in)', flush=True)
        service.handle('POST',f'/api/runs/{rid}/approval',{'decision':'approved','note':'Explicit offline acceptance run'},user,{})
        engine.process(rid)
        run = service.handle('GET',f'/api/runs/{rid}',{},user,{})
        if run['status'] != 'completed':
            raise RuntimeError(run['error'])
        output = Path(args.output)
        output.mkdir(parents=True,exist_ok=True)
        for artifact in db.all('SELECT * FROM artifacts WHERE run_id=?',(rid,)):
            (output/artifact['name']).write_text(artifact['content'],encoding='utf-8')
        print('[3/3] Review loop passed; report and reproducibility bundle written to '+str(output.resolve()))
        print(json.dumps({'status':run['status'],'revisions':run['state']['_revision'],
                          'queries':run['state']['benchmark']['queries'],
                          'metrics':{r['method']:r['ndcg'] for r in run['state']['benchmark']['results']}},indent=2))


if __name__=='__main__':
    main()
