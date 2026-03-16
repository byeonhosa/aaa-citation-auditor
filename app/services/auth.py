"""Authentication helpers: password hashing and user management."""

from __future__ import annotations

import logging

import bcrypt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aaa_db.models import User

logger = logging.getLogger(__name__)


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == email.lower()))


def create_user(db: Session, *, email: str, password: str, name: str) -> User:
    user = User(
        email=email.lower(),
        password_hash=hash_password(password),
        name=name,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("New user registered: %s", user.email)
    return user


def authenticate_user(db: Session, email: str, password: str) -> User | None:
    user = get_user_by_email(db, email)
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def users_exist(db: Session) -> bool:
    count = db.scalar(select(func.count(User.id))) or 0
    return count > 0
