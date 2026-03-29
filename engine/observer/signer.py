"""Attestation signer — signs attestations via Sigstore or local keys.

Wraps the ``cosign`` CLI for Sigstore keyless signing and local key-pair
signing.  A no-op mode is provided for local development and testing
where no signing infrastructure is available.

Three signing modes (selected via ``observer.signing_method`` in config):

- **sigstore** — keyless signing via GitHub Actions OIDC.  ``cosign``
  automatically acquires the OIDC token from the runner environment.
- **cosign-key** — signing with a local private key (for non-OIDC
  environments or offline testing).
- **none** — no-op; writes an unsigned attestation envelope.

See SPEC.md §5.6 and IMPLEMENTATION-PLAN.md §8.3.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUPPORTED_METHODS = ("sigstore", "cosign-key", "none")


@dataclass
class SignedAttestation:
    """Container for a signed (or unsigned) attestation."""

    payload: str = ""
    payload_digest: str = ""
    bundle: dict[str, Any] = field(default_factory=dict)
    signing_method: str = "none"
    signed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "payload": self.payload,
            "payload_digest": self.payload_digest,
            "bundle": self.bundle,
            "signing_method": self.signing_method,
            "signed": self.signed,
        }

    def write(self, output_dir: str | Path) -> dict[str, str]:
        """Write attestation, bundle, and metadata to *output_dir*.

        Returns a mapping of artifact name → file path.
        """
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)

        paths: dict[str, str] = {}

        att_path = out / "attestation.json"
        att_path.write_text(self.payload)
        paths["attestation"] = str(att_path)

        if self.bundle:
            bundle_path = out / "attestation.bundle.json"
            bundle_path.write_text(json.dumps(self.bundle, indent=2))
            paths["bundle"] = str(bundle_path)

        meta_path = out / "signing-metadata.json"
        meta = {
            "signing_method": self.signing_method,
            "signed": self.signed,
            "payload_digest": self.payload_digest,
        }
        meta_path.write_text(json.dumps(meta, indent=2))
        paths["metadata"] = str(meta_path)

        return paths


@dataclass
class VerificationResult:
    """Result of verifying a signed attestation."""

    valid: bool = False
    details: str = ""
    signing_method: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "details": self.details,
            "signing_method": self.signing_method,
        }


class AttestationSigner:
    """Signs and verifies in-toto attestations.

    Delegates to the ``cosign`` CLI for Sigstore and key-based signing.
    Falls back to no-op signing for local development.
    """

    def sign(
        self,
        attestation_json: str,
        method: str = "none",
        *,
        key_path: str = "",
    ) -> SignedAttestation:
        """Sign *attestation_json* using *method*.

        Args:
            attestation_json: Canonical JSON string (from
                ``AttestationBuilder.serialize``).
            method: ``"sigstore"``, ``"cosign-key"``, or ``"none"``.
            key_path: Path to cosign private key (required for
                ``cosign-key``).

        Raises:
            ValueError: Unsupported method or missing required params.
            RuntimeError: ``cosign`` CLI failure.
        """
        if method not in SUPPORTED_METHODS:
            raise ValueError(
                f"Unsupported signing method: {method!r}. Supported: {', '.join(SUPPORTED_METHODS)}"
            )

        if method == "sigstore":
            return self.sign_sigstore(attestation_json)
        if method == "cosign-key":
            if not key_path:
                raise ValueError("key_path is required for cosign-key signing")
            return self.sign_cosign_key(attestation_json, key_path)
        return self.sign_none(attestation_json)

    def sign_sigstore(self, attestation_json: str) -> SignedAttestation:
        """Keyless signing via Sigstore OIDC.

        In GitHub Actions, ``cosign`` automatically acquires the OIDC
        token from ``ACTIONS_ID_TOKEN_REQUEST_TOKEN`` /
        ``ACTIONS_ID_TOKEN_REQUEST_URL``.
        """
        _check_cosign_available()
        digest = _sha256_hex(attestation_json)

        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "attestation.json"
            bundle_path = Path(tmpdir) / "attestation.bundle.json"
            payload_path.write_text(attestation_json)

            cmd = [
                "cosign",
                "sign-blob",
                "--yes",
                "--bundle",
                str(bundle_path),
                str(payload_path),
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

            if result.returncode != 0:
                raise RuntimeError(
                    f"cosign sign-blob failed (exit {result.returncode}): {result.stderr.strip()}"
                )

            bundle: dict[str, Any] = {}
            if bundle_path.exists():
                bundle = json.loads(bundle_path.read_text())

            return SignedAttestation(
                payload=attestation_json,
                payload_digest=digest,
                bundle=bundle,
                signing_method="sigstore",
                signed=True,
            )

    def sign_cosign_key(self, attestation_json: str, key_path: str) -> SignedAttestation:
        """Sign with a local cosign private key."""
        key = Path(key_path)
        if not key.exists():
            raise ValueError(f"Key file not found: {key_path}")

        _check_cosign_available()
        digest = _sha256_hex(attestation_json)

        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "attestation.json"
            bundle_path = Path(tmpdir) / "attestation.bundle.json"
            payload_path.write_text(attestation_json)

            cmd = [
                "cosign",
                "sign-blob",
                "--yes",
                "--key",
                str(key),
                "--bundle",
                str(bundle_path),
                str(payload_path),
            ]

            env = os.environ.copy()
            if "COSIGN_PASSWORD" not in env:
                env["COSIGN_PASSWORD"] = ""

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)

            if result.returncode != 0:
                raise RuntimeError(
                    f"cosign sign-blob --key failed "
                    f"(exit {result.returncode}): "
                    f"{result.stderr.strip()}"
                )

            bundle: dict[str, Any] = {}
            if bundle_path.exists():
                bundle = json.loads(bundle_path.read_text())

            return SignedAttestation(
                payload=attestation_json,
                payload_digest=digest,
                bundle=bundle,
                signing_method="cosign-key",
                signed=True,
            )

    def sign_none(self, attestation_json: str) -> SignedAttestation:
        """No-op signer for local development and testing."""
        return SignedAttestation(
            payload=attestation_json,
            payload_digest=_sha256_hex(attestation_json),
            bundle={},
            signing_method="none",
            signed=False,
        )

    def verify(
        self,
        signed_attestation: SignedAttestation,
        *,
        key_path: str = "",
        certificate_identity: str = "",
        certificate_oidc_issuer: str = "",
    ) -> VerificationResult:
        """Verify a signed attestation.

        Args:
            signed_attestation: The attestation to verify.
            key_path: Public key path for ``cosign-key`` verification.
            certificate_identity: Expected identity (Sigstore).
            certificate_oidc_issuer: Expected OIDC issuer (Sigstore).
        """
        method = signed_attestation.signing_method

        if method == "none":
            return VerificationResult(
                valid=True,
                details="Unsigned attestation — no verification needed",
                signing_method="none",
            )

        if not signed_attestation.signed:
            return VerificationResult(
                valid=False,
                details="Attestation is marked as unsigned",
                signing_method=method,
            )

        if not signed_attestation.bundle:
            return VerificationResult(
                valid=False,
                details="No bundle present for verification",
                signing_method=method,
            )

        digest = _sha256_hex(signed_attestation.payload)
        if digest != signed_attestation.payload_digest:
            return VerificationResult(
                valid=False,
                details="Payload digest mismatch — possible tampering",
                signing_method=method,
            )

        _check_cosign_available()

        with tempfile.TemporaryDirectory() as tmpdir:
            payload_path = Path(tmpdir) / "attestation.json"
            bundle_path = Path(tmpdir) / "attestation.bundle.json"
            payload_path.write_text(signed_attestation.payload)
            bundle_path.write_text(json.dumps(signed_attestation.bundle))

            cmd = ["cosign", "verify-blob", "--bundle", str(bundle_path)]

            if method == "cosign-key":
                if not key_path:
                    return VerificationResult(
                        valid=False,
                        details="key_path required for cosign-key verification",
                        signing_method=method,
                    )
                cmd.extend(["--key", key_path])
            elif method == "sigstore":
                if certificate_identity:
                    cmd.extend(["--certificate-identity", certificate_identity])
                if certificate_oidc_issuer:
                    cmd.extend(
                        [
                            "--certificate-oidc-issuer",
                            certificate_oidc_issuer,
                        ]
                    )

            cmd.append(str(payload_path))

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

            if result.returncode == 0:
                return VerificationResult(
                    valid=True,
                    details="Signature verified successfully",
                    signing_method=method,
                )
            return VerificationResult(
                valid=False,
                details=f"Verification failed: {result.stderr.strip()}",
                signing_method=method,
            )


def _sha256_hex(data: str) -> str:
    """Compute SHA-256 hex digest of a UTF-8 string."""
    return hashlib.sha256(data.encode()).hexdigest()


def _check_cosign_available() -> None:
    """Raise ``RuntimeError`` if the ``cosign`` CLI is not on PATH."""
    try:
        result = subprocess.run(
            ["cosign", "version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError("cosign version check returned non-zero")
    except FileNotFoundError:
        raise RuntimeError(
            "cosign CLI not found on PATH. "
            "Install: https://docs.sigstore.dev/cosign/system_config/installation/"
        ) from None
