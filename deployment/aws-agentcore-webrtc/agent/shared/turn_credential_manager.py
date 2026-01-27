"""
TURN credential manager with automatic refresh.

Manages the lifecycle of TURN credentials including:
- Background refresh loop with configurable timing
- Retry logic with exponential backoff
- Thread-safe credential access
- Observer pattern for credential change notifications
"""

import asyncio
import logging
from typing import Callable, List, Optional

from .turn_models import RetryConfig, TurnCredentials
from .turn_providers import TurnCredentialProvider

logger = logging.getLogger(__name__)


class TurnCredentialManager:
    """
    Manages TURN credentials with automatic refresh.

    This class handles the lifecycle of TURN credentials:
    1. Initial credential generation on startup
    2. Background refresh loop that runs periodically
    3. Proactive refresh before credentials expire
    4. Retry logic with exponential backoff on failures
    5. Notifications to observers when credentials change

    The manager is thread-safe and uses asyncio for concurrent operations.
    """

    def __init__(
        self,
        provider: TurnCredentialProvider,
        refresh_buffer_seconds: int = 300,
        retry_config: Optional[RetryConfig] = None
    ):
        """
        Initialize credential manager.

        Args:
            provider: TURN credential provider to use for generation
            refresh_buffer_seconds: How many seconds before expiry to refresh (default: 5 minutes)
            retry_config: Optional retry configuration for failed refreshes
        """
        self.provider = provider
        self.refresh_buffer_seconds = refresh_buffer_seconds
        self.retry_config = retry_config or RetryConfig()

        # Current credentials (protected by lock)
        self._credentials: Optional[TurnCredentials] = None
        self._credentials_lock = asyncio.Lock()

        # Background task management
        self._refresh_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()

        # Observer pattern for credential changes
        self._observers: List[Callable[[Optional[TurnCredentials], TurnCredentials], None]] = []

        # Tracking for retries
        self._consecutive_failures = 0

    async def start(self):
        """
        Start the credential manager.

        Generates initial credentials and starts the background refresh loop.

        Raises:
            Exception: If initial credential generation fails
        """
        logger.info("Starting TURN credential manager")

        # Generate initial credentials
        await self._refresh_credentials()

        # Start background refresh task
        self._refresh_task = asyncio.create_task(self._refresh_loop())

        logger.info("TURN credential manager started")

    async def shutdown(self):
        """
        Shutdown the credential manager.

        Stops the background refresh loop and cleans up resources.
        """
        logger.info("Shutting down TURN credential manager")

        # Signal shutdown
        self._shutdown_event.set()

        # Wait for refresh task to complete
        if self._refresh_task:
            try:
                await asyncio.wait_for(self._refresh_task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Refresh task did not complete within timeout, cancelling")
                self._refresh_task.cancel()
                try:
                    await self._refresh_task
                except asyncio.CancelledError:
                    pass

        logger.info("TURN credential manager shutdown complete")

    async def get_credentials(self) -> TurnCredentials:
        """
        Get current credentials.

        Returns:
            Current TurnCredentials

        Raises:
            RuntimeError: If credentials are not available
        """
        async with self._credentials_lock:
            if not self._credentials:
                raise RuntimeError("Credentials not available")
            return self._credentials

    def add_observer(self, callback: Callable[[Optional[TurnCredentials], TurnCredentials], None]):
        """
        Add observer for credential changes.

        The callback will be called with (old_credentials, new_credentials) when
        credentials are refreshed.

        Args:
            callback: Async function to call on credential changes
        """
        self._observers.append(callback)

    def remove_observer(self, callback: Callable[[Optional[TurnCredentials], TurnCredentials], None]):
        """
        Remove observer for credential changes.

        Args:
            callback: Callback to remove
        """
        if callback in self._observers:
            self._observers.remove(callback)

    async def _notify_observers(self, old_creds: Optional[TurnCredentials], new_creds: TurnCredentials):
        """
        Notify all observers of credential change.

        Args:
            old_creds: Previous credentials (None if first generation)
            new_creds: New credentials
        """
        for observer in self._observers:
            try:
                result = observer(old_creds, new_creds)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Observer notification failed: {e}", exc_info=True)

    async def _refresh_loop(self):
        """
        Background refresh loop.

        Continuously monitors credential expiry and refreshes proactively
        based on the configured refresh buffer.
        """
        logger.info("Starting credential refresh loop")

        while not self._shutdown_event.is_set():
            try:
                # Calculate next refresh time
                sleep_duration = self._calculate_next_refresh()

                if sleep_duration <= 0:
                    # Credentials expired or about to expire, refresh immediately
                    logger.warning("Credentials expired or about to expire, refreshing immediately")
                    await self._refresh_credentials()
                    continue

                logger.debug(f"Next credential refresh in {sleep_duration:.0f} seconds")

                # Wait until refresh time or shutdown
                try:
                    await asyncio.wait_for(
                        self._shutdown_event.wait(),
                        timeout=sleep_duration
                    )
                    # Shutdown event was set, exit loop
                    break
                except asyncio.TimeoutError:
                    # Time to refresh
                    await self._refresh_credentials()

            except Exception as e:
                logger.error(f"Error in refresh loop: {e}", exc_info=True)
                # Wait a bit before retrying to avoid tight loop
                await asyncio.sleep(60)

        logger.info("Credential refresh loop stopped")

    def _calculate_next_refresh(self) -> float:
        """
        Calculate time until next refresh.

        Returns:
            Seconds until next refresh (can be negative if overdue)
        """
        if not self._credentials:
            return 0  # No credentials, refresh immediately

        time_until_expiry = self._credentials.time_until_expiry()
        if time_until_expiry is None:
            # Credentials never expire (static provider)
            # Refresh every 24 hours as a safety measure
            return 86400

        # Refresh before expiry by the configured buffer
        return time_until_expiry - self.refresh_buffer_seconds

    async def _refresh_credentials(self):
        """
        Refresh credentials with retry logic.

        Attempts to generate new credentials from the provider, with exponential
        backoff on failures. Updates internal state and notifies observers on success.
        """
        for attempt in range(self.retry_config.max_retries):
            try:
                # Generate new credentials
                logger.info(f"Refreshing TURN credentials (attempt {attempt + 1}/{self.retry_config.max_retries})")
                new_credentials = await self.provider.generate_credentials()

                # Update credentials (thread-safe)
                async with self._credentials_lock:
                    old_credentials = self._credentials
                    self._credentials = new_credentials

                # Log success
                expiry_str = new_credentials.expires_at.isoformat() if new_credentials.expires_at else "never"
                logger.info(
                    f"TURN credentials refreshed successfully: "
                    f"provider={new_credentials.provider}, "
                    f"urls={len(new_credentials.urls)}, "
                    f"expires_at={expiry_str}"
                )

                # Reset failure counter
                self._consecutive_failures = 0

                # Notify observers
                await self._notify_observers(old_credentials, new_credentials)

                return  # Success, exit retry loop

            except Exception as e:
                self._consecutive_failures += 1
                logger.error(
                    f"Failed to refresh credentials (attempt {attempt + 1}/{self.retry_config.max_retries}): {e}",
                    exc_info=True
                )

                # If this was the last attempt, log critical error
                if attempt == self.retry_config.max_retries - 1:
                    logger.critical(
                        f"Failed to refresh credentials after {self.retry_config.max_retries} attempts. "
                        f"Consecutive failures: {self._consecutive_failures}"
                    )
                    # Keep using old credentials if available
                    return

                # Calculate backoff and wait before retry
                backoff = self.retry_config.calculate_backoff(attempt)
                logger.info(f"Retrying in {backoff:.1f} seconds...")
                await asyncio.sleep(backoff)

    async def force_refresh(self):
        """
        Force an immediate credential refresh.

        Useful for testing or manual credential rotation.

        Raises:
            Exception: If refresh fails after retries
        """
        logger.info("Force refreshing credentials")
        await self._refresh_credentials()
