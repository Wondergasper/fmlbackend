"""Server-only Cowrywise Embed transport. No marketplace wallet mutations."""
import asyncio
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from urllib.parse import urljoin, urlparse, quote
import httpx
from config import settings

SAFE_INTEGER = 9007199254740991


class ProviderUnavailable(Exception):
    pass


class ProviderRejected(Exception):
    def __init__(self, status_code):
        self.status_code = status_code


def kobo_to_naira(amount):
    if type(amount) is not int or not 0 < amount <= SAFE_INTEGER:
        raise ValueError("Amount must be a positive safe integer in kobo")
    return format(Decimal(amount) / 100, '.2f')


def naira_to_kobo(value):
    try:
        amount = Decimal(str(value)) * 100
        if not amount.is_finite() or amount < 0 or amount > SAFE_INTEGER or amount != amount.to_integral_value():
            raise ValueError("Invalid provider balance")
        return int(amount)
    except (InvalidOperation, TypeError):
        raise ValueError("Invalid provider balance") from None


def balance_to_kobo(value):
    # Provider interest can have fractions of a kobo. Expose only whole,
    # confirmed kobo and never round up an amount available to the customer.
    try:
        amount = Decimal(str(value)) * 100
        if not amount.is_finite() or amount < 0 or amount > SAFE_INTEGER:
            raise ValueError("Invalid provider balance")
        return int(amount.to_integral_value(rounding=ROUND_DOWN))
    except (InvalidOperation, TypeError):
        raise ValueError("Invalid provider balance") from None


def resource_id(value):
    return quote(str(value), safe='')


class Cowrywise:
    def __init__(self, transport=None):
        if settings.cowrywise_mode not in {'sandbox', 'production'}:
            raise ValueError('COWRYWISE_MODE must be sandbox or production')
        self.origin = f'https://{settings.cowrywise_mode}.embed.cowrywise.com'
        self.base = self.origin + '/api/v1/'
        self.transport = transport
        self.token = None
        self.expires = 0
        self.lock = asyncio.Lock()

    @property
    def enabled(self):
        return bool(settings.cowrywise_client_id and settings.cowrywise_client_secret)

    async def _token(self, client):
        async with self.lock:
            if self.token and time.monotonic() < self.expires:
                return self.token
            try:
                response = await client.post(self.origin + '/o/token/', data={
                    'grant_type': 'client_credentials',
                    'client_id': settings.cowrywise_client_id,
                    'client_secret': settings.cowrywise_client_secret,
                })
                response.raise_for_status()
                payload = response.json()
                self.token = payload['access_token']
                self.expires = time.monotonic() + max(0, int(payload['expires_in']) - 60)
                return self.token
            except (httpx.HTTPError, ValueError, KeyError):
                raise ProviderUnavailable('Savings authentication is unavailable') from None

    async def request(self, method, path, payload=None, key=None):
        if not self.enabled:
            raise ProviderUnavailable('Savings is not configured')
        url = urljoin(self.base, path)
        parsed = urlparse(url)
        if parsed.scheme != 'https' or parsed.netloc != urlparse(self.origin).netloc or not parsed.path.startswith('/api/v1/'):
            raise ProviderUnavailable('Invalid provider pagination URL')
        async with httpx.AsyncClient(timeout=20, transport=self.transport, follow_redirects=False) as client:
            headers = {'Authorization': 'Bearer ' + await self._token(client)}
            if key:
                headers['Embed-Idempotency-Key'] = key
            try:
                response = await client.request(method, url, json=payload, headers=headers)
                if response.status_code >= 500 or response.status_code in {408, 409, 429}:
                    raise ProviderUnavailable('Savings request outcome must be reconciled')
                if response.status_code >= 400:
                    raise ProviderRejected(response.status_code)
                body = response.json(parse_float=str)
                if not isinstance(body, dict):
                    raise ValueError('Unexpected provider response')
                return body
            except (httpx.HTTPError, ValueError):
                raise ProviderUnavailable('Savings provider is unavailable; retry with the same key') from None

    async def get(self, path):
        body = await self.request('GET', path)
        data = body.get('data', body)
        if not isinstance(data, dict):
            raise ProviderUnavailable('Unexpected provider response')
        return data

    async def list_all(self, path):
        result, seen = [], set()
        while path:
            if path in seen or len(seen) >= 1000:
                raise ProviderUnavailable('Invalid provider pagination')
            seen.add(path)
            body = await self.request('GET', path)
            data = body.get('data', body)
            if isinstance(data, list):
                rows = data
                next_page = body.get('pagination', {}).get('next', body.get('next'))
            elif isinstance(data, dict):
                rows = data.get('results', data.get('data'))
                next_page = data.get('pagination', {}).get('next', data.get('next', body.get('pagination', {}).get('next', body.get('next'))))
            else:
                raise ProviderUnavailable('Unexpected provider list')
            if not isinstance(rows, list):
                raise ProviderUnavailable('Unexpected provider list')
            result.extend(rows)
            path = urljoin(urljoin(self.base, path), next_page) if next_page else None
        return result


cowrywise = Cowrywise()
