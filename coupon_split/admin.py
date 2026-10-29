"""Local admin pages: the per-account preference library, and for maintainers the shared vocabulary.

Opened only through a short-lived link from the `open_admin` tool. The link key
is exchanged for an httponly cookie bound to one account, so the page can only
read and write that account's preferences. Same-host origin is required for
every write, as on the other local pages. The vocabulary editor exists only when
COUPON_SPLIT_DEVELOPER is set; it never writes the packaged file, it exports a
new file for review.
"""
import os
import secrets
import time

from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response

LINK_TTL = 10 * 60
SESSION_TTL = 60 * 60
links = {}
sessions = {}
EXPIRED = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           '<title>后台链接已失效</title><main style="max-width:36rem;margin:12vh auto;padding:24px;font:18px/1.8 system-ui">'
           '<h1>这个后台链接已失效</h1><p>回到你的 Agent，说“打开偏好设置”获取新链接。</p></main></html>')


def issue(account):
    """A one-time key that opens the page for this account within LINK_TTL."""
    now = time.time()
    for store in (links, sessions):
        for key in [k for k, (_, until) in store.items() if until < now]:
            store.pop(key, None)
    key = secrets.token_urlsafe(24)
    links[key] = (account, now + LINK_TTL)
    return key


def _account(request):
    value = sessions.get(request.cookies.get('coupon_admin', ''))
    return value[0] if value and value[1] > time.time() else None


def developer():
    """The shared-vocabulary editor is for project maintainers; ordinary installs never show it."""
    return os.environ.get('COUPON_SPLIT_DEVELOPER', '').strip().lower() in {'1', 'true', 'yes'}


def _same_origin(request):
    return request.headers.get('origin') == 'http://' + request.headers.get('host', '')


def routes(root, prefs):
    async def open_page(request):
        entry = links.pop(request.path_params['key'], None)
        if not entry or entry[1] < time.time():
            return HTMLResponse(EXPIRED, status_code=404, headers={'Cache-Control': 'no-store'})
        session = secrets.token_urlsafe(24)
        sessions[session] = (entry[0], time.time() + SESSION_TTL)
        response = FileResponse(root / 'web/admin.html', headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        response.set_cookie('coupon_admin', session, httponly=True, samesite='strict', max_age=SESSION_TTL)
        return response

    async def preferences(request):
        account = _account(request)
        if not account:
            return JSONResponse({'error': '后台登录已过期，请回到对话重新打开。'}, status_code=401)
        if request.method == 'GET':
            return JSONResponse(prefs.read(account), headers={'Cache-Control': 'no-store'})
        if not _same_origin(request):
            return Response(status_code=403)
        try:
            saved = prefs.write(account, await request.json())
        except ValueError as exc:
            return JSONResponse({'error': str(exc)}, status_code=400)
        return JSONResponse({'saved': True, 'preferences': saved})

    async def ontology(request):
        if not developer():
            return Response(status_code=404)
        if not _account(request):
            return JSONResponse({'error': '后台登录已过期，请回到对话重新打开。'}, status_code=401)
        from coupon_split import vocabulary
        if request.method == 'GET':
            return JSONResponse(vocabulary.current(), headers={'Cache-Control': 'no-store'})
        if not _same_origin(request):
            return Response(status_code=403)
        body = await request.json()
        concepts = body.get('concepts')
        found = vocabulary.problems(concepts)
        if request.url.path.endswith('/check') or found:
            return JSONResponse({'ok': not found, 'problems': found}, status_code=200 if not found else 400)
        try:
            exported = vocabulary.export(concepts, body.get('revision', ''))
        except ValueError as exc:
            return JSONResponse({'ok': False, 'problems': str(exc).split('\n')}, status_code=400)
        import json
        text = json.dumps(exported, ensure_ascii=False, indent=2) + '\n'
        return Response(text, media_type='application/json; charset=utf-8',
                        headers={'Content-Disposition': 'attachment; filename="mcd-ontology.json"', 'Cache-Control': 'no-store'})

    async def ontology_page(request):
        if not developer() or not _account(request):
            return Response(status_code=404)
        return FileResponse(root / 'web/admin-ontology.html', headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

    async def info(request):
        if not _account(request):
            return JSONResponse({'error': '后台登录已过期，请回到对话重新打开。'}, status_code=401)
        return JSONResponse({'developer': developer()}, headers={'Cache-Control': 'no-store'})

    from starlette.routing import Route
    return [Route('/admin/preferences', preferences, methods=['GET', 'PUT']),
            Route('/admin/info', info),
            Route('/admin/ontology/check', ontology, methods=['POST']),
            Route('/admin/ontology/export', ontology, methods=['POST']),
            Route('/admin/ontology/data', ontology),
            Route('/admin/ontology', ontology_page),
            Route('/admin/{key}', open_page)]
