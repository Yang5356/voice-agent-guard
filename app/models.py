from sqlalchemy import Column, Integer, String, Text
from .db import Base


class VisitorRecord(Base):
    __tablename__ = "visitor_records"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String, nullable=False)
    plate_number = Column(String, nullable=True)
    target = Column(String, nullable=True)
    reason = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    entry_time = Column(String, nullable=True)
    status = Column(String, nullable=False, default="incomplete")
    transcript = Column(Text, nullable=True)
    missing_fields = Column(Text, nullable=True)
    is_returning_visitor = Column(Integer, nullable=False, default=0)
    matched_history_id = Column(Integer, nullable=True)