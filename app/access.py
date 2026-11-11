"""Who can see and change what.

- Owner: full control of an account (edit, share, accept or delete its statements).
- Shared user: sees the account and can correct transaction categories, but cannot accept
  or delete statements or share it.
- Household accounts: visible to users whose view scope is "all", even if not shared.
- View scope narrows what analytics and listings show:
  "own_shared": own and shared accounts. "all": those plus household accounts.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models import Account, AccountShare, User

VIEW_SCOPES = ("own_shared", "all")


def shared_account_ids(user_id: int):
    return select(AccountShare.account_id).where(AccountShare.user_id == user_id)


def can_access(session: Session, user: User, account: Account) -> bool:
    """Owner or shared: may read the account and correct its categories."""
    if account.owner_id == user.id:
        return True
    return session.get(AccountShare, (account.id, user.id)) is not None


def is_owner(user: User, account: Account) -> bool:
    return account.owner_id == user.id


def visible_accounts(session: Session, user: User) -> list[Account]:
    """What this user sees, given their view scope."""
    conditions = [Account.owner_id == user.id, Account.id.in_(shared_account_ids(user.id))]
    if user.view_scope == "all":
        conditions.append(Account.household.is_(True))
    return list(session.scalars(select(Account).where(or_(*conditions)).order_by(Account.id)))


def can_see(session: Session, user: User, account: Account) -> bool:
    return any(a.id == account.id for a in visible_accounts(session, user))


def get_or_create_user(session: Session, username: str) -> User:
    user = session.query(User).filter_by(username=username).first()
    if user is None:
        user = User(username=username)
        session.add(user)
        session.commit()
    return user
