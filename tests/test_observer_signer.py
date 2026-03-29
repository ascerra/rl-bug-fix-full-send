"""Tests for the observer attestation signer (Phase 8.3).

Covers:
- SignedAttestation dataclass: to_dict, write to directory
- VerificationResult dataclass: to_dict
- AttestationSigner.sign() dispatch: all 3 methods, unsupported method
- sign_none: unsigned envelope, correct digest
- sign_sigstore (mocked cosign): success, cosign failure, cosign not found
- sign_cosign_key (mocked cosign): success, missing key, cosign failure, empty key_path
- verify: all methods, tampered payload, missing bundle, unsigned
- _check_cosign_available: found, not found, non-zero exit
- _sha256_hex: correctness, determinism
- Integration: AttestationBuilder → serialize → sign → verify round-trip
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from engine.observer.signer import (
    SUPPORTED_METHODS,
    AttestationSigner,
    SignedAttestation,
    VerificationResult,
    _check_cosign_available,
    _sha256_hex,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_PAYLOAD = '{"_type":"https://in-toto.io/Statement/v1","subject":[]}'
SAMPLE_DIGEST = hashlib.sha256(SAMPLE_PAYLOAD.encode()).hexdigest()
SAMPLE_BUNDLE = {
    "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
    "verificationMaterial": {"certificate": {"rawBytes": "fakecert=="}},
    "messageSignature": {"signature": "fakesig=="},
}


def _make_signed(
    method: str = "sigstore",
    signed: bool = True,
    bundle: dict[str, Any] | None = None,
    payload: str = SAMPLE_PAYLOAD,
) -> SignedAttestation:
    return SignedAttestation(
        payload=payload,
        payload_digest=_sha256_hex(payload),
        bundle=bundle if bundle is not None else SAMPLE_BUNDLE,
        signing_method=method,
        signed=signed,
    )


def _cosign_sign_side_effect(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Mock side-effect that writes a fake bundle file when cosign sign-blob runs."""
    bundle_idx = None
    for i, arg in enumerate(cmd):
        if arg == "--bundle" and i + 1 < len(cmd):
            bundle_idx = i + 1
            break
    if bundle_idx is not None:
        Path(cmd[bundle_idx]).write_text(json.dumps(SAMPLE_BUNDLE))
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _cosign_version_ok(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, 0, stdout="cosign v2.4.0", stderr="")


# ===========================================================================
# SignedAttestation dataclass
# ===========================================================================


class TestSignedAttestation:
    def test_to_dict(self):
        sa = _make_signed()
        d = sa.to_dict()
        assert d["payload"] == SAMPLE_PAYLOAD
        assert d["payload_digest"] == _sha256_hex(SAMPLE_PAYLOAD)
        assert d["signing_method"] == "sigstore"
        assert d["signed"] is True
        assert d["bundle"] == SAMPLE_BUNDLE

    def test_to_dict_unsigned(self):
        sa = SignedAttestation(payload="x", signing_method="none", signed=False)
        d = sa.to_dict()
        assert d["signed"] is False
        assert d["bundle"] == {}

    def test_write_creates_directory(self, tmp_path: Path):
        sa = _make_signed()
        out = tmp_path / "nested" / "output"
        paths = sa.write(out)
        assert out.exists()
        assert "attestation" in paths
        assert Path(paths["attestation"]).exists()

    def test_write_attestation_file(self, tmp_path: Path):
        sa = _make_signed()
        paths = sa.write(tmp_path)
        content = Path(paths["attestation"]).read_text()
        assert content == SAMPLE_PAYLOAD

    def test_write_bundle_file(self, tmp_path: Path):
        sa = _make_signed()
        paths = sa.write(tmp_path)
        assert "bundle" in paths
        content = json.loads(Path(paths["bundle"]).read_text())
        assert content == SAMPLE_BUNDLE

    def test_write_no_bundle_when_empty(self, tmp_path: Path):
        sa = SignedAttestation(payload="x", bundle={})
        paths = sa.write(tmp_path)
        assert "bundle" not in paths

    def test_write_metadata_file(self, tmp_path: Path):
        sa = _make_signed()
        paths = sa.write(tmp_path)
        assert "metadata" in paths
        meta = json.loads(Path(paths["metadata"]).read_text())
        assert meta["signing_method"] == "sigstore"
        assert meta["signed"] is True


# ===========================================================================
# VerificationResult dataclass
# ===========================================================================


