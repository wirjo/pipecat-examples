"""
TURN credential provider abstraction.

Provides a flexible, provider-agnostic interface for generating TURN credentials
from various sources (Cloudflare, Twilio, static configuration, etc.).
"""

import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Optional

from .turn_models import TurnCredentials

logger = logging.getLogger(__name__)


class TurnCredentialProvider(ABC):
    """
    Abstract base class for TURN credential providers.

    Providers implement the logic to generate or fetch TURN credentials
    from various sources (APIs, configuration files, etc.).
    """

    @abstractmethod
    async def generate_credentials(self) -> TurnCredentials:
        """
        Generate fresh TURN credentials.

        Returns:
            TurnCredentials object with urls, username, credential, and expiry

        Raises:
            Exception: If credential generation fails
        """
        pass

    @abstractmethod
    def get_ttl_seconds(self) -> int:
        """
        Get the time-to-live (TTL) for generated credentials.

        Returns:
            TTL in seconds (0 means never expires)
        """
        pass

    def get_provider_name(self) -> str:
        """
        Get the provider name for logging and identification.

        Returns:
            Provider name string
        """
        return self.__class__.__name__


class CloudflareTurnProvider(TurnCredentialProvider):
    """
    Cloudflare Calls TURN provider.

    Generates ephemeral TURN credentials via Cloudflare's TURN service API.
    Uses short-lived credentials with configurable TTL.

    API Documentation:
    https://developers.cloudflare.com/calls/turn/generate-credentials/
    """

    def __init__(self, turn_key_id: str, api_token: str, ttl: int = 86400):
        """
        Initialize Cloudflare TURN provider.

        Args:
            turn_key_id: Cloudflare TURN Key ID
            api_token: Cloudflare API token with TURN permissions
            ttl: Credential time-to-live in seconds (default: 24 hours)
        """
        self.turn_key_id = turn_key_id
        self.api_token = api_token
        self.ttl = ttl
        self.api_base = "https://rtc.live.cloudflare.com/v1"

    async def generate_credentials(self) -> TurnCredentials:
        """
        Generate fresh TURN credentials from Cloudflare API.

        Makes a POST request to Cloudflare's credential generation endpoint
        and parses the response to extract ICE servers configuration.

        Returns:
            TurnCredentials with ICE server URLs and credentials

        Raises:
            Exception: If API request fails or response is invalid
        """
        import aiohttp

        url = f"{self.api_base}/turn/keys/{self.turn_key_id}/credentials/generate-ice-servers"
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }
        body = {"ttl": self.ttl}

        logger.info(f"Requesting Cloudflare TURN credentials (TTL: {self.ttl}s)")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=body, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as response:
                    # Accept any 2xx status code (200 OK, 201 Created, etc.)
                    if response.status < 200 or response.status >= 300:
                        error_text = await response.text()
                        raise Exception(f"Cloudflare API error {response.status}: {error_text}")

                    data = await response.json()
        except aiohttp.ClientError as e:
            raise Exception(f"Failed to connect to Cloudflare API: {e}")

        # Extract credentials from iceServers array
        ice_servers = data.get("iceServers", [])
        if not ice_servers:
            raise Exception("No ice servers returned from Cloudflare API")

        # Find first server with credentials (TURN server)
        turn_server = None
        for server in ice_servers:
            if "username" in server and "credential" in server:
                turn_server = server
                break

        if not turn_server:
            raise Exception("No TURN servers with credentials found in response")

        # Extract all URLs (both STUN and TURN)
        all_urls = []
        for server in ice_servers:
            urls = server.get("urls", [])
            if isinstance(urls, str):
                urls = [urls]

            # Filter out port 53 URLs (blocked by browsers)
            # See: https://developers.cloudflare.com/calls/turn/generate-credentials/
            filtered_urls = [u for u in urls if ":53" not in u]
            all_urls.extend(filtered_urls)

        logger.info(f"Cloudflare API returned {len(all_urls)} ICE server URLs (filtered port 53)")

        return TurnCredentials(
            urls=all_urls,
            username=turn_server["username"],
            credential=turn_server["credential"],
            expires_at=datetime.utcnow() + timedelta(seconds=self.ttl),
            provider="cloudflare"
        )

    def get_ttl_seconds(self) -> int:
        """Get credential TTL in seconds."""
        return self.ttl

    def get_provider_name(self) -> str:
        return "cloudflare"


