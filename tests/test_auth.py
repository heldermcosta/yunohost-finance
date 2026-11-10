import base64

from app.auth import username_from_authorization


def basic(user: str, password: str = "-") -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def test_reads_username_from_sso_basic_header():
    assert username_from_authorization(basic("helder")) == "helder"


def test_password_is_ignored():
    assert username_from_authorization(basic("elsa", "anything")) == "elsa"


def test_missing_or_other_schemes_give_no_user():
    assert username_from_authorization(None) is None
    assert username_from_authorization("Bearer abc") is None
    assert username_from_authorization("Basic !!!notbase64") is None


def test_empty_username_is_rejected():
    assert username_from_authorization(basic("")) is None
