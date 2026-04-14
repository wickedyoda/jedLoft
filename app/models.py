from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import relationship

from .database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    role = Column(String(32), nullable=False, default="read_only")
    is_approved = Column(Boolean, nullable=False, default=False)
    password_hash = Column(String(255), nullable=False)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    password_history = relationship(
        "PasswordHistory",
        back_populates="user",
        cascade="all, delete-orphan",
        order_by="desc(PasswordHistory.created_at)",
    )


class PasswordHistory(Base):
    __tablename__ = "password_history"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", back_populates="password_history")


class Bird(Base):
    __tablename__ = "birds"

    id = Column(Integer, primary_key=True, index=True)
    bird_type = Column(String(120), nullable=False)
    sex = Column(String(20), nullable=False)
    band_number = Column(String(120), unique=True, index=True, nullable=True)
    birth_date = Column(Date, nullable=True)
    birth_place = Column(String(255), nullable=True)
    foreign_loft_owner_name = Column(String(255), nullable=True)
    pedigree = Column(String(255), nullable=True)
    bloodline = Column(String(255), nullable=True)
    special_colors = Column(String(255), nullable=True)
    features_markings = Column(String(255), nullable=True)
    family_tree_notes = Column(String(1500), nullable=True)
    mate_band_number = Column(String(120), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
