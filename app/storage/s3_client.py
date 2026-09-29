from __future__ import annotations

import logging
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings

logger = logging.getLogger(__name__)


class S3Client:
    def __init__(
        self,
        endpoint: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        bucket: str | None = None,
        presign_ttl: int | None = None,
    ) -> None:
        """Initialize S3 client.

        Backward-compatible with tests that pass explicit endpoint/access/bucket kwargs.
        Falls back to settings when parameters are omitted.
        """
        self.bucket = bucket or settings.S3_BUCKET
        self._explicit_endpoint = endpoint or settings.S3_ENDPOINT or None
        self._access_key = access_key or settings.S3_ACCESS_KEY or None
        self._secret_key = secret_key or settings.S3_SECRET_KEY or None
        self._presign_ttl = presign_ttl or settings.S3_PRESIGN_TTL
        self._client = self._initialize_client()
        self._filesystem_root: Path | None = None

    def upload_bytes(self, data: bytes, key: str, content_type: str = "application/pdf") -> str:
        if self._client is not None:
            try:
                self._client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=data,
                    ContentType=content_type,
                )
                url = self._client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self.bucket, "Key": key},
                    ExpiresIn=self._presign_ttl,
                )
                logger.debug("Uploaded %s to bucket %s", key, self.bucket)
                return url
            except (BotoCoreError, ClientError) as exc:
                logger.exception("S3 upload failed for %s: %s", key, exc)
                if settings.ENV.lower() == "prod":
                    raise RuntimeError("Failed to upload PDF to object storage") from exc
        local_url = self._write_to_filesystem(data, key)
        logger.debug("Stored %s locally at %s", key, local_url)
        return local_url

    async def upload_file(self, data: bytes, key: str, content_type: str = "image/png") -> str:
        """Async wrapper for upload_bytes.

        Offloads the blocking boto3 upload to a worker thread so it never blocks
        the event loop — a slow S3 call used to hang the whole request (and the
        uvicorn worker) with no way to recover.
        """
        import anyio

        return await anyio.to_thread.run_sync(self.upload_bytes, data, key, content_type)

    def _initialize_client(self):
        try:
            session = boto3.session.Session(
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
            )
            endpoint = self._explicit_endpoint
            region = getattr(settings, "S3_REGION", "us-east-1")

            # For AWS S3, don't set endpoint_url (let boto3 use default AWS endpoints)
            client_kwargs = {
                "service_name": "s3",
                "region_name": region,
                # Fast-fail timeouts + bounded retries so a flaky/misconfigured S3
                # returns an error in seconds instead of hanging the request.
                "config": Config(
                    signature_version="s3v4",
                    connect_timeout=5,
                    read_timeout=20,
                    retries={"max_attempts": 2, "mode": "standard"},
                ),
            }

            # Only set endpoint_url for non-AWS S3-compatible services
            if endpoint:
                client_kwargs["endpoint_url"] = endpoint

            client = session.client(**client_kwargs)
            # Ensure bucket exists; create if missing in non-prod setups
            try:
                client.head_bucket(Bucket=self.bucket)
            except ClientError as exc:
                error_code = exc.response.get("Error", {}).get("Code", "")
                if error_code in {"404", "NoSuchBucket"} and settings.ENV.lower() != "prod":
                    logger.info("Bucket %s missing; attempting creation", self.bucket)
                    create_kwargs = {"Bucket": self.bucket}
                    if endpoint and "localhost" in endpoint:
                        create_kwargs["CreateBucketConfiguration"] = {
                            "LocationConstraint": "us-east-1",
                        }
                    client.create_bucket(**create_kwargs)
                else:
                    raise
            return client
        except (BotoCoreError, ClientError) as exc:
            logger.warning("Falling back to filesystem storage for bucket %s: %s", self.bucket, exc)
            return None

    def _write_to_filesystem(self, data: bytes, key: str) -> str:
        base = self._ensure_filesystem_root()
        target = base / key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target.resolve().as_uri()

    def _ensure_filesystem_root(self) -> Path:
        if self._filesystem_root is None:
            root = Path("storage") / self.bucket
            root.mkdir(parents=True, exist_ok=True)
            self._filesystem_root = root
            logger.info("Using filesystem storage fallback at %s", root)
        return self._filesystem_root

    def ensure_lifecycle_policy(self) -> bool:
        """Apply cost-saving S3 lifecycle rules.

        - PDFs older than 90 days → Glacier Instant Retrieval ($0.004/GB vs $0.023)
        - Receipt images older than 180 days → Glacier Instant Retrieval
        - Tax report archives older than 365 days → Glacier Flexible Retrieval

        Idempotent — safe to call repeatedly.
        Returns True if policy was applied.
        """
        if self._client is None:
            return False
        try:
            self._client.put_bucket_lifecycle_configuration(
                Bucket=self.bucket,
                LifecycleConfiguration={
                    "Rules": [
                        {
                            "ID": "invoices-to-glacier-90d",
                            "Filter": {"Prefix": "invoices/"},
                            "Status": "Enabled",
                            "Transitions": [
                                {"Days": 90, "StorageClass": "GLACIER_IR"},
                            ],
                        },
                        {
                            "ID": "receipts-to-glacier-180d",
                            "Filter": {"Prefix": "receipts/"},
                            "Status": "Enabled",
                            "Transitions": [
                                {"Days": 180, "StorageClass": "GLACIER_IR"},
                            ],
                        },
                        {
                            "ID": "tax-reports-to-deep-365d",
                            "Filter": {"Prefix": "tax-reports/"},
                            "Status": "Enabled",
                            "Transitions": [
                                {"Days": 365, "StorageClass": "GLACIER"},
                            ],
                        },
                    ]
                },
            )
            logger.info("S3 lifecycle policy applied to bucket %s", self.bucket)
            return True
        except (BotoCoreError, ClientError) as exc:
            logger.warning("Failed to set S3 lifecycle on %s: %s", self.bucket, exc)
            return False

    def download_bytes(self, key: str) -> bytes | None:
        """Download an object from S3 and return its bytes.

        Args:
            key: The S3 object key (e.g., 'tax-reports/1/2025-02.pdf')

        Returns:
            File bytes or None if download fails / client unavailable.
        """
        if self._client is None:
            # Try filesystem fallback
            if self._filesystem_root:
                local_path = self._filesystem_root / key
                if local_path.exists():
                    return local_path.read_bytes()
            return None
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except (BotoCoreError, ClientError) as exc:
            logger.warning("Failed to download %s from S3: %s", key, exc)
            return None

    def get_presigned_url(self, key: str, expires_in: int | None = None) -> str | None:
        """Generate a fresh presigned URL for an existing S3 object.

        Args:
            key: The S3 object key (e.g., 'logos/user_34.png')
            expires_in: Optional TTL in seconds, defaults to S3_PRESIGN_TTL

        Returns:
            Presigned URL or None if S3 client is not available
        """
        if self._client is None:
            return None
        try:
            ttl = expires_in or self._presign_ttl
            url = self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=ttl,
            )
            return url
        except (BotoCoreError, ClientError) as exc:
            logger.warning("Failed to generate presigned URL for %s: %s", key, exc)
            return None

    def refresh_presigned_url(self, url: str | None, expires_in: int | None = None) -> str | None:
        """Re-sign a stored (possibly expired) presigned S3 URL from its object key.

        Presigned URLs expire (``S3_PRESIGN_TTL``, ~1h). Persisting one in the DB and
        serving the stored value later yields "AccessDenied / Request has expired".
        This extracts the object key and mints a FRESH URL so links are always valid
        when the user clicks. Non-S3 values (None, ``file://``/local paths, unparseable
        URLs) and cases where S3 isn't configured are returned unchanged.
        """
        if not url or not url.startswith("http"):
            return url
        key = self.extract_key_from_url(url)
        if not key:
            return url
        return self.get_presigned_url(key, expires_in=expires_in) or url

    def extract_key_from_url(self, url: str) -> str | None:
        """Extract S3 key from a presigned URL or stored URL.

        Args:
            url: The full S3 URL (presigned or otherwise)

        Returns:
            The S3 key (e.g., 'logos/user_34.png') or None if cannot parse
        """
        if not url:
            return None
        try:
            # Handle presigned URLs: https://bucket.s3.region.amazonaws.com/key?X-Amz-...
            # or https://bucket.s3.amazonaws.com/key?X-Amz-...
            from urllib.parse import unquote, urlparse

            parsed = urlparse(url)
            # The path starts with /, so remove it
            key = unquote(parsed.path.lstrip("/"))
            return key if key else None
        except Exception:  # noqa: BLE001
            return None


# Singleton instance for application use
s3_client = S3Client()
