"""Who can see what.

- An account is visible to its owner and to users it has been shared with.
- A user's view scope narrows the accounts used in analytics and exports.
  "own_shared" and "all" currently select the same accounts, because nothing is
  visible to a user other than their own and shared accounts. See README.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models import Account, AccountShare, User

VIEW_SCOPES = ("own_shared", "all")


def shared_account_ids(user_id: int):
    return select(AccountShare.account_id).where(AccountShare.user_id == user_id)


def accessible_accounts(session: Session, user: User) -> list[Account]:
    stmt = select(Account).where(
        or_(Account.owner_id == user.id, Account.id.in_(shared_account_ids(user.id)))
    ).order_by(Account.id)
    return list(session.scalars(stmt))


def can_access(session: Session, user: User, account: Account) -> bool:
    if account.owner_id == user.id:
        return True
    return session.get(AccountShare, (account.id, user.id)) is not None


def get_or_create_user(session: Session, username: str) -> User:
    user = session.query(User).filter_by(username=username).first()
    if user is None:
        user = User(username=username)
        session.add(user)
        session.commit()
    return user
