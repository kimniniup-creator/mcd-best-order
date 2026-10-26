"""Real order-page connection state, derived from get_cart(wait) heartbeats."""
import time

# get_cart(wait=25) is renewed by the host right after it returns; a gap longer
# than this means no conversation is waiting (turn ended, chat closed, host quit).
GRACE_SECONDS = 12


def heartbeat(cart, now=None):
    now = time.time() if now is None else now
    if now - (cart.get('wait_heartbeat') or 0) >= 1:  # one write a second is plenty for a 12 s grace
        cart['wait_heartbeat'] = now


def connection(cart, now=None):
    now = time.time() if now is None else now
    last = cart.get('wait_heartbeat')
    waiting = bool(last) and now - last <= GRACE_SECONDS and not cart.get('submitted')
    return {
        'agent_waiting': waiting,
        'connection': 'submitted' if cart.get('submitted') else 'waiting' if waiting else 'idle',
        'agent_last_seen': last,
        'server_time': now,
    }
