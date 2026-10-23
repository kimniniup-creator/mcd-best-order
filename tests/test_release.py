"""Fabricated release regressions. No account, store, address or live network data."""
import copy
import json
import time
import tomllib

import pytest

from coupon_split.service import Service, owner
from coupon_split.presentation import markdown, public
from coupon_split.cli import configure_codex


def plan():
    request={'items':[{'productCode':'100','quantity':1}]}
    order={'request':request,'price':100,'productList':[dict(request['items'][0],productName='虚构单品',subtotal=100)],'evidence_id':'fixture'}
    return {'plan_id':'fixture','owner':owner('fixture'),'confirm_token':'fixture-confirmation','created':time.time(),'state':'quoted','elements_validated':True,'orders':[],'requested_items':copy.deepcopy(request['items']),
            'storeCode':'fixture-store','plans':[{'option_id':'0','price':100,'savings':0,'orders':[order]}],
            'evidence':[{'evidence_id':'fixture','request':copy.deepcopy(request),'response':{'data':{'price':100,'productList':copy.deepcopy(request['items'])}}}]}


class Fixture:
    allow_real=True
    def __init__(self):self.calls=[];self.cancelled=False
    async def call(self,token,name,args):
        self.calls.append(name)
        if name=='order-list':return {'data':{'list':[]}}
        if name=='create-order':return {'data':{'orderId':'fixture-order','status':'10','orderStatus':'待支付','realTotalAmount':'1.00','expirePayTime':time.time()+900,'payH5Url':'https://m.mcd.cn/mcp/scanToPay?orderId=fixture-order'}}
        if name=='query-order':return {'data':{'status':'60' if self.cancelled else '10'}}
        if name=='cancel-order':self.cancelled=True;return {'data':{'cancelResult':True}}
        if name=='query-store-coupons':return {'data':[]}
        raise AssertionError(name)


async def test_create_payment_cancel_fixture(tmp_path):
    api=Fixture();service=Service(api,str(tmp_path/'plans.db'));p=plan();service.save(p)
    placed=await service.place('fixture',p['plan_id'],p['confirm_token'])
    assert placed['state']=='awaiting_payment'
    assert '手机打开麦当劳 App' in markdown(placed) and '15分钟' in markdown(placed)
    assert public(placed)['orders'][0]['mobilePayUrl'].startswith('https://m.mcd.cn/mcp/jumpToApp/')
    again=await service.place('fixture',p['plan_id'],p['confirm_token'])
    assert again['orders']==placed['orders'] and api.calls.count('create-order')==1
    cancelled=await service.cancel('fixture',p['plan_id'])
    assert cancelled['state']=='cancelled' and cancelled['orders'][0]['status']=='60'
    assert api.calls[-1]=='query-order'


async def test_cancel_result_requires_actual_status(tmp_path):
    class Stale(Fixture):
        async def call(self,token,name,args):
            result=await super().call(token,name,args)
            if name=='query-order':return {'data':{'status':'10'}}
            return result
    api=Stale();service=Service(api,str(tmp_path/'plans.db'));p=plan();service.save(p)
    await service.place('fixture',p['plan_id'],p['confirm_token'])
    assert (await service.cancel('fixture',p['plan_id']))['state']=='rollback_pending'


def test_codex_policy_is_scoped_and_idempotent(tmp_path,monkeypatch):
    monkeypatch.setenv('CODEX_HOME',str(tmp_path))
    path=tmp_path/'config.toml';path.write_text('model = "unchanged"\n[other]\nvalue = true\n',encoding='utf-8')
    configure_codex();first=path.read_text(encoding='utf-8');configure_codex()
    assert path.read_text(encoding='utf-8')==first
    config=tomllib.loads(first);assert config['other']['value'] and config['model']=='unchanged'
    policy=config['plugins']['mcd-best-order@mcd-best-order-marketplace']['mcp_servers']['coupon-split']
    assert policy['tools']['quote_best']['approval_mode']=='approve'
    assert all(policy['tools'][tool]['approval_mode']=='prompt' for tool in ('place_orders','cancel_plan','pay_card'))


