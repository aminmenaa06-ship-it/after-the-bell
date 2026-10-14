#!/usr/bin/env python3
"""Decide which edition a run should produce, from the NYSE calendar and the time in New York.

Prints "<mode> <session_date>":
  premarket 2026-10-14   a trading day before the 4 PM close (late morning runs still count as
                         that day's pre-market edition; the page shows when it was published)
  close     2026-10-14   a trading day after the 4 PM ET close
  closed    2026-10-17   weekend or exchange holiday
"""
from datetime import datetime, time
from zoneinfo import ZoneInfo

import exchange_calendars as xc

now = datetime.now(ZoneInfo("America/New_York"))
day = now.date().isoformat()
cal = xc.get_calendar("XNYS")
if not cal.is_session(day):
    print("closed", day)
else:
    close_t = cal.session_close(day).tz_convert("America/New_York").time()  # 1 PM on early-close days
    print("close" if now.time() >= close_t else "premarket", day)