class TwilioTurnProvider(TurnCredentialProvider):
    """
    Twilio TURN provider.

    Generates ephemeral TURN credentials via Twilio's Network Traversal Service.

    API Documentation:
    https://www.twilio.com/docs/stun-turn/api
    """

    def __init__(self, account_sid: str, auth_token: str, ttl: int = 86400):
        """
        Initialize Twilio TURN provider.

        Args:
            account_sid: Twilio Account SID
            auth_token: Twilio Auth Token
            ttl: Credential time-to-live in seconds (default: 24 hours)
        """
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.ttl = ttl
        self.api_base = "https://api.twilio.com/2010-04-01"

    async def generate_credentials(self) -> TurnCredentials:
        """
        Generate fresh TURN credentials from Twilio API.

        Makes a POST request to Twilio's token generation endpoint
        using HTTP Basic authentication.

        Returns:
            TurnCredentials with ICE server URLs and credentials

        Raises:
            Exception: If API request fails or response is invalid
        """
        import aiohttp

        url = f"{self.api_base}/Accounts/{self.account_sid}/Tokens.json"
        auth = aiohttp.BasicAuth(self.account_sid, self.auth_token)
        data = {"Ttl": self.ttl}

        logger.info(f"Requesting Twilio TURN credentials (TTL: {self.ttl}s)")

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, auth=auth, data=data, timeout=aiohttp.ClientTimeout(total=30)) as response:
                    if response.status != 201:
                        error_text = await response.text()
                        raise Exception(f"Twilio API error {response.status}: {error_text}")

                    result = await response.json()
        except aiohttp.ClientError as e:
            raise Exception(f"Failed to connect to Twilio API: {e}")

        # Extract credentials from response
        username = result.get("username")
        password = result.get("password")
        ice_servers = result.get("ice_servers", [])

        if not username or not password:
            raise Exception("No credentials returned from Twilio API")

        # Extract URLs from ice_servers array
        all_urls = []
        for server in ice_servers:
            url = server.get("url")
            if url:
                all_urls.append(url)
            urls = server.get("urls", [])
            if isinstance(urls, str):
                urls = [urls]
            all_urls.extend(urls)

        if not all_urls:
            raise Exception("No ICE server URLs returned from Twilio API")

        logger.info(f"Twilio API returned {len(all_urls)} ICE server URLs")

        return TurnCredentials(
            urls=all_urls,
            username=username,
            credential=password,
            expires_at=datetime.utcnow() + timedelta(seconds=self.ttl),
            provider="twilio"
        )

    def get_ttl_seconds(self) -> int:
        """Get credential TTL in seconds."""
        return self.ttl

    def get_provider_name(self) -> str:
        return "twilio"


class StaticTurnProvider(TurnCredentialProvider):
    """
    Static TURN provider for backward compatibility.

    Returns pre-configured credentials from environment variables or
    constructor arguments. No API calls are made.

    Use this provider for:
    - Development/testing with fixed credentials
    - Backward compatibility with existing deployments
    - TURN services that don't support dynamic credential generation
    """

    def __init__(
        self,
        urls: list[str],
        username: str,
        credential: str,
        ttl: int = 0
    ):
        """
        Initialize static TURN provider.

        Args:
            urls: List of TURN/STUN server URLs
            username: Static username
            credential: Static password/credential
            ttl: Optional TTL in seconds (0 = never expires)
        """
        self.urls = urls
        self.username = username
        self.credential = credential
        self.ttl = ttl

    async def generate_credentials(self) -> TurnCredentials:
        """
        Return static credentials.

        Returns:
            TurnCredentials with pre-configured values
        """
        logger.info("Using static TURN credentials (no API call)")

        expires_at = None
        if self.ttl > 0:
            expires_at = datetime.utcnow() + timedelta(seconds=self.ttl)

        return TurnCredentials(
            urls=self.urls,
            username=self.username,
            credential=self.credential,
            expires_at=expires_at,
            provider="static"
        )

    def get_ttl_seconds(self) -> int:
        """Get credential TTL in seconds (0 = never expires)."""
        return self.ttl

    def get_provider_name(self) -> str:
        return "static"


def create_provider_from_env() -> TurnCredentialProvider:
    """
    Create TURN credential provider from environment variables.

    Reads TURN_PROVIDER environment variable to determine which provider
    to instantiate, then reads provider-specific configuration.

    Environment Variables:
        TURN_PROVIDER: Provider type (cloudflare|twilio|static)
        TURN_TTL: Credential TTL in seconds (default: 86400)

        For Cloudflare:
            CLOUDFLARE_TURN_KEY_ID: Cloudflare TURN Key ID
            CLOUDFLARE_TURN_API_TOKEN: Cloudflare API token

        For Twilio:
            TWILIO_ACCOUNT_SID: Twilio Account SID
            TWILIO_AUTH_TOKEN: Twilio Auth Token

        For Static:
            ICE_SERVER_URLS: Comma-separated list of URLs
            ICE_SERVER_USERNAME: Static username
            ICE_SERVER_CREDENTIAL: Static credential

    Returns:
        Configured TurnCredentialProvider instance

    Raises:
        ValueError: If configuration is missing or invalid
    """
    provider_type = os.getenv("TURN_PROVIDER", "cloudflare").lower()
    ttl = int(os.getenv("TURN_TTL", "86400"))

    logger.info(f"Creating TURN provider: {provider_type} (TTL: {ttl}s)")

    if provider_type == "cloudflare":
        turn_key_id = os.getenv("CLOUDFLARE_TURN_KEY_ID")
        api_token = os.getenv("CLOUDFLARE_TURN_API_TOKEN")

        if not turn_key_id or not api_token:
            raise ValueError(
                "Cloudflare provider requires CLOUDFLARE_TURN_KEY_ID and "
                "CLOUDFLARE_TURN_API_TOKEN environment variables"
            )

        return CloudflareTurnProvider(turn_key_id, api_token, ttl)

    elif provider_type == "twilio":
        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")

        if not account_sid or not auth_token:
            raise ValueError(
                "Twilio provider requires TWILIO_ACCOUNT_SID and "
                "TWILIO_AUTH_TOKEN environment variables"
            )

        return TwilioTurnProvider(account_sid, auth_token, ttl)

    elif provider_type == "static":
        raw_urls = os.getenv("ICE_SERVER_URLS")
        username = os.getenv("ICE_SERVER_USERNAME")
        credential = os.getenv("ICE_SERVER_CREDENTIAL")

        if not raw_urls or not username or not credential:
            raise ValueError(
                "Static provider requires ICE_SERVER_URLS, ICE_SERVER_USERNAME, "
                "and ICE_SERVER_CREDENTIAL environment variables"
            )

        urls = [u.strip() for u in raw_urls.split(",") if u.strip()]

        return StaticTurnProvider(urls, username, credential, ttl)

    else:
        raise ValueError(f"Unknown TURN provider: {provider_type}")