async def test_expired_wrong_account_and_tamper_are_rejected(tmp_path):
    api=Fixture();service=Service(api,str(tmp_path/'plans.db'));p=plan();service.save(p)
    with pytest.raises(ValueError):await service.place('other',p['plan_id'],p['confirm_token'])
    with pytest.raises(ValueError):await service.place('fixture',p['plan_id'],'wrong')
    p['created']-=601;service.save(p)
    with pytest.raises(ValueError):await service.place('fixture',p['plan_id'],p['confirm_token'])
    p['created']=time.time();p['plans'][0]['orders'][0]['request']['items'][0]['quantity']=2;service.save(p)
    with pytest.raises(ValueError):await service.place('fixture',p['plan_id'],p['confirm_token'])
    assert not api.calls


@pytest.mark.parametrize('confirmation',['','不要确认','先等，不下单','取消'])
async def test_dialogue_confirmation_required(tmp_path,monkeypatch,confirmation):
    from coupon_split import server as s
    api=Fixture();service=Service(api,str(tmp_path/'plans.db'));p=plan();service.save(p)
    monkeypatch.setattr(s,'service',service);monkeypatch.setattr(s,'UPSTREAM_TOKEN','fixture')
    with pytest.raises(ValueError,match='明确确认'):await s.place_orders(p['plan_id'],p['confirm_token'],confirmation)
    assert not api.calls


async def test_large_amount_additional_confirmation(tmp_path,monkeypatch):
    from coupon_split import server as s
    api=Fixture();service=Service(api,str(tmp_path/'plans.db'));p=plan();p['plans'][0]['price']=20001;service.save(p)
    monkeypatch.setattr(s,'service',service);monkeypatch.setattr(s,'UPSTREAM_TOKEN','fixture')
    for extra in ('','确认下单 ¥200.00'):
        with pytest.raises(ValueError,match='200.01'):await s.place_orders(p['plan_id'],p['confirm_token'],'确认',high_amount_confirmation=extra)
    assert not api.calls


async def test_host_approval_metadata():
    from coupon_split import server as s
    tool=next(t for t in await s.mcp.list_tools() if t.name=='place_orders')
    assert tool.meta['anthropic/requiresUserInteraction'] is True
    assert tool.annotations.destructiveHint and not tool.annotations.readOnlyHint
    assert 'user_confirmation' in tool.inputSchema['required']


async def test_new_authorization_is_recovered_without_server_restart(tmp_path,monkeypatch):
    from coupon_split import server as s
    from coupon_split.local import LocalState,CartStore
    class Account:
        async def call(self,token,name,args):
            assert token=='fixture-new' and name=='query-my-account'
            return {'data':{'accountId':'fixture-xxxx'}}
    async def url():return 'http://127.0.0.1:1'
    monkeypatch.setattr(s,'local_url',url)
    monkeypatch.setattr(s,'memory',LocalState(tmp_path/'memory'))
    monkeypatch.setattr(s,'carts',CartStore(tmp_path/'carts.db'))
    monkeypatch.setattr(s,'UPSTREAM_TOKEN','')
    monkeypatch.setattr(s,'upstream',Account())
    ctx=s.credential.set('')
    try:
        assert (await s.mcd_start('我要吃麦当劳')).structuredContent['stage']=='setup'
        monkeypatch.setattr(s,'load_token',lambda prefer_saved=False:'fixture-new')
        resumed=(await s.mcd_start('好了')).structuredContent
        assert resumed['stage']=='ask' and '可以说' in resumed['display_markdown']
        assert 'session_id' in resumed
    finally:s.credential.reset(ctx)


async def test_item_details_fill_account_without_asking_user(tmp_path,monkeypatch):
    from coupon_split import server as s
    from coupon_split.local import LocalState
    class Details:
        async def call(self,token,name,args):
            if name=='query-my-account':return {'data':{'accountId':'fixture-xxxx'}}
            if name=='query-meals':return {'data':{'meals':{'100':{'name':'虚构单品'}}}}
            if name=='query-meal-detail':return {'data':{'code':'100','name':'虚构单品','rounds':[]}}
            raise AssertionError(name)
    monkeypatch.setattr(s,'upstream',Details());monkeypatch.setattr(s,'UPSTREAM_TOKEN','fixture')
    monkeypatch.setattr(s,'memory',LocalState(tmp_path/'memory'))
    result=await s.get_item_detail('fixture-store','100',{'fulfillment':'pickup'})
    assert result.structuredContent['default_item']['productCode']=='100'
