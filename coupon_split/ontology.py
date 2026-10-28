"""The ordering ontology: one data file, three readers.

`data/mcd-ontology.json` holds the vocabulary (concepts, used by slang.py) and the
model (entities, slots, constraints, order-state graph) used here. Nothing in this
module writes sentences for the host to say: constraints carry a stable reason code,
a fact about the situation and what could be done instead; the host words the reply.
"""
import json
import re
from pathlib import Path

PATH = Path(__file__).parent / 'data/mcd-ontology.json'
FULL = json.loads(PATH.read_text(encoding='utf-8'))
MODEL = FULL['model']
VOCABULARY = {k: v for k, v in FULL.items() if k != 'model'}
SLOTS, CONSTRAINTS, STATES = MODEL['slots'], MODEL['constraints'], MODEL['states']
INVARIANTS = MODEL['invariants']


class _Missing(dict):
    def __missing__(self, key):
        return '{' + key + '}'


def violation(code, **facts):
    """A violated constraint: reason code, fact for this situation, feasible alternative."""
    spec = CONSTRAINTS[code]
    fill = lambda text: re.sub(r'\{(\w+)\}', lambda m: str(facts.get(m[1], m[0])), text)
    out = {'reason_code': code, 'slot': spec['slot'], 'fact': fill(spec['fact']), 'alternative': fill(spec['alternative'])}
    return out


def state_name(plan_state=None, cart=None):
    """Map a persisted plan state or a cart snapshot to a node of the state graph."""
    if plan_state:
        return plan_state if plan_state in STATES else 'collecting'
    if not cart:
        return 'start'
    if cart.get('violations') or cart.get('open_questions'):
        return 'blocked'
    if cart.get('recommendation_pending'):
        return 'proposed'
    if cart.get('pending_plan_id'):
        return 'items_ready'
    if cart.get('items') and cart.get('store_code'):
        return 'items_ready'
    if cart.get('picker_issued') and cart.get('store_code') and not cart.get('submitted'):
        return 'picking'
    if cart.get('store_code'):
        return 'store_ready'
    return 'collecting'


def allowed_actions(state, has_plan=False):
    actions = list(STATES[state]['actions'])
    if not has_plan:
        actions = [a for a in actions if a not in {'place_orders', 'withdraw_order', 'wait_payment', 'pay_card', 'get_options', 'get_evidence'}]
    return actions


def validate():
    """Structural self-check used by the unit tests."""
    problems = []
    for code, spec in CONSTRAINTS.items():
        if spec['slot'] not in SLOTS:
            problems.append(code + ': unknown slot ' + spec['slot'])
        if not code.isupper() or not spec.get('fact') or not spec.get('alternative'):
            problems.append(code + ': malformed')
    for name, spec in STATES.items():
        for action in spec['actions']:
            if not re.fullmatch(r'[a-z_]+', action):
                problems.append(name + ': bad action ' + action)
    for name, slot in SLOTS.items():
        for key in ('entity', 'sources', 'ask', 'default'):
            if key not in slot:
                problems.append(name + ': missing ' + key)
        if slot['entity'] not in MODEL['entities']:
            problems.append(name + ': unknown entity')
    return problems
