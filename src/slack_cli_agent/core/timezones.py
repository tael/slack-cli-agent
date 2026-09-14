"""Single source for the timezone used in all user-facing timestamps."""

from __future__ import annotations

from datetime import timedelta, timezone

KST = timezone(timedelta(hours=9))
