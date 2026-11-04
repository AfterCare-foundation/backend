import pytest

from app.push_tokens import is_valid_push_token

HEX64 = "ab12cd34" * 8
FCM_REALISTIC = "cXyZ123:APA91b" + "Ab9_-" * 28

IOS_ACCEPT = [
    HEX64,
    HEX64.upper(),
    "aB12Cd34" * 8,
    "ab" * 100,
]

IOS_REFUSE = [
    "a" * 63,
    "a" * 65,
    "a" * 62,
    "a" * 202,
    "",
    "g" + "a" * 63,
    "../3/device/x?y=1",
    HEX64 + "/",
    HEX64 + "?",
    HEX64 + "#",
    HEX64 + "%2F",
    HEX64 + " ",
    HEX64 + "\n",
    " " + HEX64,
    HEX64[:30] + "\x00" + HEX64[31:],
    "٣" * 64,
    "a" * 32 + "٣" * 32,
    "ａ" * 64,
    "a" * 1_000_000,
]

ANDROID_ACCEPT = [
    FCM_REALISTIC,
    "a" * 32,
    "a" * 4096,
    "A-z_0:9." * 8,
]

ANDROID_REFUSE = [
    "a" * 31,
    "a" * 4097,
    "a" * 40 + "/",
    "a" * 40 + "?",
    "a" * 40 + " ",
    "a" * 40 + "\n",
    "a" * 40 + "+",
    "a" * 40 + "=",
    "a" * 40 + "é",
    "a" * 40 + "\x00",
    "",
]


def test_realistic_fcm_fixture_length():
    assert 32 <= len(FCM_REALISTIC) <= 4096


def _ids(tokens):
    # Long tokens as raw ids overflow pytest's per-test environment variable
    # on Windows.
    return [f"{i}-len{len(t)}" for i, t in enumerate(tokens)]


@pytest.mark.parametrize("token", IOS_ACCEPT, ids=_ids(IOS_ACCEPT))
def test_ios_accepts(token):
    assert is_valid_push_token("ios", token) is True


@pytest.mark.parametrize("token", IOS_REFUSE, ids=_ids(IOS_REFUSE))
def test_ios_refuses(token):
    assert is_valid_push_token("ios", token) is False


@pytest.mark.parametrize("token", ANDROID_ACCEPT, ids=_ids(ANDROID_ACCEPT))
def test_android_accepts(token):
    assert is_valid_push_token("android", token) is True


@pytest.mark.parametrize("token", ANDROID_REFUSE, ids=_ids(ANDROID_REFUSE))
def test_android_refuses(token):
    assert is_valid_push_token("android", token) is False


def test_fcm_token_is_not_an_ios_token():
    assert is_valid_push_token("ios", FCM_REALISTIC) is False


def test_apns_hex_fits_android_charset():
    # Documented consequence of the rules: 64 hex chars satisfy the FCM
    # charset and length, so the platform field is not cross-checked.
    assert is_valid_push_token("android", HEX64) is True


@pytest.mark.parametrize("platform", ["", "web", "IOS", "Android", "ios ", None])
def test_unknown_platform_refused(platform):
    assert is_valid_push_token(platform, HEX64) is False


@pytest.mark.parametrize("token", [None, 123, b"ab" * 32, ["x"]], ids=["none", "int", "bytes", "list"])
@pytest.mark.parametrize("platform", ["ios", "android"])
def test_non_str_token_refused(platform, token):
    assert is_valid_push_token(platform, token) is False
