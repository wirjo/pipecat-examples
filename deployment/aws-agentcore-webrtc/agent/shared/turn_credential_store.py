"""
TURN credential storage using AWS Secrets Manager.

Provides secure storage and retrieval of TURN credentials with:
- Automatic secret creation if it doesn't exist
- Fallback to cached credentials if AWS is unavailable
- JSON serialization/deserialization
- Error handling and logging
"""

import json
import logging
from datetime import datetime
from typing import Optional

import boto3
from botocore.exceptions import ClientError, BotoCoreError

from .turn_models import TurnCredentials

logger = logging.getLogger(__name__)


class TurnCredentialStore:
    """
    AWS Secrets Manager storage for TURN credentials.

    This class provides a centralized, secure storage mechanism for TURN
    credentials that can be accessed by both the intermediary server
    (for writing) and AgentCore runtime (for reading).
    """

    def __init__(self, secret_name: str, region: str = "us-east-1"):
        """
        Initialize credential store.

        Args:
            secret_name: Name of the AWS Secrets Manager secret
            region: AWS region where the secret is stored
        """
        self.secret_name = secret_name
        self.region = region
        self._client: Optional[boto3.client] = None
        self._cached_credentials: Optional[TurnCredentials] = None

    def _get_client(self):
        """
        Get or create boto3 Secrets Manager client.

        Lazy initialization to avoid creating client during import.

        Returns:
            boto3 Secrets Manager client
        """
        if not self._client:
            self._client = boto3.client("secretsmanager", region_name=self.region)
        return self._client

    async def get_credentials(self) -> TurnCredentials:
        """
        Retrieve credentials from Secrets Manager.

        Fetches the latest version of the secret and deserializes it
        into a TurnCredentials object. Falls back to cached credentials
        if AWS is unavailable.

        Returns:
            TurnCredentials object

        Raises:
            RuntimeError: If credentials cannot be retrieved and no cache exists
        """
        try:
            client = self._get_client()

            logger.debug(f"Retrieving credentials from Secrets Manager: {self.secret_name}")

            response = client.get_secret_value(SecretId=self.secret_name)

            # Parse JSON secret
            secret_string = response["SecretString"]
            secret_data = json.loads(secret_string)

            # Deserialize to TurnCredentials
            credentials = TurnCredentials.from_dict(secret_data)

            # Update cache
            self._cached_credentials = credentials

            logger.info(
                f"Retrieved credentials from Secrets Manager: "
                f"provider={credentials.provider}, "
                f"urls={len(credentials.urls)}"
            )

            return credentials

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")

            if error_code == "ResourceNotFoundException":
                logger.error(f"Secret not found: {self.secret_name}")
                raise RuntimeError(f"Secret {self.secret_name} does not exist in Secrets Manager")

            elif error_code == "AccessDeniedException":
                logger.error(f"Access denied to secret: {self.secret_name}")
                logger.error("Ensure IAM role has secretsmanager:GetSecretValue permission")
                raise RuntimeError(f"Access denied to secret {self.secret_name}")

            else:
                logger.error(f"Failed to retrieve credentials from Secrets Manager: {e}")

                # Fall back to cached credentials if available
                if self._cached_credentials:
                    logger.warning("Using cached credentials due to AWS error")
                    return self._cached_credentials

                raise RuntimeError(f"Failed to retrieve credentials and no cache available: {e}")

        except (BotoCoreError, Exception) as e:
            logger.error(f"Unexpected error retrieving credentials: {e}", exc_info=True)

            # Fall back to cached credentials if available
            if self._cached_credentials:
                logger.warning("Using cached credentials due to error")
                return self._cached_credentials

            raise RuntimeError(f"Failed to retrieve credentials and no cache available: {e}")

    async def update_credentials(self, credentials: TurnCredentials):
        """
        Update credentials in Secrets Manager.

        Creates the secret if it doesn't exist, otherwise updates the existing secret.
        Includes metadata about when the credentials were updated.

        Args:
            credentials: TurnCredentials to store

        Raises:
            Exception: If update fails (non-critical, logged but not raised)
        """
        try:
            client = self._get_client()

            # Add metadata
            secret_data = credentials.to_dict()
            secret_data["updated_at"] = datetime.utcnow().isoformat()

            secret_string = json.dumps(secret_data, indent=2)

            logger.debug(f"Updating credentials in Secrets Manager: {self.secret_name}")

            try:
                # Try to update existing secret
                client.put_secret_value(
                    SecretId=self.secret_name,
                    SecretString=secret_string
                )

                logger.info(f"Updated credentials in Secrets Manager: {self.secret_name}")

            except ClientError as e:
                error_code = e.response.get("Error", {}).get("Code", "Unknown")

                if error_code == "ResourceNotFoundException":
                    # Secret doesn't exist, create it
                    logger.info(f"Creating new secret: {self.secret_name}")

                    client.create_secret(
                        Name=self.secret_name,
                        Description="TURN server credentials for WebRTC connections",
                        SecretString=secret_string
                    )

                    logger.info(f"Created new secret in Secrets Manager: {self.secret_name}")

                else:
                    raise  # Re-raise other errors

            # Update cache
            self._cached_credentials = credentials

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")

            if error_code == "AccessDeniedException":
                logger.error(f"Access denied when updating secret: {self.secret_name}")
                logger.error("Ensure IAM role has secretsmanager:CreateSecret and secretsmanager:PutSecretValue permissions")
            else:
                logger.error(f"Failed to update credentials in Secrets Manager: {e}")

            # Don't raise exception - credential updates are non-critical
            # The old credentials will continue to work until they expire

        except (BotoCoreError, Exception) as e:
            logger.error(f"Unexpected error updating credentials: {e}", exc_info=True)
            # Don't raise exception - non-critical

    async def delete_credentials(self):
        """
        Delete credentials from Secrets Manager.

        Schedules the secret for deletion (AWS enforces a waiting period).
        Use with caution - this is typically only needed for cleanup.

        Raises:
            Exception: If deletion fails
        """
        try:
            client = self._get_client()

            logger.warning(f"Deleting secret from Secrets Manager: {self.secret_name}")

            client.delete_secret(
                SecretId=self.secret_name,
                ForceDeleteWithoutRecovery=False  # Use recovery window for safety
            )

            logger.info(f"Scheduled secret for deletion: {self.secret_name}")

            # Clear cache
            self._cached_credentials = None

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")

            if error_code == "ResourceNotFoundException":
                logger.warning(f"Secret already deleted or doesn't exist: {self.secret_name}")
                return

            logger.error(f"Failed to delete secret: {e}")
            raise

        except (BotoCoreError, Exception) as e:
            logger.error(f"Unexpected error deleting secret: {e}", exc_info=True)
            raise

    def get_cached_credentials(self) -> Optional[TurnCredentials]:
        """
        Get cached credentials without AWS call.

        Returns:
            Cached credentials, or None if no cache exists
        """
        return self._cached_credentials
