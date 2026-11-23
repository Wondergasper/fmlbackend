"""Persistent savings operations, provider reconciliation and ownership checks."""
import hashlib
import json
import logging
from datetime import date, datetime, timedelta, timezone
from fastapi import HTTPException
from database import supabase_admin
from config import settings
from services.cowrywise import cowrywise, ProviderRejected, ProviderUnavailable, balance_to_kobo, kobo_to_naira, resource_id

logger = logging.getLogger(__name__)
GOAL_FIELDS = ('id', 'name', 'target_amount_kobo', 'balance_kobo', 'maturity_date', 'status')


def first(result):
    return result.data[0] if result.data else None


def public_goal(row):
    return {field: row[field] for field in GOAL_FIELDS}


class SavingsService:
    def __init__(self, db=None, provider=None):
        self.db = db if db is not None else supabase_admin
        self.provider = provider if provider is not None else cowrywise
        self.mode = settings.cowrywise_mode

    def table(self, name):
        return self.db.table(name)

    def account(self, uid):
        return first(self.table('savings_accounts').select('*').eq('user_id', uid).eq('mode', self.mode).execute())

    def goal(self, uid, goal_id):
        goal = first(self.table('savings_goals').select('*').eq('id', goal_id).eq('user_id', uid).eq('mode', self.mode).execute())
        if not goal:
            raise HTTPException(404, 'Savings goal not found.')
        return goal

    async def refresh_account(self, uid):
        account = self.account(uid)
        if not account:
            return None
        data = await self.provider.get('accounts/' + resource_id(account['account_id']))
        if data.get('account_id') != account['account_id']:
            raise ProviderUnavailable('Invalid provider account')
        updates = {'verified': data.get('is_verified') is True,
                   'terms_accepted': data.get('terms_of_use_accepted') is True}
        self.table('savings_accounts').update(updates).eq('user_id', uid).eq('mode', self.mode).execute()
        return {**account, **updates}

    async def status(self, uid):
        result = {'enabled': self.provider.enabled, 'account_ready': False, 'mode': self.mode}
        if not self.provider.enabled:
            return {**result, 'message': 'Savings is not configured. Contact support.'}
        try:
            account = await self.refresh_account(uid)
        except (ProviderUnavailable, ProviderRejected):
            return {**result, 'message': 'Unable to confirm investment account verification. Try again later.'}
        if not account:
            return {**result, 'message': 'Create an investment account and accept Cowrywise terms, then complete verification.'}
        if not account['terms_accepted']:
            return {**result, 'message': 'Accept Cowrywise terms before investing.'}
        if not account['verified']:
            return {**result, 'message': 'Investment identity verification is required or still processing.'}
        if not account.get('wallet_id'):
            return {**result, 'message': 'Create your investment NGN wallet before saving.'}
        # Validate stored wallet ownership and currency against the provider.
        wallet = await self.provider.get('wallets/' + resource_id(account['wallet_id']))
        if wallet.get('account_id') != account['account_id'] or wallet.get('currency', wallet.get('currency_code')) != 'NGN':
            raise ProviderUnavailable('Investment wallet ownership could not be verified')
        return {**result, 'account_ready': True}

    async def ready(self, uid):
        state = await self.status(uid)
        if not state['enabled']:
            raise HTTPException(503, state['message'])
        if not state['account_ready']:
            raise HTTPException(409, state['message'])
        return self.account(uid)

    def claim(self, uid, operation, key, payload, provider_payload):
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        provider_key = hashlib.sha256(f'{self.mode}:{uid}:{operation}:{key}'.encode()).hexdigest()
        data = self.db.rpc('claim_savings_operation', {
            'p_user_id': uid, 'p_mode': self.mode, 'p_operation': operation,
            'p_request_key': key, 'p_payload_hash': digest, 'p_payload': payload,
            'p_provider_payload': provider_payload, 'p_provider_key': provider_key,
        }).execute().data
        if data.get('conflict'):
            raise HTTPException(409, 'Idempotency-Key was already used with a different payload.')
        return data

    def save(self, op, updates):
        updates['updated_at'] = datetime.now(timezone.utc).isoformat()
        result = (self.table('savings_operations').update(updates).eq('id', op['id'])
                  .eq('lease_token', op['lease_token']).execute())
        if not result.data:
            raise HTTPException(409, 'Operation is being reconciled. Retry with the same key.')
        op.update(updates)

    async def operate(self, uid, operation, key, payload, provider_payload):
        if not self.provider.enabled:
            raise HTTPException(503, 'Savings is not configured.')
        claimed = self.claim(uid, operation, key, payload, provider_payload)
        op = claimed['operation']
        if op['state'] == 'complete':
            return op['response']
        if op['state'] == 'failed':
            raise HTTPException(op['error_status'] or 422, 'The savings provider rejected this operation.')
        if not claimed['claimed']:
            if op.get('response'):
                return op['response']
            raise HTTPException(409, 'Operation is processing. Retry with the same Idempotency-Key.')
        try:
            return await self.run(op)
        except ProviderRejected as exc:
            # Rejections are final, while timeout/server errors remain recoverable.
            self.save(op, {'state': 'failed', 'error_status': 422, 'lease_until': None, 'lease_token': None})
            raise HTTPException(422, 'The savings provider rejected this operation. Check verification or available funds.') from None
        except ProviderUnavailable:
            self.save(op, {'state': 'unknown', 'lease_until': None, 'lease_token': None})
            raise HTTPException(503, 'Savings outcome is not confirmed. Retry with the same Idempotency-Key.') from None
        except HTTPException:
            raise
        except Exception:
            # Database failures after provider acceptance must never trigger a new provider key.
            logger.exception('Savings operation needs reconciliation: %s', op['id'])
            raise HTTPException(503, 'Savings operation needs reconciliation. Retry with the same key.') from None

    async def run(self, op):
        uid, kind = op['user_id'], op['operation']
        payload = op['provider_payload']
        paths = {'create': 'savings', 'account': 'accounts', 'wallet': 'wallets'}
        if kind.startswith('fund:'):
            path = 'wallets/' + resource_id(payload['wallet_id']) + '/transfer'
            body = {k: v for k, v in payload.items() if k != 'wallet_id'}
        elif kind == 'identity':
            # Identity values are deliberately never stored. Unknown outcomes are
            # reconciled by polling verification; the caller can retry the same key.
            raise HTTPException(409, 'Check verification status or resubmit the same identity request.')
        else:
            path, body = paths[kind], payload
        result = op.get('provider_result')
        if not result:
            response = await self.provider.request('POST', path, body, op['provider_key'])
            result = response.get('data', response)
            if not isinstance(result, dict):
                raise ProviderUnavailable('Unexpected savings provider response')
            # Persist provider receipt before local finalization; safe to replay after a crash.
            self.save(op, {'provider_result': result})
        if kind == 'create':
            account = self.account(uid)
            if result.get('account_id') != account['account_id'] or not result.get('savings_id') or not result.get('product_code'):
                raise ProviderUnavailable('Savings ownership could not be verified')
            goal = {
                'id': op['id'], 'user_id': uid, 'mode': self.mode, 'name': op['payload']['name'],
                'target_amount_kobo': op['payload']['target_amount_kobo'],
                'balance_kobo': balance_to_kobo(result['balance']),
                'maturity_date': date.fromisoformat(result['maturity_date']).isoformat(),
                'status': 'active', 'savings_id': result['savings_id'], 'product_code': result['product_code'],
            }
            self.table('savings_goals').upsert(goal, on_conflict='id').execute()
            response = public_goal(goal)
        elif kind.startswith('fund:'):
            transfer_id = result.get('transfer_id') or result.get('id')
            reference = result.get('reference') or transfer_id
            if not reference:
                raise ProviderUnavailable('Missing transfer reference')
            if op['state'] == 'pending':
                try:
                    result = await self.provider.get('transfers/' + resource_id(transfer_id or reference))
                except ProviderRejected:
                    # A lookup rejection does not prove the original money movement failed.
                    raise ProviderUnavailable('Transfer confirmation is unavailable') from None
                self.save(op, {'provider_result': result})
            provider_status = str(result.get('status', '')).lower()
            if provider_status in {'failed', 'cancelled', 'reversed'}:
                raise ProviderRejected(422)
            settled = provider_status in {'success', 'successful', 'completed', 'processed'}
            response = {'status': 'success' if settled else 'pending', 'reference': str(reference)}
            if not settled:
                self.save(op, {'state': 'pending', 'response': response, 'lease_until': None, 'lease_token': None})
                return response
        elif kind == 'account':
            if not result.get('account_id'):
                raise ProviderUnavailable('Missing investment account ID')
            # Singleton account creation uses a server-derived key, even across browser keys.
            account = {'user_id': uid, 'mode': self.mode, 'account_id': result['account_id'],
                       'verified': result.get('is_verified') is True,
                       'terms_accepted': result.get('terms_of_use_accepted') is True}
            self.table('savings_accounts').upsert(account, on_conflict='user_id,mode').execute()
            response = {'status': 'created', 'message': 'Complete identity verification before saving.'}
        elif kind == 'wallet':
            account = self.account(uid)
            if result.get('account_id') != account['account_id'] or result.get('currency', result.get('currency_code')) != 'NGN' or not result.get('wallet_id'):
                raise ProviderUnavailable('Invalid investment wallet')
            self.table('savings_accounts').update({'wallet_id': result['wallet_id']}).eq('user_id', uid).eq('mode', self.mode).execute()
            response = {'status': 'created'}
        self.save(op, {'state': 'complete', 'response': response, 'lease_until': None, 'lease_token': None})
        return response

    def replay(self, existing, payload):
        if not existing:
            return None
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if digest != existing['payload_hash']:
            raise HTTPException(409, 'Idempotency-Key was already used with a different payload.')
        if existing['state'] == 'complete':
            return existing['response']
        if existing['state'] == 'failed':
            raise HTTPException(existing['error_status'] or 422, 'The savings provider rejected this operation.')
        return None

    async def create_goal(self, uid, key, payload):
        # Claim checks payload equality before recomputing time-sensitive provider days.
        days = (date.fromisoformat(payload['target_date']) - datetime.now(timezone.utc).date()).days
        existing = first(self.table('savings_operations').select('*').eq('user_id', uid).eq('mode', self.mode)
                         .eq('operation', 'create').eq('request_key', key).execute())
        replay = self.replay(existing, payload)
        if replay is not None:
            return replay
        account = await self.ready(uid)
        if not existing and days < 90:
            raise HTTPException(422, 'Target date must be at least 90 days away.')
        body = {'account_id': account['account_id'], 'currency_code': 'NGN',
                'days': days, 'interest_enabled': payload['interest_enabled']}
        return await self.operate(uid, 'create', key, payload, body)

    async def fund(self, uid, goal_id, key, payload):
        goal = self.goal(uid, goal_id)
        # A settled operation can always be replayed after maturity.
        existing = first(self.table('savings_operations').select('*').eq('user_id', uid).eq('mode', self.mode)
                         .eq('operation', 'fund:' + goal_id).eq('request_key', key).execute())
        replay = self.replay(existing, payload)
        if replay is not None:
            return replay
        account = await self.ready(uid)
        if not existing and (goal['status'] != 'active' or date.fromisoformat(goal['maturity_date']) <= datetime.now(timezone.utc).date()):
            raise HTTPException(409, 'This goal is no longer accepting funds.')
        return await self.operate(uid, 'fund:' + goal_id, key, payload, {
            'wallet_id': account['wallet_id'], 'product_code': goal['product_code'],
            'amount': kobo_to_naira(payload['amount_kobo']),
        })

    async def reconcile(self, uid):
        rows = (self.table('savings_operations').select('*').eq('user_id', uid).eq('mode', self.mode)
                .in_('state', ['processing', 'unknown', 'pending']).order('created_at').limit(100).execute().data or [])
        for op in rows:
            if op['operation'] == 'identity':
                account = await self.refresh_account(uid)
                if account and account['verified']:
                    claimed = self.claim(uid, 'identity', op['request_key'], op['payload'], {})
                    if claimed['claimed']:
                        self.save(claimed['operation'], {'state': 'complete',
                            'response': {'status': 'verified'}, 'lease_until': None, 'lease_token': None})
                continue
            try:
                await self.operate(uid, op['operation'], op['request_key'], op['payload'], op['provider_payload'])
            except HTTPException as exc:
                if exc.status_code not in {409, 422}:
                    raise

    async def goals(self, uid):
        if not self.provider.enabled:
            raise HTTPException(503, 'Savings is not configured.')
        await self.reconcile(uid)
        rows = self.table('savings_goals').select('*').eq('user_id', uid).eq('mode', self.mode).execute().data or []
        if not rows:
            return []
        account = self.account(uid)
        # Provider listing is integration-wide. Only locally owned IDs are accepted.
        remote = await self.provider.list_all('savings')
        owned = {item['savings_id']: item for item in remote
                 if item.get('account_id') == account['account_id'] and item.get('savings_id') in {row['savings_id'] for row in rows}}
        updates = []
        for row in rows:
            item = owned.get(row['savings_id'])
            if not item or item.get('product_code') != row['product_code']:
                raise ProviderUnavailable('Savings balance could not be synchronized')
            maturity = date.fromisoformat(item['maturity_date'])
            status = 'completed' if str(item.get('status', '')).lower() == 'completed' else (
                'matured' if maturity <= datetime.now(timezone.utc).date() else 'active')
            updates.append((row, {'balance_kobo': balance_to_kobo(item['balance']),
                                  'maturity_date': maturity.isoformat(), 'status': status}))
        for row, update in updates:
            self.table('savings_goals').update(update).eq('id', row['id']).eq('user_id', uid).execute()
            row.update(update)
        return [public_goal(row) for row in rows]

    async def create_account(self, uid, key, payload, profile):
        if profile.get('account_type') == 'business':
            raise HTTPException(409, 'Business investment accounts require Cowrywise business verification. Contact support; buyer business registration does not verify an investment account.')
        body = {**payload, 'email': profile['email']}
        # Only one investment account per buyer/environment, independent of tab keys.
        return await self.operate(uid, 'account', 'account-onboarding', payload, body)

    async def create_wallet(self, uid, key):
        account = await self.refresh_account(uid)
        if not account or not account['verified'] or not account['terms_accepted']:
            raise HTTPException(409, 'Complete investment account verification and accept terms first.')
        return await self.operate(uid, 'wallet', 'ngn-wallet', {}, {
            'account_id': account['account_id'], 'currency_code': 'NGN',
        })

    async def verify_identity(self, uid, key, payload):
        import hmac
        if not self.provider.enabled:
            raise HTTPException(503, 'Savings is not configured.')
        account = self.account(uid)
        if not account:
            raise HTTPException(409, 'Create an investment account first.')
        # Do not persist BVNs or provider identity responses. HMAC avoids a searchable BVN hash.
        digest = hmac.new(settings.cowrywise_client_secret.encode(), payload['bvn'].encode(), hashlib.sha256).hexdigest()
        claimed = self.claim(uid, 'identity', key, {'identity_digest': digest}, {})
        op = claimed['operation']
        if op['state'] == 'complete':
            return op['response']
        if op['state'] == 'failed':
            raise HTTPException(422, 'Identity submission was rejected.')
        if not claimed['claimed']:
            raise HTTPException(409, 'Identity submission is processing; retry with the same key.')
        try:
            await self.provider.request('POST', 'accounts/' + resource_id(account['account_id']) + '/identity',
                                        {'identity_type': 'BVN', 'identity_value': payload['bvn']}, op['provider_key'])
            response = {'status': 'pending', 'message': 'Identity submitted. Check /savings/status for verified readiness.'}
            self.save(op, {'state': 'complete', 'response': response, 'lease_until': None, 'lease_token': None})
            return response
        except ProviderRejected:
            self.save(op, {'state': 'failed', 'error_status': 422, 'lease_until': None, 'lease_token': None})
            raise HTTPException(422, 'Identity submission was rejected.') from None
        except ProviderUnavailable:
            self.save(op, {'state': 'unknown', 'lease_until': None, 'lease_token': None})
            raise HTTPException(503, 'Identity outcome is unknown. Check status or retry with the same key.') from None

def reconcile_savings():
    """APScheduler job: recover persisted writes and refresh confirmed balances."""
    if not cowrywise.enabled:
        return
    import asyncio
    from services.cowrywise import Cowrywise

    async def work():
        svc = SavingsService(provider=Cowrywise())
        rows = svc.table('savings_accounts').select('user_id').eq('mode', svc.mode).execute().data or []
        pending = (svc.table('savings_operations').select('user_id').eq('mode', svc.mode)
                   .in_('state', ['processing', 'unknown', 'pending']).execute().data or [])
        for uid in {row['user_id'] for row in rows + pending}:
            try:
                await svc.goals(uid)
            except Exception:
                logger.warning('Savings reconciliation deferred for user %s', uid)
    asyncio.run(work())