class TestVerificationResult:
    def test_to_dict(self):
        vr = VerificationResult(valid=True, details="ok", signing_method="sigstore")
        d = vr.to_dict()
        assert d == {"valid": True, "details": "ok", "signing_method": "sigstore"}

    def test_defaults(self):
        vr = VerificationResult()
        assert vr.valid is False
        assert vr.details == ""
        assert vr.signing_method == ""


# ===========================================================================
# sign_none
# ===========================================================================


class TestSignNone:
    def test_returns_unsigned(self):
        signer = AttestationSigner()
        result = signer.sign_none(SAMPLE_PAYLOAD)
        assert result.signed is False
        assert result.signing_method == "none"
        assert result.bundle == {}

    def test_correct_digest(self):
        signer = AttestationSigner()
        result = signer.sign_none(SAMPLE_PAYLOAD)
        assert result.payload_digest == SAMPLE_DIGEST

    def test_payload_preserved(self):
        signer = AttestationSigner()
        result = signer.sign_none(SAMPLE_PAYLOAD)
        assert result.payload == SAMPLE_PAYLOAD


# ===========================================================================
# sign_sigstore (mocked cosign)
# ===========================================================================


class TestSignSigstore:
    @patch("engine.observer.signer.subprocess.run")
    def test_success(self, mock_run: MagicMock):
        mock_run.side_effect = [
            _cosign_version_ok(["cosign", "version"]),
            None,
        ]

        def sign_effect(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            return _cosign_sign_side_effect(cmd, **kw)

        mock_run.side_effect = [
            _cosign_version_ok(["cosign", "version"]),
            sign_effect,
        ]
        mock_run.side_effect = None
        mock_run.side_effect = _dispatch_cosign_mock

        signer = AttestationSigner()
        result = signer.sign_sigstore(SAMPLE_PAYLOAD)

        assert result.signed is True
        assert result.signing_method == "sigstore"
        assert result.payload == SAMPLE_PAYLOAD
        assert result.payload_digest == SAMPLE_DIGEST
        assert result.bundle == SAMPLE_BUNDLE

    @patch("engine.observer.signer.subprocess.run")
    def test_cosign_failure(self, mock_run: MagicMock):
        mock_run.side_effect = _dispatch_cosign_mock_sign_fail

        signer = AttestationSigner()
        with pytest.raises(RuntimeError, match="cosign sign-blob failed"):
            signer.sign_sigstore(SAMPLE_PAYLOAD)

    @patch("engine.observer.signer.subprocess.run")
    def test_cosign_not_found(self, mock_run: MagicMock):
        mock_run.side_effect = FileNotFoundError("cosign not found")

        signer = AttestationSigner()
        with pytest.raises(RuntimeError, match="cosign CLI not found"):
            signer.sign_sigstore(SAMPLE_PAYLOAD)


# ===========================================================================
# sign_cosign_key (mocked cosign)
# ===========================================================================


class TestSignCosignKey:
    @patch("engine.observer.signer.subprocess.run")
    def test_success(self, mock_run: MagicMock, tmp_path: Path):
        key_file = tmp_path / "cosign.key"
        key_file.write_text("-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----")

        mock_run.side_effect = _dispatch_cosign_mock

        signer = AttestationSigner()
        result = signer.sign_cosign_key(SAMPLE_PAYLOAD, str(key_file))

        assert result.signed is True
        assert result.signing_method == "cosign-key"
        assert result.payload_digest == SAMPLE_DIGEST
        assert result.bundle == SAMPLE_BUNDLE

    def test_missing_key_file(self, tmp_path: Path):
        signer = AttestationSigner()
        with pytest.raises(ValueError, match="Key file not found"):
            signer.sign_cosign_key(SAMPLE_PAYLOAD, str(tmp_path / "nope.key"))

    @patch("engine.observer.signer.subprocess.run")
    def test_cosign_failure(self, mock_run: MagicMock, tmp_path: Path):
        key_file = tmp_path / "cosign.key"
        key_file.write_text("key")

        mock_run.side_effect = _dispatch_cosign_mock_sign_fail

        signer = AttestationSigner()
        with pytest.raises(RuntimeError, match="cosign sign-blob --key failed"):
            signer.sign_cosign_key(SAMPLE_PAYLOAD, str(key_file))

    @patch("engine.observer.signer.subprocess.run")
    def test_sets_cosign_password_env(self, mock_run: MagicMock, tmp_path: Path):
        key_file = tmp_path / "cosign.key"
        key_file.write_text("key")

        captured_env: dict[str, str] = {}

        def capture_env(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if "sign-blob" in cmd:
                captured_env.update(kwargs.get("env", {}))
                return _cosign_sign_side_effect(cmd, **kwargs)
            return _cosign_version_ok(cmd)

        mock_run.side_effect = capture_env

        signer = AttestationSigner()
        signer.sign_cosign_key(SAMPLE_PAYLOAD, str(key_file))
        assert "COSIGN_PASSWORD" in captured_env


# ===========================================================================
# sign() dispatch
# ===========================================================================


class TestSignDispatch:
    @patch("engine.observer.signer.subprocess.run")
    def test_dispatch_sigstore(self, mock_run: MagicMock):
        mock_run.side_effect = _dispatch_cosign_mock
        signer = AttestationSigner()
        result = signer.sign(SAMPLE_PAYLOAD, "sigstore")
        assert result.signing_method == "sigstore"

    def test_dispatch_none(self):
        signer = AttestationSigner()
        result = signer.sign(SAMPLE_PAYLOAD, "none")
        assert result.signing_method == "none"
        assert result.signed is False

    @patch("engine.observer.signer.subprocess.run")
    def test_dispatch_cosign_key(self, mock_run: MagicMock, tmp_path: Path):
        key_file = tmp_path / "key.pem"
        key_file.write_text("key")
        mock_run.side_effect = _dispatch_cosign_mock
        signer = AttestationSigner()
        result = signer.sign(SAMPLE_PAYLOAD, "cosign-key", key_path=str(key_file))
        assert result.signing_method == "cosign-key"

    def test_dispatch_unsupported(self):
        signer = AttestationSigner()
        with pytest.raises(ValueError, match="Unsupported signing method"):
            signer.sign(SAMPLE_PAYLOAD, "pgp")

    def test_dispatch_cosign_key_no_path(self):
        signer = AttestationSigner()
        with pytest.raises(ValueError, match="key_path is required"):
            signer.sign(SAMPLE_PAYLOAD, "cosign-key")


# ===========================================================================
# verify
# ===========================================================================


class TestVerify:
    def test_verify_none_always_valid(self):
        sa = SignedAttestation(
            payload="x",
            payload_digest=_sha256_hex("x"),
            signing_method="none",
            signed=False,
        )
        signer = AttestationSigner()
        result = signer.verify(sa)
        assert result.valid is True
        assert result.signing_method == "none"

    def test_verify_unsigned_invalid(self):
        sa = SignedAttestation(
            payload="x",
            payload_digest=_sha256_hex("x"),
            signing_method="sigstore",
            signed=False,
        )
        signer = AttestationSigner()
        result = signer.verify(sa)
        assert result.valid is False
        assert "unsigned" in result.details

    def test_verify_no_bundle(self):
        sa = SignedAttestation(
            payload="x",
            payload_digest=_sha256_hex("x"),
            signing_method="sigstore",
            signed=True,
            bundle={},
        )
        signer = AttestationSigner()
        result = signer.verify(sa)
        assert result.valid is False
        assert "bundle" in result.details.lower()

    def test_verify_tampered_payload(self):
        sa = _make_signed()
        sa.payload = "TAMPERED"
        signer = AttestationSigner()
        result = signer.verify(sa)
        assert result.valid is False
        assert "tamper" in result.details.lower()

    @patch("engine.observer.signer.subprocess.run")
    def test_verify_sigstore_success(self, mock_run: MagicMock):
        mock_run.side_effect = _dispatch_cosign_verify_ok
        sa = _make_signed(method="sigstore")
        signer = AttestationSigner()
        result = signer.verify(
            sa,
            certificate_identity="https://github.com/org/repo/.github/workflows/ci.yml@refs/heads/main",
            certificate_oidc_issuer="https://token.actions.githubusercontent.com",
        )
        assert result.valid is True

    @patch("engine.observer.signer.subprocess.run")
    def test_verify_sigstore_includes_identity_args(self, mock_run: MagicMock):
        captured_cmds: list[list[str]] = []

        def capture(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            captured_cmds.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        mock_run.side_effect = capture
        sa = _make_signed(method="sigstore")
        signer = AttestationSigner()
        signer.verify(
            sa,
            certificate_identity="my-id",
            certificate_oidc_issuer="my-issuer",
        )

        verify_cmd = [c for c in captured_cmds if "verify-blob" in c]
        assert len(verify_cmd) == 1
        assert "--certificate-identity" in verify_cmd[0]
        assert "my-id" in verify_cmd[0]
        assert "--certificate-oidc-issuer" in verify_cmd[0]
        assert "my-issuer" in verify_cmd[0]

    @patch("engine.observer.signer.subprocess.run")
    def test_verify_sigstore_failure(self, mock_run: MagicMock):
        mock_run.side_effect = _dispatch_cosign_verify_fail
        sa = _make_signed(method="sigstore")
        signer = AttestationSigner()
        result = signer.verify(sa)
        assert result.valid is False
        assert "Verification failed" in result.details

    @patch("engine.observer.signer.subprocess.run")
    def test_verify_cosign_key_success(self, mock_run: MagicMock):
        mock_run.side_effect = _dispatch_cosign_verify_ok
        sa = _make_signed(method="cosign-key")
        signer = AttestationSigner()
        result = signer.verify(sa, key_path="/tmp/cosign.pub")
        assert result.valid is True

    @patch("engine.observer.signer.subprocess.run")
    def test_verify_cosign_key_includes_key_arg(self, mock_run: MagicMock):
        captured_cmds: list[list[str]] = []

        def capture(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
            captured_cmds.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

        mock_run.side_effect = capture
        sa = _make_signed(method="cosign-key")
        signer = AttestationSigner()
        signer.verify(sa, key_path="/my/key.pub")

        verify_cmd = [c for c in captured_cmds if "verify-blob" in c]
        assert len(verify_cmd) == 1
        assert "--key" in verify_cmd[0]
        assert "/my/key.pub" in verify_cmd[0]

    def test_verify_cosign_key_missing_key_path(self):
        sa = _make_signed(method="cosign-key")
        signer = AttestationSigner()
        with patch("engine.observer.signer.subprocess.run", side_effect=_dispatch_cosign_verify_ok):
            result = signer.verify(sa)
        assert result.valid is False
        assert "key_path required" in result.details

    @patch("engine.observer.signer.subprocess.run")
    def test_verify_cosign_key_failure(self, mock_run: MagicMock):
        mock_run.side_effect = _dispatch_cosign_verify_fail
        sa = _make_signed(method="cosign-key")
        signer = AttestationSigner()
        result = signer.verify(sa, key_path="/tmp/cosign.pub")
        assert result.valid is False


# ===========================================================================
# _check_cosign_available
# ===========================================================================


class TestCheckCosignAvailable:
    @patch("engine.observer.signer.subprocess.run")
    def test_found(self, mock_run: MagicMock):
        mock_run.return_value = subprocess.CompletedProcess(
            ["cosign", "version"], 0, stdout="cosign v2.4.0", stderr=""
        )
        _check_cosign_available()

    @patch("engine.observer.signer.subprocess.run")
    def test_not_found(self, mock_run: MagicMock):
        mock_run.side_effect = FileNotFoundError()
        with pytest.raises(RuntimeError, match="cosign CLI not found"):
            _check_cosign_available()

    @patch("engine.observer.signer.subprocess.run")
    def test_non_zero_exit(self, mock_run: MagicMock):
        mock_run.return_value = subprocess.CompletedProcess(
            ["cosign", "version"], 1, stdout="", stderr="error"
        )
        with pytest.raises(RuntimeError, match="non-zero"):
            _check_cosign_available()


# ===========================================================================
# _sha256_hex
# ===========================================================================


class TestSha256Hex:
    def test_known_value(self):
        expected = hashlib.sha256(b"hello").hexdigest()
        assert _sha256_hex("hello") == expected

    def test_deterministic(self):
        assert _sha256_hex("test") == _sha256_hex("test")

    def test_different_inputs(self):
        assert _sha256_hex("a") != _sha256_hex("b")

    def test_empty_string(self):
        expected = hashlib.sha256(b"").hexdigest()
        assert _sha256_hex("") == expected


# ===========================================================================
# SUPPORTED_METHODS constant
# ===========================================================================


class TestSupportedMethods:
    def test_contains_all_three(self):
        assert "sigstore" in SUPPORTED_METHODS
        assert "cosign-key" in SUPPORTED_METHODS
        assert "none" in SUPPORTED_METHODS

    def test_is_tuple(self):
        assert isinstance(SUPPORTED_METHODS, tuple)


# ===========================================================================
# Integration: build → serialize → sign → verify
# ===========================================================================


class TestIntegration:
    def test_none_round_trip(self):
        from engine.observer import CrossCheckReport
        from engine.observer.attestation import AttestationBuilder

        builder = AttestationBuilder()
        att = builder.build(timeline=[], cross_check_report=CrossCheckReport())
        canonical = AttestationBuilder.serialize(att)

        signer = AttestationSigner()
        signed = signer.sign(canonical, "none")
        assert signed.payload == canonical
        assert signed.signed is False

        result = signer.verify(signed)
        assert result.valid is True

    @patch("engine.observer.signer.subprocess.run")
    def test_sigstore_round_trip(self, mock_run: MagicMock):
        mock_run.side_effect = _dispatch_cosign_mock_full

        from engine.observer import CrossCheckReport, ModelInfo
        from engine.observer.attestation import AttestationBuilder

        builder = AttestationBuilder()
        att = builder.build(
            timeline=[],
            cross_check_report=CrossCheckReport(),
            model_info=[ModelInfo(model="gemini-2.5-pro", provider="google", total_calls=1)],
        )
        canonical = AttestationBuilder.serialize(att)

        signer = AttestationSigner()
        signed = signer.sign(canonical, "sigstore")
        assert signed.signed is True

        result = signer.verify(
            signed,
            certificate_identity="test",
            certificate_oidc_issuer="test",
        )
        assert result.valid is True

    @patch("engine.observer.signer.subprocess.run")
    def test_cosign_key_round_trip(self, mock_run: MagicMock, tmp_path: Path):
        mock_run.side_effect = _dispatch_cosign_mock_full

        key_file = tmp_path / "cosign.key"
        key_file.write_text("key")

        from engine.observer.attestation import AttestationBuilder

        att = AttestationBuilder().build(
            timeline=[],
            cross_check_report=__import__(
                "engine.observer", fromlist=["CrossCheckReport"]
            ).CrossCheckReport(),
        )
        canonical = AttestationBuilder.serialize(att)

        signer = AttestationSigner()
        signed = signer.sign(canonical, "cosign-key", key_path=str(key_file))
        assert signed.signed is True

        result = signer.verify(signed, key_path=str(key_file))
        assert result.valid is True

    def test_signed_attestation_write_and_read_back(self, tmp_path: Path):
        sa = _make_signed()
        paths = sa.write(tmp_path / "output")

        payload = Path(paths["attestation"]).read_text()
        assert payload == SAMPLE_PAYLOAD

        bundle = json.loads(Path(paths["bundle"]).read_text())
        assert bundle == SAMPLE_BUNDLE

        meta = json.loads(Path(paths["metadata"]).read_text())
        assert meta["signed"] is True
        assert meta["signing_method"] == "sigstore"
        assert meta["payload_digest"] == SAMPLE_DIGEST


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _dispatch_cosign_mock(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Route mock calls: cosign version → OK, cosign sign-blob → OK + bundle file."""
    if cmd[0:2] == ["cosign", "version"]:
        return _cosign_version_ok(cmd)
    if "sign-blob" in cmd:
        return _cosign_sign_side_effect(cmd, **kwargs)
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _dispatch_cosign_mock_sign_fail(
    cmd: list[str], **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    if cmd[0:2] == ["cosign", "version"]:
        return _cosign_version_ok(cmd)
    if "sign-blob" in cmd:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="signing error")
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _dispatch_cosign_verify_ok(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    if cmd[0:2] == ["cosign", "version"]:
        return _cosign_version_ok(cmd)
    if "verify-blob" in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout="Verified OK", stderr="")
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _dispatch_cosign_verify_fail(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    if cmd[0:2] == ["cosign", "version"]:
        return _cosign_version_ok(cmd)
    if "verify-blob" in cmd:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="signature mismatch")
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _dispatch_cosign_mock_full(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Handles version, sign-blob, and verify-blob."""
    if cmd[0:2] == ["cosign", "version"]:
        return _cosign_version_ok(cmd)
    if "sign-blob" in cmd:
        return _cosign_sign_side_effect(cmd, **kwargs)
    if "verify-blob" in cmd:
        return subprocess.CompletedProcess(cmd, 0, stdout="Verified OK", stderr="")
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
