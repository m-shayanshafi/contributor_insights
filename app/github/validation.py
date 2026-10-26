"""Input validation for values that end up inside GitHub search strings."""

import re

# GitHub's own login rules: letters, digits and single hyphens, no leading hyphen,
# at most 39 characters. Spaces, colons and quotes are impossible, so a login can't
# smuggle extra search qualifiers such as "x repo:other/repo".
LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


class InvalidLogin(ValueError):
    pass


def validate_login(login: str) -> str:
    if not isinstance(login, str) or not LOGIN_PATTERN.fullmatch(login):
        raise InvalidLogin("login must be 1-39 letters, digits or hyphens")
    return login
