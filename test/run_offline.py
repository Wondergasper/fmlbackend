"""Run the backend suite with dummy Supabase keys and external HTTP disabled."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
import httpx
import pytest
os.environ['SUPABASE_URL'] = 'https://example.supabase.co'
os.environ['SUPABASE_KEY'] = 'eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoiYW5vbiJ9.test'
os.environ['SUPABASE_SERVICE_ROLE_KEY'] = 'eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.test'
def offline(*args, **kwargs):
    raise httpx.ConnectError('External HTTP disabled in offline test run')
async def offline_async(*args, **kwargs):
    raise httpx.ConnectError('External HTTP disabled in offline test run')
httpx.HTTPTransport.handle_request = offline
httpx.AsyncHTTPTransport.handle_async_request = offline_async
raise SystemExit(pytest.main(['test', '-o', 'addopts=', '-q', '-p', 'no:cacheprovider', '--tb=short']))
