"""
Data models for TURN credential management.

Provides type-safe dataclasses for TURN credentials, retry configuration,
and other common data structures used throughout the credential management system.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class TurnCredentials:
    """
    TURN server credentials with expiry information.

    Attributes:
        urls: List of TURN/STUN server URLs (e.g., "turn:server.com:3478?transport=udp")
        username: Username for TURN authentication
        credential: Password/credential for TURN authentication
        expires_at: Optional timestamp when credentials expire
        provider: Name of the provider that generated these credentials
    """
    urls: List[str]
    username: str
    credential: str
    expires_at: Optional[datetime] = None
    provider: str = "unknown"

    def to_dict(self) -> dict:
        """Convert credentials to dictionary for serialization."""
        return {
            "urls": self.urls,
            "username": self.username,
            "credential": self.credential,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "provider": self.provider,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TurnCredentials":
        """Create credentials from dictionary."""
        expires_at = None
        if data.get("expires_at"):
            expires_at = datetime.fromisoformat(data["expires_at"])

        return cls(
            urls=data["urls"],
            username=data["username"],
            credential=data["credential"],
            expires_at=expires_at,
            provider=data.get("provider", "unknown"),
        )

    def is_expired(self) -> bool:
        """Check if credentials have expired."""
        if not self.expires_at:
            return False
        return datetime.utcnow() >= self.expires_at

    def time_until_expiry(self) -> Optional[float]:
        """
        Get time until expiry in seconds.

        Returns:
            Seconds until expiry, or None if no expiry is set
        """
        if not self.expires_at:
            return None
        delta = self.expires_at - datetime.utcnow()
        return max(0, delta.total_seconds())


@dataclass
class IceServerConfig:
    """
    ICE server configuration for WebRTC.

    This matches the format expected by RTCPeerConnection.

    Attributes:
        urls: List of server URLs (STUN/TURN)
        username: Optional username for authenticated servers
        credential: Optional credential for authenticated servers
    """
    urls: List[str]
    username: Optional[str] = None
    credential: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        result = {"urls": self.urls}
        if self.username:
            result["username"] = self.username
        if self.credential:
            result["credential"] = self.credential
        return result


@dataclass
class RetryConfig:
    """
    Configuration for retry logic with exponential backoff.

    Attributes:
        max_retries: Maximum number of retry attempts
        backoff_seconds: Initial backoff duration in seconds
        max_backoff: Maximum backoff duration in seconds (cap)
        jitter: Add random jitter to backoff (0.0-1.0, fraction of backoff)
    """
    max_retries: int = 3
    backoff_seconds: int = 5
    max_backoff: int = 60
    jitter: float = 0.1

    def calculate_backoff(self, attempt: int) -> float:
        """
        Calculate backoff duration for given attempt.

        Args:
            attempt: Retry attempt number (0-indexed)

        Returns:
            Backoff duration in seconds with exponential backoff and jitter
        """
        import random

        # Exponential backoff: backoff_seconds * (2 ** attempt)
        backoff = min(self.backoff_seconds * (2 ** attempt), self.max_backoff)

        # Add jitter to prevent thundering herd
        if self.jitter > 0:
            jitter_amount = backoff * self.jitter * random.random()
            backoff += jitter_amount

        return backoff
