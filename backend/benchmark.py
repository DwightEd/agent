"""Small reproducible retrieval experiments, without generated-code execution.

CSV schema: query,document,relevant,query_id (query_id is optional).
All document candidates for a query form one retrieval group. No training labels
are used: relevance is used only for evaluation.
"""
import csv
import hashlib
import io
import math
import random
import re
from collections import Counter


def tokens(text):
    return re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]', text.lower())


def parse_csv(content):
    reader = csv.DictReader(io.StringIO(content.lstrip('\ufeff')))
    if not {'query', 'document', 'relevant'}.issubset(reader.fieldnames or []):
        raise ValueError('CSV 必须包含 query,document,relevant 列；可选 query_id')
    rows = []
    group_queries = {}
    seen = set()
    for row in reader:
        query, doc = (row.get('query') or '').strip(), (row.get('document') or '').strip()
        if not query or not doc or row.get('relevant') not in ('0', '1'):
            raise ValueError('每行 query/document 必须非空，relevant 必须为 0 或 1')
        if len(query) > 2000 or len(doc) > 12000:
            raise ValueError('query 最大 2000 字符，document 最大 12000 字符')
        group = (row.get('query_id') or query).strip()
        if group in group_queries and group_queries[group] != query:
            raise ValueError('同一 query_id 必须使用同一 query')
        if (group, doc) in seen:
            raise ValueError('同一查询中的 document 不得重复')
        seen.add((group, doc))
        group_queries[group] = query
        rows.append({'query': query, 'document': doc, 'relevant': int(row['relevant']), 'group': group})
        if len(rows) > 3000:
            raise ValueError('单次实验最多 3000 行')
    groups = {}
    for row in rows:
        groups.setdefault(row['group'], []).append(row)
    if len(groups) < 2 or any(len(g) < 2 or not any(r['relevant'] for r in g) for g in groups.values()):
        raise ValueError('至少 2 个查询；每个查询至少 2 篇候选文档和 1 篇相关文档')
    return rows, groups


def evaluate(content, methods=None, k=3):
    methods = methods or ['overlap', 'tfidf', 'bm25']
    if not methods or any(x not in ('overlap', 'tfidf', 'bm25') for x in methods):
        raise ValueError('可用方法：overlap / tfidf / bm25')
    if type(k) is not int or not 1 <= k <= 20:
        raise ValueError('k 必须在 1–20 之间')
    rows, groups = parse_csv(content)
    documents = list(dict.fromkeys(r['document'] for r in rows))
    counts = {d: Counter(tokens(d)) for d in documents}
    df = Counter(t for d in documents for t in counts[d])
    n = len(documents)
    avg_length = sum(sum(c.values()) for c in counts.values()) / n or 1
    idf = {t: math.log((1 + n) / (1 + f)) + 1 for t, f in df.items()}

    def score(method, query, document):
        q, d = Counter(tokens(query)), counts[document]
        if method == 'overlap':
            return len(q.keys() & d.keys()) / max(1, len(q))
        if method == 'bm25':
            length = sum(d.values())
            return sum(math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) *
                       d[t] * 2.5 / (d[t] + 1.5 * (0.25 + 0.75 * length / avg_length))
                       for t in q if d[t])
        qv = {t: f * idf.get(t, math.log(n + 1) + 1) for t, f in q.items()}
        dv = {t: f * idf[t] for t, f in d.items()}
        norm = math.sqrt(sum(v*v for v in qv.values()) * sum(v*v for v in dv.values()))
        return sum(v * dv.get(t, 0) for t, v in qv.items()) / (norm or 1)

    results = []
    for method in dict.fromkeys(methods):
        per_query = []
        for group, candidates in groups.items():
            # Stable content-hash tie breaking, independent of relevance and input order.
            ranked = sorted(candidates, key=lambda r: (-score(method, r['query'], r['document']),
                            hashlib.sha256(r['document'].encode()).hexdigest()))
            labels = [r['relevant'] for r in ranked]
            recall = sum(labels[:k]) / sum(labels)
            mrr = next((1 / (i + 1) for i, label in enumerate(labels) if label), 0)
            dcg = sum(label / math.log2(i + 2) for i, label in enumerate(labels[:k]))
            ideal = sum(1 / math.log2(i + 2) for i in range(min(sum(labels), k)))
            per_query.append({'query_id': group, 'query': candidates[0]['query'], 'recall': recall,
                              'mrr': mrr, 'ndcg': dcg / ideal, 'ranking': [
                                  {'document': r['document'], 'relevant': r['relevant'],
                                   'score': round(score(method, r['query'], r['document']), 6)} for r in ranked]})
        rng = random.Random(42)
        values = [p['ndcg'] for p in per_query]
        boots = sorted(sum(rng.choices(values, k=len(values))) / len(values) for _ in range(400))
        results.append({'method': method, 'recall': sum(p['recall'] for p in per_query) / len(per_query),
                        'mrr': sum(p['mrr'] for p in per_query) / len(per_query),
                        'ndcg': sum(values) / len(values), 'ndcg_ci95': [boots[9], boots[389]],
                        'per_query': per_query})
    return {'dataset_sha256': hashlib.sha256(content.encode()).hexdigest(), 'rows': len(rows),
            'queries': len(groups), 'documents': n, 'k': k, 'seed': 42,
            'protocol': 'fixed-candidate retrieval; no parameter fitting; query bootstrap 400 samples',
            'limitations': '小样本固定候选集实验；置信区间仅反映查询重采样，不代表真实业务泛化。',
            'results': results}
