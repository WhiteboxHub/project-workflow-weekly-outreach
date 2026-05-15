import threading
import httpx
from typing import Generator
from app.core.config import settings
from app.core.redis_client import get_redis
from app.core.logging import get_logger

logger = get_logger(__name__)

class TokenManager:
    """
    Centralized Token Manager that caches the JWT in Redis.
    If a 401 is encountered, it handles logging in and fetching a fresh
    token using the credentials defined in config.py.
    """
    REDIS_KEY = "outreach:auth_token"
    
    # In-memory lock to prevent multiple threads from hammering the login endpoint
    _lock = threading.Lock()

    @classmethod
    def get_valid_token(cls) -> str | None:
        """
        Attempt to retrieve the cached token from Redis.
        Falls back to settings.api_bearer_token if Redis is empty.
        """
        try:
            r = get_redis()
            token = r.get(cls.REDIS_KEY)
            if token:
                return token
        except Exception as e:
            logger.warning("token_manager_redis_read_failed", error=str(e))
            
        return settings.api_bearer_token

    @classmethod
    def refresh_token(cls) -> str | None:
        """
        Hit the auth endpoint to get a fresh token. 
        Thread-safe to prevent multiple simultaneous login requests.
        """
        if not settings.api_login_email or not settings.api_login_password:
            logger.error(
                "token_refresh_failed", 
                reason="api_login_email or api_login_password not set in .env"
            )
            return None

        with cls._lock:
            # Double-check if another thread already refreshed it while we waited
            try:
                r = get_redis()
                cached = r.get(cls.REDIS_KEY)
                # If cached token is different from settings (or if settings is empty),
                # it might have just been updated. But to be completely safe against
                # race conditions where it just expired again, we proceed with refresh.
            except Exception:
                pass

            try:
                logger.info("token_manager_refreshing_token", email=settings.api_login_email)
                with httpx.Client() as client:
                    resp = client.post(
                        f"{settings.api_url}{settings.api_login_path}",
                        data={
                            "username": settings.api_login_email,
                            "password": settings.api_login_password,
                        },
                        timeout=15.0
                    )
                    resp.raise_for_status()
                    
                    token = resp.json().get("access_token")
                    if token:
                        logger.info("token_manager_refresh_success")
                        try:
                            r = get_redis()
                            # Cache the token for 12 hours. The APIAuth flow will naturally 
                            # overwrite this if it expires sooner.
                            r.set(cls.REDIS_KEY, token, ex=3600 * 12)
                        except Exception as redis_err:
                            logger.warning("token_manager_redis_write_failed", error=str(redis_err))
                        return token
                    else:
                        logger.error("token_manager_refresh_failed", reason="no access_token in response")
                        return None
            except Exception as e:
                logger.error("token_manager_refresh_failed", error=str(e))
                return None


class APIAuth(httpx.Auth):
    """
    Native httpx Auth class that intercepts requests to automatically inject
    the Bearer token, and silently retries the request if a 401 is encountered.
    """
    
    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        # 1. Get the best available token and inject it
        token = TokenManager.get_valid_token()
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
            
        # 2. Yield the request and wait for the response
        response = yield request

        # 3. If unauthorized, refresh the token and replay the exact same request
        if response.status_code == 401:
            logger.warning("api_auth_401_detected", url=str(request.url))
            new_token = TokenManager.refresh_token()
            if new_token:
                logger.info("api_auth_replaying_request", url=str(request.url))
                request.headers["Authorization"] = f"Bearer {new_token}"
                yield request
