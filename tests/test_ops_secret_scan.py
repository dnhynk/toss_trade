"""ops/hooks/pre_commit_secret_scan.py 테스트 — 소유: W5.

git 스테이징 상태에 의존하지 않도록 순수 함수(find_secrets/is_blocked_filename)만
단위 테스트한다. 실제 값이 아니라 명백히 가짜인 패턴만 사용한다(계약 C-11 §5).
"""
from __future__ import annotations

from ops.hooks.pre_commit_secret_scan import find_secrets, is_blocked_filename


def test_blocked_filenames():
    assert is_blocked_filename("api_keys") is True
    assert is_blocked_filename("data/token_state.json") is True
    assert is_blocked_filename("nested/dir/api_keys") is True
    assert is_blocked_filename(".env") is True
    assert is_blocked_filename(".env.local") is True
    assert is_blocked_filename("secret.pem") is True
    assert is_blocked_filename("id.key") is True


def test_allowed_filenames_pass():
    assert is_blocked_filename("tossmon/api/tokens.py") is False
    assert is_blocked_filename("config/config.example.yaml") is False
    assert is_blocked_filename("docs/09_secret_hygiene.md") is False


def test_find_secrets_detects_client_secret_assignment():
    text = 'client_secret = "FAKE1234567890abcdefFAKE"\n'
    hits = find_secrets(text, path="somefile.py")
    assert hits


def test_find_secrets_detects_dotenv_style():
    text = "CLIENT_ID=fake-id-123456\nCLIENT_SECRET=fakeSecretValue1234567890\n"
    hits = find_secrets(text, path="notes.txt")
    assert len(hits) >= 2


def test_find_secrets_detects_bearer_token():
    text = "Authorization: Bearer fakeAccessToken1234567890.fake\n"
    hits = find_secrets(text, path="log.txt")
    assert hits


def test_find_secrets_detects_aws_style_key():
    text = "AKIAFAKEEXAMPLE1234A is not a real key\n"
    hits = find_secrets(text, path="notes.txt")
    assert hits


def test_find_secrets_ignores_plain_code():
    text = (
        "def get_prices(symbols):\n"
        "    return [Price(symbol=s, ts_ms=None, last_u=0) for s in symbols]\n"
    )
    assert find_secrets(text, path="tossmon/api/client.py") == []


def test_find_secrets_exempts_self():
    # 패턴 정의 파일 자신은 예외 처리 — 정의된 정규식 리터럴이 스스로를 오검출하지 않게.
    from pathlib import Path

    self_path = Path("ops/hooks/pre_commit_secret_scan.py")
    text = self_path.read_text(encoding="utf-8")
    assert find_secrets(text, path=str(self_path)) == []
