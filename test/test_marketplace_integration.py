"""Offline regression coverage for marketplace publication and savings safety."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4
import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from routers import products, savings, auth
from middleware.auth import get_current_user, require_role
from schemas.savings import SavingsGoalCreate, SavingsFund
from services.savings import SavingsService
from services.cowrywise import Cowrywise, ProviderUnavailable, ProviderRejected, kobo_to_naira, naira_to_kobo
from config import settings


def run(coro):
    return asyncio.run(coro)


class Query:
    def __init__(self, db, name):
        self.db, self.name, self.filters = db, name, []
        self.action, self.values = 'select', None
    def select(self, *args): return self
    def order(self, *args, **kwargs): return self
    def limit(self, *args): return self
    def eq(self, key, value):
        self.filters.append((key, lambda x: x == value)); return self
    def gt(self, key, value):
        self.filters.append((key, lambda x: x is not None and x > value)); return self
    def in_(self, key, value):
        self.filters.append((key, lambda x: x in value)); return self
    def insert(self, value): self.action, self.values = 'insert', value; return self
    def upsert(self, value, **kwargs): self.action, self.values = 'upsert', value; return self
    def update(self, value): self.action, self.values = 'update', value; return self
    def delete(self): self.action = 'delete'; return self
    def execute(self):
        rows = self.db.rows.setdefault(self.name, [])
        if self.action in {'insert', 'upsert'}:
            value = deepcopy(self.values)
            if self.action == 'upsert':
                keys = ['id'] if 'id' in value else ['user_id', 'mode']
                existing = next((r for r in rows if all(r.get(k) == value.get(k) for k in keys)), None)
                if existing is not None:
                    existing.update(value); return SimpleNamespace(data=[deepcopy(existing)])
            value.setdefault('id', str(uuid4()))
            rows.append(value)
            return SimpleNamespace(data=[deepcopy(value)])
        def match(row):
            for key, fn in self.filters:
                value = row
                for part in key.split('.'): value = value.get(part) if isinstance(value, dict) else None
                if not fn(value): return False
            return True
        selected = [r for r in rows if match(r)]
        if self.action == 'update':
            if self.db.fail_update == self.name:
                raise RuntimeError('database write failed')
            for row in selected: row.update(deepcopy(self.values))
        if self.action == 'delete':
            self.db.rows[self.name] = [r for r in rows if not match(r)]
        return SimpleNamespace(data=deepcopy(selected))


class DB:
    def __init__(self, **rows): self.rows, self.fail_update = rows, None
    def table(self, name): return Query(self, name)
    def rpc(self, name, p):
        assert name == 'claim_savings_operation'
        rows = self.rows.setdefault('savings_operations', [])
        op = next((r for r in rows if all(r[k] == p['p_' + k] for k in ('user_id','mode','operation','request_key'))), None)
        if op is None:
            op = {k[2:]: deepcopy(v) for k,v in p.items()}
            op.update(id=str(uuid4()), state='processing', lease_token=None, lease_until=None,
                      provider_result=None, response=None, error_status=None)
            rows.append(op)
        if op['payload_hash'] != p['p_payload_hash']:
            return SimpleNamespace(execute=lambda: SimpleNamespace(data={'conflict': True}))
        claimed = op['state'] not in {'complete','failed'} and not op['lease_until']
        if claimed: op.update(lease_token=str(uuid4()), lease_until='leased')
        return SimpleNamespace(execute=lambda: SimpleNamespace(data={'claimed': claimed,'operation': deepcopy(op)}))


@pytest.fixture
def db(monkeypatch):
    db = DB(profiles=[{'id':'vendor','role':'vendor','status':'Active','email':'v@example.com'},
                      {'id':'buyer','role':'customer','status':'Active'},
                      {'id':'admin','role':'admin','status':'Active'}],
            products=[{'id':'p','name':'Yam','vendor_id':'vendor','status':'Approved','stock':5,'price':100,
                       'vendor':{'status':'Active'}}])
    for module in (products, auth):
        monkeypatch.setattr(module, 'supabase', db)
        monkeypatch.setattr(module, 'supabase_admin', db)
    import dependencies
    monkeypatch.setattr(dependencies, 'supabase_admin', db)
    monkeypatch.setattr(products.send_product_submitted, 'delay', MagicMock())
    monkeypatch.setattr(products.send_product_approved, 'delay', MagicMock())
    monkeypatch.setattr(products.send_product_rejected, 'delay', MagicMock())
    monkeypatch.setattr(products, 'create_notification', AsyncMock())
    return db


def client_for(uid):
    app = FastAPI()
    app.include_router(products.router)
    app.include_router(savings.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=uid)
    return TestClient(app)


@pytest.mark.parametrize('state,stock', [('Rejected',5),('Pending Approval',5),('Approved',0)])
def test_unavailable_products_hidden_on_list_detail_and_batch(db, state, stock):
    db.rows['products'][0].update(status=state,stock=stock)
    client = client_for('buyer')
    assert client.get('/products/').json() == []
    assert client.get('/products/p').status_code == 404
    assert client.post('/products/batch',json={'ids':['p']}).json() == []


def test_suspended_vendor_products_hidden(db):
    db.rows['products'][0]['vendor']['status'] = 'Suspended'
    assert client_for('buyer').get('/products/').json() == []


@pytest.mark.parametrize('uid', ['buyer','rider','stranger'])
def test_non_merchant_cannot_edit_or_delete(db, uid):
    client = client_for(uid)
    assert client.patch('/products/p',json={'name':'Stolen'}).status_code == 403
    assert client.delete('/products/p').status_code == 403
    assert db.rows['products'][0]['name'] == 'Yam'


def test_farmer_edits_return_to_pending_and_admin_edits_keep_status(db):
    client = client_for('vendor')
    assert client.patch('/products/p/image',json={'image_url':'https://example.com/a.jpg'}).status_code == 200
    assert db.rows['products'][0]['status'] == 'Pending Approval'
    db.rows['products'][0]['status'] = 'Rejected'
    assert client_for('admin').patch('/products/p',json={'price':200}).status_code == 200
    assert db.rows['products'][0]['status'] == 'Rejected'


def test_farmer_cannot_edit_other_farmers_product(db):
    db.rows['products'][0]['vendor_id'] = 'other'
    assert client_for('vendor').patch('/products/p',json={'price':200}).status_code == 403


def test_publish_zero_stock_is_unchanged(db):
    db.rows['products'][0].update(stock=0,status='Rejected')
    response = client_for('admin').patch('/products/p/status',json={'status':'Approved'})
    assert response.status_code == 409
    assert db.rows['products'][0]['status'] == 'Rejected'


def test_vendor_cannot_publish_or_inject_ownership(db):
    client = client_for('vendor')
    assert client.patch('/products/p/status',json={'status':'Approved'}).status_code == 403
    data = dict(name='Yam',category='Tubers',price=100,stock=2,origin='Jos')
    assert client.post('/products/',json={**data,'vendor_id':'other'}).status_code == 422
    response = client.post('/products/',json=data)
    assert response.json()['data']['status'] == 'Pending Approval'
    assert response.json()['data']['vendor_id'] == 'vendor'


def test_database_failure_cannot_grant_role_from_id_or_attribute(db):
    user = SimpleNamespace(id='admin-001', role='admin')
    db.rows['profiles'] = []
    with pytest.raises(HTTPException) as exc: run(require_role(['admin'])(user))
    assert exc.value.status_code == 403
    db.table = MagicMock(side_effect=RuntimeError('offline'))
    with pytest.raises(HTTPException) as exc: run(require_role(['admin'])(user))
    assert exc.value.status_code == 503


@pytest.mark.parametrize('data', [dict(account_type='business'),
    dict(account_type='business',business_name=' ',business_phone='12345678'),
    dict(role='vendor',account_type='business',business_name='Farm',business_phone='12345678'),
    dict(account_type='individual',business_name='Farm')])
def test_buyer_metadata_validation(data):
    with pytest.raises(ValidationError):
        auth.RegisterRequest(email='buyer@example.com',password='password',full_name='Buyer',**data)


def test_business_stays_customer():
    data = auth.RegisterRequest(email='b@example.com',password='password',full_name='Buyer',
        account_type='business',business_name=' Farm ',business_phone='12345678')
    assert data.role == 'customer' and data.business_name == 'Farm'


@pytest.mark.parametrize('amount', [0,-1,True,1.5,'100',9007199254740992])
def test_savings_reject_invalid_amounts(amount):
    with pytest.raises(ValidationError): SavingsFund(amount_kobo=amount)


def test_exact_decimal_conversion():
    assert kobo_to_naira(9007199254740991) == '90071992547409.91'
    assert kobo_to_naira(101) == '1.01'
    assert naira_to_kobo('1.0100') == 101
    for value in ('NaN','Infinity','0.001','-1'):
        with pytest.raises(ValueError): naira_to_kobo(value)


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setattr(settings,'cowrywise_client_secret','test-secret')
    maturity = (datetime.now(timezone.utc).date()+timedelta(days=100)).isoformat()
    db = DB(savings_accounts=[{'user_id':'buyer','mode':'sandbox','account_id':'acct','wallet_id':'wallet',
                              'verified':True,'terms_accepted':True}], savings_goals=[])
    provider = SimpleNamespace(enabled=True, request=AsyncMock(), get=AsyncMock(), list_all=AsyncMock())
    provider.request.return_value = {'data':{'savings_id':'sav','product_code':'code','account_id':'acct',
                                             'maturity_date':maturity,'balance':'0.00'}}
    service = SavingsService(db,provider)
    service.ready = AsyncMock(return_value=db.rows['savings_accounts'][0])
    return service


def goal_payload():
    return {'name':'Food','target_amount_kobo':50000,
            'target_date':(datetime.now(timezone.utc).date()+timedelta(days=100)).isoformat(), 'interest_enabled':True}


def test_idempotency_replays_and_payload_conflicts(svc):
    payload = goal_payload()
    one = run(svc.create_goal('buyer','key',payload))
    two = run(svc.create_goal('buyer','key',payload))
    assert one == two and len(svc.db.rows['savings_goals']) == 1
    assert svc.provider.request.await_count == 1
    with pytest.raises(HTTPException) as exc:
        run(svc.create_goal('buyer','key',{**payload,'name':'Changed'}))
    assert exc.value.status_code == 409


def test_timeout_retry_reuses_provider_payload_and_key(svc):
    receipt = deepcopy(svc.provider.request.return_value)
    svc.provider.request.side_effect = [ProviderUnavailable(), receipt]
    with pytest.raises(HTTPException) as exc: run(svc.create_goal('buyer','key',goal_payload()))
    assert exc.value.status_code == 503
    assert not svc.db.rows['savings_goals']
    assert svc.db.rows['savings_operations'][0]['state'] == 'unknown'
    run(svc.create_goal('buyer','key',goal_payload()))
    calls = svc.provider.request.await_args_list
    assert calls[0] == calls[1]


def test_claimed_operation_cannot_call_provider_concurrently(svc):
    payload = goal_payload()
    svc.claim('buyer','create','key',payload,{'account_id':'acct'})
    with pytest.raises(HTTPException) as exc: run(svc.create_goal('buyer','key',payload))
    assert exc.value.status_code == 409
    svc.provider.request.assert_not_awaited()


def test_reconcile_after_reload_recovers_unknown_creation(svc):
    svc.provider.request.side_effect = ProviderUnavailable()
    with pytest.raises(HTTPException): run(svc.create_goal('buyer','key',goal_payload()))
    svc.provider.request.side_effect = None
    run(svc.reconcile('buyer'))
    assert len(svc.db.rows['savings_goals']) == 1
    assert svc.db.rows['savings_operations'][0]['state'] == 'complete'


def test_funding_pending_then_confirmed_does_not_change_local_balance(svc):
    goal = run(svc.create_goal('buyer','key',goal_payload()))
    svc.provider.request.return_value = {'data':{'transfer_id':'ref','status':'Pending'}}
    result = run(svc.fund('buyer',goal['id'],'fundkey',{'amount_kobo':101}))
    assert result == {'status':'pending','reference':'ref'}
    assert svc.db.rows['savings_goals'][0]['balance_kobo'] == 0
    assert svc.provider.request.await_args.args[2]['amount'] == '1.01'
    svc.provider.get.return_value = {'transfer_id':'ref','status':'Successful'}
    assert run(svc.fund('buyer',goal['id'],'fundkey',{'amount_kobo':101}))['status'] == 'success'
    assert svc.db.rows['savings_goals'][0]['balance_kobo'] == 0
    assert svc.provider.request.await_count == 2


def test_cross_user_goal_funding_fails_before_provider(svc):
    goal = run(svc.create_goal('buyer','key',goal_payload()))
    svc.provider.request.reset_mock()
    with pytest.raises(HTTPException) as exc: run(svc.fund('intruder',goal['id'],'key',{'amount_kobo':100}))
    assert exc.value.status_code == 404
    svc.provider.request.assert_not_awaited()


def test_short_maturity_rejected(svc):
    payload = {**goal_payload(),'target_date':(datetime.now(timezone.utc).date()+timedelta(days=89)).isoformat()}
    with pytest.raises(HTTPException) as exc: run(svc.create_goal('buyer','key',payload))
    assert exc.value.status_code == 422
    svc.provider.request.assert_not_awaited()


def test_balance_sync_filters_integration_wide_listing(svc):
    goal = run(svc.create_goal('buyer','key',goal_payload()))
    own = {**svc.provider.request.return_value['data'],'balance':'123.45'}
    svc.provider.list_all.return_value = [own,{**own,'account_id':'other','savings_id':'other','balance':'900000'}]
    result = run(svc.goals('buyer'))
    assert result[0]['balance_kobo'] == 12345 and len(result) == 1
    assert 'savings_id' not in result[0]
    svc.provider.list_all.side_effect = ProviderUnavailable()
    with pytest.raises(ProviderUnavailable): run(svc.goals('buyer'))


def test_provider_failure_never_fabricates_success(svc):
    svc.provider.request.side_effect = ProviderRejected(400)
    with pytest.raises(HTTPException) as exc: run(svc.create_goal('buyer','key',goal_payload()))
    assert exc.value.status_code == 422
    assert not svc.db.rows['savings_goals']
    with pytest.raises(HTTPException): run(svc.create_goal('buyer','key',goal_payload()))
    assert svc.provider.request.await_count == 1


def test_transport_oauth_caching_pagination_exact_amount_and_headers(monkeypatch):
    monkeypatch.setattr(settings,'cowrywise_client_id','client')
    monkeypatch.setattr(settings,'cowrywise_client_secret','secret')
    requests=[]
    def handle(request):
        requests.append(request)
        if request.url.path == '/o/token/': return httpx.Response(200,json={'access_token':'token','expires_in':3600})
        assert request.headers['Authorization'] == 'Bearer token'
        if request.method == 'POST':
            assert request.headers['Embed-Idempotency-Key'] == 'stable-key'
            assert b'1.01' in request.content
            return httpx.Response(200,json={'data':{'status':'Pending'}})
        if not request.url.query:
            return httpx.Response(200,json={'data':{'results':[{'id':1}],'next':'?page=2'}})
        return httpx.Response(200,json={'data':{'results':[{'id':2}],'next':None}})
    async def scenario():
        provider = Cowrywise(httpx.MockTransport(handle))
        assert len(await provider.list_all('savings')) == 2
        await provider.request('POST','wallets/w/transfer',{'amount':kobo_to_naira(101)},'stable-key')
        with pytest.raises(ProviderUnavailable): await provider.get('https://attacker.example/api/v1/savings')
    run(scenario())
    assert sum(r.url.path == '/o/token/' for r in requests) == 1


def test_cart_validation_aggregates_quantities_and_hides_unpublished(db):
    client = client_for('buyer')
    response = client.post('/products/validate-cart', json={'items':[{'product_id':'p','quantity':3},{'product_id':'p','quantity':3}]})
    assert response.json() == {'valid':False,'unavailable_product_ids':['p'],'items':[]}
    db.rows['products'][0]['status'] = 'Rejected'
    assert client.post('/products/validate-cart',json={'items':[{'product_id':'p','quantity':1}]}).json()['items'] == []


def test_private_lists_require_database_role_and_vendor_is_scoped(db):
    from routers import admin, vendors
    app = FastAPI()
    app.include_router(admin.router)
    app.include_router(vendors.router)
    import unittest.mock
    with unittest.mock.patch.object(admin, 'supabase_admin', db), unittest.mock.patch.object(vendors, 'supabase_admin', db):
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id='buyer')
        client = TestClient(app)
        assert client.get('/admin/products').status_code == 403
        assert client.get('/vendors/me/products').status_code == 403
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id='vendor')
        db.rows['products'].append({'id':'other','vendor_id':'other','status':'Pending Approval'})
        assert [p['id'] for p in client.get('/vendors/me/products').json()] == ['p']
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id='admin')
        assert len(client.get('/admin/products').json()) == 2


def test_business_registration_is_persisted_and_returned_from_me(db, monkeypatch):
    monkeypatch.setattr(db, 'auth', SimpleNamespace(sign_up=lambda _: SimpleNamespace(user=SimpleNamespace(id='newbuyer'))), raising=False)
    for task in (auth.send_welcome_customer, auth.send_welcome_vendor, auth.send_admin_new_vendor):
        monkeypatch.setattr(task, 'delay', MagicMock())
    app = FastAPI()
    app.include_router(auth.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id='newbuyer')
    client = TestClient(app)
    response = client.post('/auth/register', json={'email':'b@example.com','password':'password', 'full_name':'Buyer',
        'role':'customer','account_type':'business','business_name':'Farm Shop','business_phone':'12345678'})
    assert response.status_code == 201
    profile = client.get('/auth/me').json()
    assert profile['account_type'] == 'business' and profile['business_name'] == 'Farm Shop'
    assert profile['role'] == 'customer'
    assert client.post('/auth/register', json={'email':'b@example.com','password':'password','full_name':'Buyer','role':'admin'}).status_code == 400


def test_savings_requires_customer_and_idempotency_header(db, svc):
    app = FastAPI()
    app.include_router(savings.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id='buyer')
    app.dependency_overrides[savings.service] = lambda: svc
    client = TestClient(app)
    assert client.post('/savings/',json=goal_payload()).status_code == 422
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id='vendor')
    assert client.get('/savings/status').status_code == 403
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id='buyer')
    svc.provider.enabled = False
    assert client.get('/savings/status').json() == {
        'enabled':False,'account_ready':False,'mode':'sandbox','message':'Savings is not configured. Contact support.'}


def test_completed_operation_replays_without_live_provider_readiness(svc):
    goal = run(svc.create_goal('buyer','key',goal_payload()))
    svc.ready.side_effect = ProviderUnavailable()
    assert run(svc.create_goal('buyer','key',goal_payload())) == goal


def test_funding_matured_goal_is_rejected(svc):
    goal = run(svc.create_goal('buyer','key',goal_payload()))
    svc.db.rows['savings_goals'][0]['maturity_date'] = datetime.now(timezone.utc).date().isoformat()
    with pytest.raises(HTTPException) as exc:
        run(svc.fund('buyer',goal['id'],'fund-key',{'amount_kobo':100}))
    assert exc.value.status_code == 409


def test_identity_never_persists_bvn_and_account_is_not_ready_until_verified(svc):
    svc.provider.request.return_value = {'data':{'identity_value':'22222222281'}}
    result = run(svc.verify_identity('buyer','identity-key',{'bvn':'22222222281'}))
    assert result['status'] == 'pending'
    assert '22222222281' not in repr(svc.db.rows)
    svc.provider.get.return_value = {'account_id':'acct','is_verified':False,'terms_of_use_accepted':True}
    assert run(svc.status('buyer'))['account_ready'] is False


def test_savings_http_errors_and_timeouts_are_not_success(monkeypatch):
    monkeypatch.setattr(settings,'cowrywise_client_id','client')
    monkeypatch.setattr(settings,'cowrywise_client_secret','secret')
    for code in (400,500):
        def handle(request):
            if request.url.path == '/o/token/': return httpx.Response(200,json={'access_token':'token','expires_in':3600})
            return httpx.Response(code,json={'error':'provider error'})
        async def scenario():
            provider = Cowrywise(httpx.MockTransport(handle))
            with pytest.raises(ProviderRejected if code == 400 else ProviderUnavailable):
                await provider.request('POST','savings',{},'key')
        run(scenario())


def test_provider_top_level_pagination_and_decimal_json(monkeypatch):
    monkeypatch.setattr(settings,'cowrywise_client_id','client')
    monkeypatch.setattr(settings,'cowrywise_client_secret','secret')
    def handle(request):
        if request.url.path == '/o/token/': return httpx.Response(200,json={'access_token':'token','expires_in':3600})
        if not request.url.query:
            return httpx.Response(200,content=b'{"data":[{"balance":90071992547409.91}],"pagination":{"next":"?page=2"}}')
        return httpx.Response(200,json={'data':[{'balance':'1.00'}],'pagination':{'next':None}})
    result = run(Cowrywise(httpx.MockTransport(handle)).list_all('savings'))
    assert len(result) == 2
    assert naira_to_kobo(result[0]['balance']) == 9007199254740991


def test_fractional_kobo_interest_is_never_rounded_up():
    from services.cowrywise import balance_to_kobo
    assert balance_to_kobo('2002.7499') == 200274


def test_documented_transfer_id_is_used_for_polling(svc):
    goal = run(svc.create_goal('buyer','key',goal_payload()))
    svc.provider.request.return_value = {'data':{'id':'transfer-uuid','reference':'TRF123','status':'Pending'}}
    assert run(svc.fund('buyer',goal['id'],'fund',{'amount_kobo':100})) == {'status':'pending','reference':'TRF123'}
    svc.provider.get.return_value = {'id':'transfer-uuid','reference':'TRF123','status':'Processed'}
    assert run(svc.fund('buyer',goal['id'],'fund',{'amount_kobo':100}))['status'] == 'success'
    svc.provider.get.assert_awaited_with('transfers/transfer-uuid')


def test_failed_product_edit_does_not_retry_partial_fields(db):
    db.fail_update = 'products'
    before = deepcopy(db.rows['products'])
    with pytest.raises(RuntimeError):
        client_for('vendor').patch('/products/p',json={'name':'Changed','unit':'bag'})
    assert db.rows['products'] == before


def test_transfer_lookup_error_does_not_mark_money_movement_failed(svc):
    goal = run(svc.create_goal('buyer','key',goal_payload()))
    svc.provider.request.return_value = {'data':{'id':'transfer-uuid','reference':'TRF123','status':'Pending'}}
    run(svc.fund('buyer',goal['id'],'fund',{'amount_kobo':100}))
    svc.provider.get.side_effect = ProviderRejected(404)
    with pytest.raises(HTTPException) as exc:
        run(svc.fund('buyer',goal['id'],'fund',{'amount_kobo':100}))
    assert exc.value.status_code == 503
    assert svc.db.rows['savings_operations'][-1]['state'] == 'unknown'
    assert svc.provider.request.await_count == 2
