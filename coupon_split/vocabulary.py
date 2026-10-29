"""Edit-time checks and export for the shared ordering vocabulary (the `concepts` of mcd-ontology.json).

The developer page edits concepts only; the structural model (entities, slots,
constraints, state graph) stays read-only. Nothing here writes the packaged
file: `export` returns the full JSON to save into the repository through a
normal review.
"""
import copy
import json
import re

from coupon_split.ontology import FULL, PATH

FIELDS = {'id', 'entry', 'type', 'prefLabel', 'altLabel', 'groundBy', 'broader', 'byLabel', 'note',
          'askAlways', 'askLabels', 'askWhenSiblings', 'availableIn', 'hasMain', 'cardVariant'}
FORBIDDEN = re.compile(r'price|Price|productCode|storeCode|"code"|¥|￥')


def _closure(concepts, cid):
    seen, queue = [], list(concepts[cid].get('broader', []))
    while queue:
        current = queue.pop(0)
        if current in concepts and current not in seen:
            seen.append(current)
            queue.extend(concepts[current].get('broader', []))
    return seen


def problems(concepts, types=None):
    """Every reason the concept list cannot ship, empty when it can."""
    types = types or FULL['types']
    out = []
    if not isinstance(concepts, list):
        return ['concepts 必须是列表']
    ids = [c.get('id') for c in concepts if isinstance(c, dict)]
    if len(ids) != len(concepts):
        return ['每个概念都必须是对象']
    by_id = {c['id']: c for c in concepts if c.get('id')}
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        out.append(f'概念 id 重复：{dup}')
    owners = {}
    entries = sorted(c['entry'] for c in concepts if 'entry' in c)
    if entries != list(range(1, len(entries) + 1)):
        out.append('entry 编号必须从 1 连续编号，现有 ' + str(len(entries)) + ' 条')
    for c in concepts:
        cid = c.get('id') or '（无 id）'
        if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', str(c.get('id', ''))):
            out.append(f'{cid}：id 只能用小写字母、数字和连字符')
        extra = set(c) - FIELDS
        if extra:
            out.append(f'{cid}：不认识的字段 ' + '、'.join(sorted(extra)))
        if c.get('type') not in types:
            out.append(f'{cid}：类型 {c.get("type")} 不存在')
        if not isinstance(c.get('prefLabel'), str) or not c['prefLabel'].strip():
            out.append(f'{cid}：缺少标准名')
        for key in ('altLabel', 'groundBy'):
            if not isinstance(c.get(key), list) or not all(isinstance(x, str) and x.strip() for x in c.get(key, [])):
                out.append(f'{cid}：{key} 必须是非空文字列表')
        if 'entry' in c and not c.get('altLabel'):
            out.append(f'{cid}：黑话条目至少要有一个说法')
        for parent in c.get('broader', []):
            if parent not in by_id:
                out.append(f'{cid}：上位概念 {parent} 不存在')
        if c.get('hasMain') and c['hasMain'] not in by_id:
            out.append(f'{cid}：主食概念 {c["hasMain"]} 不存在')
        for label in c.get('byLabel', {}):
            if label not in c.get('altLabel', []):
                out.append(f'{cid}：byLabel 的 {label} 不在说法里')
        if c.get('id') in by_id and c['id'] in _closure(by_id, c['id']):
            out.append(f'{cid}：上位关系成环')
        if FORBIDDEN.search(json.dumps(c, ensure_ascii=False)):
            out.append(f'{cid}：不能存价格、商品码或门店码')
        if 'entry' in c:
            for alias in c.get('altLabel', []):
                owners.setdefault(alias, []).append(cid)
    for alias, who in sorted(owners.items()):
        if len(who) > 1:
            out.append(f'说法“{alias}”同时属于 ' + '、'.join(who))
    return out


def current():
    """What the page shows: editable concepts, read-only model, type and relation glossaries."""
    return {'revision': FULL['revision'], 'types': FULL['types'], 'relations': FULL['relations'],
            'concepts': copy.deepcopy(FULL['concepts']), 'model': FULL['model']}


def export(concepts, revision):
    """The packaged file with only the concepts replaced. Raises with every problem when it cannot ship."""
    found = problems(concepts)
    if found:
        raise ValueError('\n'.join(found))
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(revision)):
        raise ValueError('revision 用 YYYY-MM-DD')
    out = copy.deepcopy(json.loads(PATH.read_text(encoding='utf-8')))
    out['revision'] = revision
    out['concepts'] = concepts
    return out
