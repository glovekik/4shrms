from pydantic import BaseModel
from typing import Optional


class ProjectMeetingCreate(BaseModel):
    """A meeting held on a project, and what was said in it.

    Notes are the point. The scheduling side deliberately stays thin — a
    title, when it happened, who was there — because the app already has a
    recruitment-interview scheduler and a second half-built one would be a
    place for meetings to get lost rather than found.

    `decisions` and `actionItems` are kept apart from the body because they
    are what people come back for. A decision buried in a paragraph three
    weeks old may as well not have been recorded.
    """
    title: str
    # YYYY-MM-DD. Defaults to today when omitted, so writing up a meeting you
    # just left takes one field.
    date: Optional[str] = None
    # HH:MM, optional — plenty of notes are written up without one.
    time: Optional[str] = None
    attendeeIds: Optional[list[str]] = None
    # Someone who isn't in the system: a client, a vendor.
    externalAttendees: Optional[str] = None
    notes: Optional[str] = None
    decisions: Optional[str] = None
    actionItems: Optional[str] = None
    phaseId: Optional[str] = None


class ProjectMeetingUpdate(BaseModel):
    title: Optional[str] = None
    date: Optional[str] = None
    time: Optional[str] = None
    attendeeIds: Optional[list[str]] = None
    externalAttendees: Optional[str] = None
    notes: Optional[str] = None
    decisions: Optional[str] = None
    actionItems: Optional[str] = None
    phaseId: Optional[str] = None
