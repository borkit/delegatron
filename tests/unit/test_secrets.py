"""Tests for the secrets scanning module."""

from __future__ import annotations

from pathlib import Path

import pytest

from delegatron.security.patch_lines import added_lines
from delegatron.security.secrets import (
    has_secrets,
    scan_patch,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_patch(added_lines: list[str]) -> dict:
    """Build a minimal patch dict with added lines in unified diff format."""
    diff_lines = [
        "--- a/file.py",
        "+++ b/file.py",
        "@@ -0,0 +1 @@",
    ]
    for line in added_lines:
        diff_lines.append(f"+{line}")
    return {"diff_unified": "\n".join(diff_lines)}


# ---------------------------------------------------------------------------
# AWS keys
# ---------------------------------------------------------------------------


class TestAWSKeys:
    def test_detects_aws_access_key(self) -> None:
        patch = _make_patch(["AWS_KEY = 'AKIAIOSFODNN7EXAMPLE'"])
        findings = scan_patch(patch)
        assert len(findings) == 1
        assert findings[0].pattern_name == "aws_access_key"
        assert findings[0].severity == "critical"

    def test_no_false_positive_on_short_akia(self) -> None:
        patch = _make_patch(["# AKIA is just a prefix"])
        findings = scan_patch(patch)
        assert len(findings) == 0


# ---------------------------------------------------------------------------
# Generic API keys
# ---------------------------------------------------------------------------


class TestGenericAPIKeys:
    def test_detects_api_key_assignment(self) -> None:
        patch = _make_patch(["api_key = 'abcdefghij1234567890ABCD'"])
        findings = scan_patch(patch)
        assert len(findings) == 1
        assert findings[0].pattern_name == "generic_api_key"
        assert findings[0].severity == "high"

    def test_detects_apikey_colon(self) -> None:
        patch = _make_patch(['apikey: "xyzXYZ0123456789abcdefgh"'])
        findings = scan_patch(patch)
        assert len(findings) == 1
        assert findings[0].pattern_name == "generic_api_key"

    def test_no_false_positive_short_value(self) -> None:
        patch = _make_patch(["api_key = 'short'"])
        findings = scan_patch(patch)
        assert len(findings) == 0


# ---------------------------------------------------------------------------
# Private keys
# ---------------------------------------------------------------------------


class TestPrivateKeys:
    def test_detects_rsa_private_key(self) -> None:
        patch = _make_patch(["-----BEGIN RSA PRIVATE KEY-----"])
        findings = scan_patch(patch)
        assert len(findings) == 1
        assert findings[0].pattern_name == "private_key"
        assert findings[0].severity == "critical"

    def test_detects_ec_private_key(self) -> None:
        patch = _make_patch(["-----BEGIN EC PRIVATE KEY-----"])
        findings = scan_patch(patch)
        assert len(findings) == 1
        assert findings[0].pattern_name == "private_key"

    def test_detects_openssh_private_key(self) -> None:
        patch = _make_patch(["-----BEGIN OPENSSH PRIVATE KEY-----"])
        findings = scan_patch(patch)
        assert len(findings) == 1
        assert findings[0].pattern_name == "private_key"

    def test_no_false_positive_on_public_key(self) -> None:
        patch = _make_patch(["-----BEGIN PUBLIC KEY-----"])
        findings = scan_patch(patch)
        assert len(findings) == 0


# ---------------------------------------------------------------------------
# GitHub tokens
# ---------------------------------------------------------------------------


class TestGitHubTokens:
    def test_detects_ghp_token(self) -> None:
        token = "ghp_" + "A" * 36
        patch = _make_patch([f'GITHUB_TOKEN = "{token}"'])
        findings = scan_patch(patch)
        assert len(findings) == 1
        assert findings[0].pattern_name == "github_token"
        assert findings[0].severity == "critical"

    def test_detects_gho_token(self) -> None:
        token = "gho_" + "b" * 36
        patch = _make_patch([f"token={token}"])
        findings = scan_patch(patch)
        assert any(f.pattern_name == "github_token" for f in findings)

    def test_detects_github_pat(self) -> None:
        token = "github_pat_" + "X" * 22
        patch = _make_patch([f"pat = '{token}'"])
        findings = scan_patch(patch)
        assert any(f.pattern_name == "github_pat" for f in findings)


# ---------------------------------------------------------------------------
# Stripe keys
# ---------------------------------------------------------------------------


class TestStripeKeys:
    # Real Stripe keys use underscores: sk_live_..., sk_test_..., rk_live_...
    @pytest.mark.parametrize("prefix", ["sk_live_", "sk_test_", "rk_live_"])
    def test_detects_stripe_key(self, prefix: str) -> None:
        key = prefix + "a1B2" * 6
        findings = scan_patch(_make_patch([f"STRIPE_KEY = '{key}'"]))
        assert len(findings) == 1
        assert findings[0].pattern_name == "stripe_key"
        assert findings[0].severity == "critical"


# ---------------------------------------------------------------------------
# Provider API keys
# ---------------------------------------------------------------------------


class TestProviderKeys:
    @pytest.mark.parametrize(
        ("key", "name"),
        [
            ("sk-ant-api03-" + "x" * 30, "anthropic_api_key"),
            ("sk-proj-" + "Y" * 30, "openai_api_key"),
        ],
    )
    def test_detects_provider_key(self, key: str, name: str) -> None:
        findings = scan_patch(_make_patch([f'client = OpenAI(api_key="{key}")']))
        assert [(f.pattern_name, f.severity) for f in findings] == [(name, "critical")]


# ---------------------------------------------------------------------------
# Connection strings
# ---------------------------------------------------------------------------


class TestConnectionStrings:
    def test_detects_postgres_url(self) -> None:
        patch = _make_patch(["DATABASE_URL = 'postgres://user:pass@host:5432/mydb'"])
        findings = scan_patch(patch)
        assert any(f.pattern_name == "connection_string" for f in findings)
        assert any(f.severity == "high" for f in findings)

    def test_detects_mysql_url(self) -> None:
        patch = _make_patch(["url = 'mysql://root:secret@db.internal.example.com/db'"])
        findings = scan_patch(patch)
        assert any(f.pattern_name == "connection_string" for f in findings)

    def test_detects_remote_postgres_with_password(self) -> None:
        line = 'DATABASE_URL = "postgresql://app:S3cr3tPw@db.prod.example.com/app"'
        findings = scan_patch(_make_patch([line]))
        assert [f.pattern_name for f in findings] == ["connection_string"]
        assert "S3cr3tPw" not in findings[0].line_content

    def test_detects_driver_qualified_scheme(self) -> None:
        line = "engine = create_engine('postgresql+psycopg2://u:hunter2@db.example.com/x')"
        assert [f.pattern_name for f in scan_patch(_make_patch([line]))] == ["connection_string"]

    @pytest.mark.parametrize(
        "line",
        [
            'CELERY_BROKER_URL = "redis://localhost:6379/0"',
            'os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost/test")',
            "url = 'mysql://root:secret@127.0.0.1/db'",
            "url = 'redis://:pw@[::1]:6379'",
            "url = 'postgres://reader@db.example.com/app'",  # no password
            'url = f"postgresql://{user}:{password}@{host}/db"',  # template
        ],
    )
    def test_no_finding_for_loopback_passwordless_or_template(self, line: str) -> None:
        assert scan_patch(_make_patch([line])) == []

    def test_detects_mongodb_url(self) -> None:
        patch = _make_patch(["MONGO = 'mongodb://admin:password@mongo.example.com/prod'"])
        findings = scan_patch(patch)
        assert any(f.pattern_name == "connection_string" for f in findings)

    def test_detects_redis_url(self) -> None:
        patch = _make_patch(["REDIS = 'redis://default:secretpass@redis.io:6379'"])
        findings = scan_patch(patch)
        assert any(f.pattern_name == "connection_string" for f in findings)

    def test_no_false_positive_on_schema_only(self) -> None:
        # "postgres://" alone with very short content shouldn't match
        patch = _make_patch(["# protocol: postgres://"])
        findings = scan_patch(patch)
        assert not any(f.pattern_name == "connection_string" for f in findings)


# ---------------------------------------------------------------------------
# High entropy strings
# ---------------------------------------------------------------------------


class TestHighEntropy:
    def test_detects_high_entropy_base64_in_assignment(self) -> None:
        # A base64 blob > 40 chars in an assignment context
        blob = "U2VjcmV0S2V5VGhhdElzVmVyeUxvbmdBbmRTaG91bGRCZURldGVjdGVk"
        patch = _make_patch([f"secret_key = '{blob}'"])
        findings = scan_patch(patch)
        assert len(findings) >= 1
        # Could match generic_token or high_entropy_string
        names = {f.pattern_name for f in findings}
        assert names & {"high_entropy_string", "generic_token", "generic_api_key"}

    def test_no_false_positive_without_assignment_context(self) -> None:
        # High entropy but not in a key-like assignment
        blob = "U2VjcmV0S2V5VGhhdElzVmVyeUxvbmdBbmRTaG91bGRCZURldGVjdGVk"
        patch = _make_patch([f"data = '{blob}'"])
        findings = scan_patch(patch)
        # Should NOT trigger high_entropy_string since no secret-ish context
        assert not any(f.pattern_name == "high_entropy_string" for f in findings)


# ---------------------------------------------------------------------------
# Generic token
# ---------------------------------------------------------------------------


class TestGenericToken:
    @pytest.mark.parametrize(
        "line",
        [
            "access_token = create_access_token_for_user(user)",
            "password = generate_random_password(16)",
            "token = jwt_encode_with_expiry(payload)",
            "self.api_key = api_key_from_environment_variable",
            'password = os.environ["DATABASE_PASSWORD_FOR_TESTS"]',
            'token = f"{settings.TOKEN_PREFIX_VALUE_LONG}"',
        ],
    )
    def test_code_is_not_a_secret(self, line: str) -> None:
        assert scan_patch(_make_patch([line])) == []

    @pytest.mark.parametrize(
        ("line", "name"),
        [
            ('api_key = "AbCdEfGhIjKlMnOpQrStUvWx"', "generic_api_key"),
            ('password = "AbCdEfGhIjKlMnOpQrStUvWx"', "generic_token"),
            ('{"password": "AbCdEfGhIjKlMnOpQrStUvWx"}', "generic_token"),
        ],
    )
    def test_quoted_literal_is_flagged(self, line: str, name: str) -> None:
        findings = scan_patch(_make_patch([line]))
        assert [(f.pattern_name, f.severity) for f in findings] == [(name, "high")]

    def test_unquoted_value_is_a_secret_only_in_config_files(self) -> None:
        line = "API_KEY=AbCdEfGhIjKlMnOpQrStUvWx"
        files = {".env": line, "deploy/app.yaml": "password: " + "Zz9" * 8}
        findings = scan_patch({"files": files})
        assert {(f.file, f.pattern_name) for f in findings} == {
            (".env", "generic_api_key"),
            ("deploy/app.yaml", "generic_token"),
        }
        assert scan_patch({"files": {"app.py": line}}) == []

    def test_detects_token_assignment(self) -> None:
        patch = _make_patch(["token = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9abc'"])
        findings = scan_patch(patch)
        assert any(f.pattern_name == "generic_token" for f in findings)

    def test_detects_password_assignment(self) -> None:
        patch = _make_patch(["password = 'SuperSecretPassword123!'"])
        findings = scan_patch(patch)
        assert any(f.pattern_name == "generic_token" for f in findings)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_line_content_is_redacted(self) -> None:
        patch = _make_patch(["AKIAIOSFODNN7EXAMPLE1 = something very secret here"])
        findings = scan_patch(patch)
        assert len(findings) == 1
        assert "****REDACTED****" in findings[0].line_content
        # The original secret should not appear in full
        assert "EXAMPLE1 = something very secret here" not in findings[0].line_content

    def test_only_four_characters_of_the_secret_are_kept(self) -> None:
        findings = scan_patch(_make_patch(['    api_key = "AbCdEfGhIjKlMnOpQrStUvWx"']))
        assert findings[0].line_content == 'api_key = "AbCd****REDACTED****'


# ---------------------------------------------------------------------------
# has_secrets convenience
# ---------------------------------------------------------------------------


class TestHasSecrets:
    def test_returns_true_when_secrets_found(self) -> None:
        patch = _make_patch(["-----BEGIN RSA PRIVATE KEY-----"])
        assert has_secrets(patch) is True

    def test_returns_false_when_clean(self) -> None:
        patch = _make_patch(["x = 1", "print('hello')"])
        assert has_secrets(patch) is False


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


class TestAllowlist:
    def test_allowlist_suppresses_finding(self) -> None:
        patch = _make_patch(["AKIAIOSFODNN7EXAMPLE1 = test fixture key"])
        # Without allowlist — detected
        assert has_secrets(patch) is True
        # With allowlist matching the key
        assert has_secrets(patch, allowlist=[r"AKIAIOSFODNN7EXAMPLE1"]) is False

    def test_allowlist_only_suppresses_matching_lines(self) -> None:
        patch = _make_patch(
            [
                "AKIAIOSFODNN7EXAMPLE1 = fixture",
                "-----BEGIN RSA PRIVATE KEY-----",
            ]
        )
        findings = scan_patch(patch, allowlist=[r"AKIAIOSFODNN7EXAMPLE1"])
        assert len(findings) == 1
        assert findings[0].pattern_name == "private_key"

    def test_invalid_allowlist_pattern_is_skipped(self) -> None:
        patch = _make_patch(["-----BEGIN RSA PRIVATE KEY-----"])
        # Invalid regex should not crash, just warn
        findings = scan_patch(patch, allowlist=[r"[invalid"])
        assert len(findings) == 1

    def test_allowlist_with_regex(self) -> None:
        patch = _make_patch(["api_key = 'test_fixture_key_00000000'"])
        assert has_secrets(patch) is True
        assert has_secrets(patch, allowlist=[r"test_fixture_key_\d+"]) is False


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_patch(self) -> None:
        assert scan_patch({"diff_unified": ""}) == []

    def test_patch_with_no_additions(self) -> None:
        diff = "--- a/f.py\n+++ b/f.py\n@@ -1 +1 @@\n-old\n"
        assert scan_patch({"diff_unified": diff}) == []

    def test_files_dict_is_scanned(self) -> None:
        patch = {
            "diff_unified": "",
            "files": {"config.py": "-----BEGIN RSA PRIVATE KEY-----\ndata"},
        }
        findings = scan_patch(patch)
        assert len(findings) == 1
        assert findings[0].pattern_name == "private_key"
        assert findings[0].file == "config.py"

    def test_diff_header_lines_not_scanned(self) -> None:
        # +++ lines should not be treated as additions
        diff = "+++ b/AKIAIOSFODNN7EXAMPLE1.py\n"
        assert scan_patch({"diff_unified": diff}) == []

    def test_findings_are_sorted_by_line_number(self) -> None:
        patch = _make_patch(
            [
                "normal line",
                "-----BEGIN RSA PRIVATE KEY-----",
                "normal",
                "AKIAIOSFODNN7EXAMPLE1",
            ]
        )
        findings = scan_patch(patch)
        assert len(findings) == 2
        assert findings[0].line_number < findings[1].line_number


# ---------------------------------------------------------------------------
# Location and scan scope
# ---------------------------------------------------------------------------


class TestLocation:
    def test_finding_has_file_and_per_file_line(self) -> None:
        secret = 'redis_url = "redis://:pw@cache.example.com"'
        findings = scan_patch({"files": {"a.py": "x=1\ny=2", "b.py": secret}})
        assert [(f.file, f.line_number, f.pattern_name) for f in findings] == [
            ("b.py", 1, "connection_string")
        ]

    def test_diff_findings_use_hunk_line_numbers(self) -> None:
        diff = "+++ b/s.py\n@@ -7,1 +7,2 @@\n ctx\n+-----BEGIN RSA PRIVATE KEY-----\n"
        findings = scan_patch({"diff_unified": diff})
        assert [(f.file, f.line_number) for f in findings] == [("s.py", 8)]


class TestExistingLinesAreNotScanned:
    SECRET = 'DATABASE_URL = "postgresql://app:S3cr3tPw@db.prod.example.com/app"'

    def test_existing_secret_line_is_ignored(self, tmp_path: Path) -> None:
        (tmp_path / "config.py").write_text(f"{self.SECRET}\nDEBUG = False\n", encoding="utf-8")
        patch = {"files": {"config.py": f"{self.SECRET}\nDEBUG = False\nTIMEOUT = 30\n"}}
        assert scan_patch(patch, added=added_lines(patch, tmp_path)) == []

    def test_added_secret_line_is_reported_once(self, tmp_path: Path) -> None:
        (tmp_path / "config.py").write_text("DEBUG = False\n", encoding="utf-8")
        patch = {"files": {"config.py": f"DEBUG = False\n{self.SECRET}\n"}}
        findings = scan_patch(patch, added=added_lines(patch, tmp_path))
        assert [(f.file, f.line_number, f.pattern_name) for f in findings] == [
            ("config.py", 2, "connection_string")
        ]
