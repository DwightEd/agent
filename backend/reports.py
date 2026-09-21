import csv
import io
import json

from .db import dumps


def build_report(db, run, state):
    writer = state['writer']
    sources = db.sources(run['id'])
    index = {s['id']: i + 1 for i, s in enumerate(sources)}
    cite = lambda ids: ' '.join(f'[{index[sid]}]' for sid in ids)
    lines = ['# ' + writer['title'], '',
             '**模式：' + ('演示策略 / 示例资料' if run['mode'] == 'demo' else '真实模型 / 外部或导入资料') + '**',
             '', '**任务：** ' + run['goal'], '', writer['summary'], '', '## 研究计划', '',
             state['planner']['rationale'], '', '纳入标准：' + '；'.join(state['planner']['criteria']), '']
    for section in writer['sections']:
        lines.extend(['## ' + section['heading'], '', section['text'] + ' ' + cite(section['source_ids']), ''])
    lines.extend(['## 证据摘录', ''])
    for claim in state['analyst']['claims']:
        lines.extend(['- ' + claim['text'] + ' ' + cite([claim['source_id']]),
                      '\n  原文：' + claim['quote'].replace('\n', ' '), ''])
    lines.extend(['## 方法比较', '', '| 方法 | 能力或优点 | 边界 | 来源 |', '|---|---|---|---|'])
    clean = lambda s: s.replace('|', '\\|').replace('\n', ' ')
    for c in state['analyst']['comparisons']:
        lines.append('| ' + ' | '.join([clean(c['method']), clean(c['strength']), clean(c['weakness']), cite(c['source_ids'])]) + ' |')
    lines.extend(['', '## 实测结果', ''])
    benchmark = state.get('benchmark')
    if benchmark:
        k = benchmark['k']
        lines.extend([f"查询数：{benchmark['queries']}；行数：{benchmark['rows']}；数据 SHA-256：`{benchmark['dataset_sha256']}`。", '',
                      f'| 方法 | Recall@{k} | MRR（全候选） | nDCG@{k} | nDCG 95% CI |', '|---|---:|---:|---:|---|'])
        for r in benchmark['results']:
            lines.append(f"| {r['method']} | {r['recall']:.4f} | {r['mrr']:.4f} | {r['ndcg']:.4f} | {r['ndcg_ci95'][0]:.4f}–{r['ndcg_ci95'][1]:.4f} |")
        lines.extend(['', benchmark['limitations'], '', state.get('experimenter', {}).get('summary', '')])
        out = io.StringIO()
        writer_csv = csv.writer(out)
        writer_csv.writerow(['method', 'recall_at_k', 'mrr', 'ndcg_at_k', 'ci_low', 'ci_high', 'k'])
        for r in benchmark['results']:
            writer_csv.writerow([r['method'], r['recall'], r['mrr'], r['ndcg'], *r['ndcg_ci95'], k])
        db.artifact(run['id'], 'metrics.csv', 'text/csv', out.getvalue())
    else:
        lines.append('未执行实验：' + state.get('experiment_skipped', '研究计划不包含基准实验'))
    lines.extend(['', '## 建议', '', state['writer']['recommendation'], '', '## 局限与未决问题', ''])
    lines.extend('- ' + s for s in state['writer']['limitations'])
    lines.extend(['', '## 评审记录', '', state['reviewer']['summary'], '', '## 资料来源', ''])
    for source in sources:
        lines.append(f"[{index[source['id']]}] {source['title']} — {source['kind']}" + (f" — {source['url']}" if source['url'] else ' — 项目内导入资料'))
    lines.extend(['', '注：引用校验验证原文存在，不自动证明论点的语义正确性。arXiv 检索默认只读取摘要。'])
    db.artifact(run['id'], 'report.md', 'text/markdown', '\n'.join(lines))
    db.artifact(run['id'], 'evidence.json', 'application/json', dumps({'sources': sources, 'claims': state['analyst']['claims']}))
    public_config = {k: v for k, v in run['config'].items() if k != 'dataset'}
    db.artifact(run['id'], 'manifest.json', 'application/json', dumps({
        'version': 1, 'run_id': run['id'], 'goal': run['goal'], 'mode': run['mode'], 'config': public_config,
        'dataset_sha256': benchmark['dataset_sha256'] if benchmark else None, 'outputs': state,
        'trace': [{**e, 'data': json.loads(e['data'])} for e in db.all('SELECT * FROM events WHERE run_id=? ORDER BY id', (run['id'],))],
    }))
    if run['config'].get('dataset'):
        db.artifact(run['id'], 'dataset.csv', 'text/csv', run['config']['dataset']['content'])
