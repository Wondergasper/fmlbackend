"""Centralized configuration via pydantic-settings (env vars / .env).

All variables can be set with or without the `FM_` prefix.
Existing unprefixed names (SUPABASE_URL, etc.) work for backward compatibility.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Supabase
    supabase_url: str = ""
    supabase_key: str = ""
    supabase_service_role_key: str = ""

    # Redis
    redis_url: str = "redis://red-d52sksogjchc738njoug:6379"
    rate_limit_window_seconds: int = 60
    rate_limit_max_requests: int = 100

    # Celery
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/0"

    # Paystack
    paystack_secret_key: str = ""
    paystack_public_key: str = ""

    # Email (Resend primary, SendGrid + SMTP fallback)
    resend_api_key: str = ""
    sendgrid_api_key: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""

    # Google
    google_maps_api_key: str = ""
    google_client_id: str = ""

    # App
    environment: str = "development"
    app_name: str = "Farmers Market API"
    cors_origins: str = "https://farmermarket-brown.vercel.app,https://farmermarket-git-cold-gasper-wonders-projects.vercel.app,http://localhost:5173"

    model_config = {"env_file": ".env", "extra": "ignore"}


settings = Settings()
