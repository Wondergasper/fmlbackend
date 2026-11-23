"""Authenticated buyer savings API."""
from typing import Annotated
from fastapi import APIRouter, Depends, Header, HTTPException
from dependencies import require_role
from schemas.savings import SavingsGoalCreate, SavingsFund, SavingsAccountCreate, SavingsIdentity
from services.savings import SavingsService
from services.cowrywise import ProviderUnavailable, ProviderRejected

router = APIRouter(prefix='/savings', tags=['savings'])
Customer = Depends(require_role(['customer']))
Key = Annotated[str, Header(alias='Idempotency-Key', min_length=1, max_length=200, pattern=r'^[\x21-\x7e]+$')]


async def service():
    try:
        yield SavingsService()
    except (ProviderUnavailable, ValueError, KeyError):
        raise HTTPException(503, 'Unable to confirm savings provider data. Try again later.') from None
    except ProviderRejected:
        raise HTTPException(503, 'Savings provider is unavailable for this account.') from None


@router.get('/status')
async def savings_status(user=Customer, svc=Depends(service)):
    return await svc.status(user.id)


@router.get('/terms')
async def savings_terms(user=Customer, svc=Depends(service)):
    terms = await svc.provider.get('accounts/retrieve-terms')
    return {'terms_link': terms['terms_link']}


@router.post('/account', status_code=201)
async def savings_account(payload: SavingsAccountCreate, idempotency_key: Key,
                          user=Customer, svc=Depends(service)):
    return await svc.create_account(user.id, idempotency_key, payload.model_dump(), user.profile)


@router.post('/account/identity', status_code=202)
async def savings_identity(payload: SavingsIdentity, idempotency_key: Key,
                           user=Customer, svc=Depends(service)):
    return await svc.verify_identity(user.id, idempotency_key, payload.model_dump())


@router.post('/account/wallet', status_code=201)
async def savings_wallet(idempotency_key: Key, user=Customer, svc=Depends(service)):
    return await svc.create_wallet(user.id, idempotency_key)


@router.get('/operations')
async def savings_operations(user=Customer, svc=Depends(service)):
    await svc.reconcile(user.id)
    rows = (svc.table('savings_operations').select('id, operation, request_key, state, response, created_at')
            .eq('user_id', user.id).eq('mode', svc.mode).order('created_at', desc=True).limit(100).execute().data or [])
    # Provider account/wallet/savings IDs, credentials and identity data stay server-side.
    return rows


@router.get('/')
async def savings_goals(user=Customer, svc=Depends(service)):
    return await svc.goals(user.id)


@router.post('/', status_code=201)
async def create_savings(payload: SavingsGoalCreate, idempotency_key: Key,
                         user=Customer, svc=Depends(service)):
    return await svc.create_goal(user.id, idempotency_key, payload.model_dump(mode='json'))


@router.post('/{goal_id}/fund')
async def fund_savings(goal_id: str, payload: SavingsFund, idempotency_key: Key,
                       user=Customer, svc=Depends(service)):
    return await svc.fund(user.id, goal_id, idempotency_key, payload.model_dump())
