"""FastAPI dependencies for the logged-in user and role checks (enforced on the backend)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from tally_automation.auth.service import CurrentUser
from tally_automation.db.models import UserRole

ROLE_LABELS = {
    UserRole.VIEWER: "viewer", UserRole.REVIEWER: "reviewer", UserRole.APPROVER: "approver", UserRole.ADMIN: "administrator",
}


class NotAuthenticated(Exception):
    pass


class Forbidden(Exception):
    def __init__(self, needed: UserRole) -> None:
        super().__init__(needed.value)
        self.needed = needed

    @property
    def message(self) -> str:
        return f"This needs the {ROLE_LABELS[self.needed]} role (or higher). Ask an administrator."


def current_user(request: Request) -> CurrentUser:
    user = getattr(request.state, "user", None)
    if user is None:          # the middleware normally answers 401 before this
        raise NotAuthenticated()
    return user


CurrentUserDep = Annotated[CurrentUser, Depends(current_user)]


def require(role: UserRole):
    def check(user: CurrentUserDep) -> CurrentUser:
        if not user.can(role):
            raise Forbidden(role)
        return user

    return Depends(check)


Reviewer = Annotated[CurrentUser, require(UserRole.REVIEWER)]
Approver = Annotated[CurrentUser, require(UserRole.APPROVER)]
