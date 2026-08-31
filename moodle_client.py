import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
import httpx

MSK_TZ = timezone(timedelta(hours=3))

@dataclass
class DeadlineEvent:
    event_id: str
    name: str
    course_name: str
    due_timestamp: int
    due_datetime: datetime
    url: str
    description: str = ""
    is_opening: bool = False

    @property
    def formatted_date(self) -> str:
        return self.due_datetime.strftime("%d.%m.%Y в %H:%M МСК")

    @property
    def clean_name(self) -> str:
        """Очищенное название задания без технических суффиксов"""
        clean = self.name
        for suffix in [" - срок сдачи", " срок сдачи", " is due", " закрывается", " открывается", " opens"]:
            clean = clean.replace(suffix, "")
        return clean.strip()

    @property
    def time_left_str(self) -> str:
        now = datetime.now(MSK_TZ)
        diff = self.due_datetime - now
        if diff.total_seconds() <= 0:
            return "Срок истек" if not self.is_opening else "Уже открыт"
        
        days = diff.days
        hours, remainder = divmod(diff.seconds, 3600)
        minutes, _ = divmod(remainder, 60)

        parts = []
        if days > 0:
            parts.append(f"{days} дн.")
        if hours > 0:
            parts.append(f"{hours} ч.")
        if minutes > 0 and days == 0:
            parts.append(f"{minutes} мин.")
        
        prefix = "Открытие через: " if self.is_opening else "Осталось: "
        return prefix + (" ".join(parts) if parts else "< 1 мин.")


def parse_ical_datetime(dt_str: str) -> Optional[datetime]:
    dt_str = dt_str.strip()
    try:
        if "T" in dt_str:
            clean_str = dt_str.replace("Z", "")
            dt = datetime.strptime(clean_str, "%Y%m%dT%H%M%S")
            if dt_str.endswith("Z"):
                dt = dt.replace(tzinfo=timezone.utc).astimezone(MSK_TZ)
            else:
                dt = dt.replace(tzinfo=MSK_TZ)
            return dt
        else:
            dt = datetime.strptime(dt_str, "%Y%m%d")
            return dt.replace(tzinfo=MSK_TZ)
    except Exception:
        return None


def parse_ical_content(ical_text: str) -> List[DeadlineEvent]:
    events: List[DeadlineEvent] = []
    raw_events = re.findall(r'BEGIN:VEVENT(.*?)END:VEVENT', ical_text, re.DOTALL)
    
    for block in raw_events:
        fields = {}
        unfolded = re.sub(r'\r?\n[ \t]', '', block)
        
        for line in unfolded.strip().splitlines():
            if ":" in line:
                key_part, val_part = line.split(":", 1)
                key = key_part.split(";")[0].strip().upper()
                fields[key] = val_part.strip()

        summary = fields.get("SUMMARY", "Без названия")
        summary = summary.replace(r'\,', ',').replace(r'\;', ';').replace(r'\n', '\n')
        
        uid = fields.get("UID", str(int(time.time())))
        event_num_match = re.match(r'^(\d+)', uid)
        event_num = event_num_match.group(1) if event_num_match else ""
        default_url = f"https://online-edu.mirea.ru/calendar/view.php?view=event&id={event_num}" if event_num else "https://online-edu.mirea.ru"
        url = fields.get("URL") or default_url
        
        categories = fields.get("CATEGORIES", "")
        description = fields.get("DESCRIPTION", "").replace(r'\,', ',').replace(r'\n', '\n')
        
        dt_raw = fields.get("DTEND") or fields.get("DTSTART") or ""
        dt = parse_ical_datetime(dt_raw)
        
        if not dt:
            continue

        course_name = categories if categories else "СДО МИРЭА"
        
        # Определяем, является ли это событием открытия (теста/задания)
        summary_lower = summary.lower()
        is_opening = any(w in summary_lower for w in ["открывается", "opens", "доступен с", "начало"])

        events.append(DeadlineEvent(
            event_id=uid,
            name=summary,
            course_name=course_name,
            due_timestamp=int(dt.timestamp()),
            due_datetime=dt,
            url=url,
            description=description,
            is_opening=is_opening
        ))

    events.sort(key=lambda x: x.due_timestamp)
    return events


class MoodleClient:
    def __init__(self, base_url: str = "https://online-edu.mirea.ru", token: Optional[str] = None, calendar_url: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.calendar_url = calendar_url

    async def get_upcoming_deadlines(self, limit: int = 50) -> List[DeadlineEvent]:
        if self.calendar_url:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                resp = await client.get(self.calendar_url)
                resp.raise_for_status()
                all_events = parse_ical_content(resp.text)
                
                # Оставляем события, которые завершаются не ранее чем 2 часа назад
                current_ts = int(time.time()) - 7200
                upcoming = [e for e in all_events if e.due_timestamp >= current_ts]
                return upcoming[:limit]

        if self.token:
            endpoint = f"{self.base_url}/webservice/rest/server.php"
            current_ts = int(time.time())
            params = {
                "wstoken": self.token,
                "wsfunction": "core_calendar_get_action_events_by_timesort",
                "moodlewsrestformat": "json",
                "timesortfrom": current_ts,
                "limitnum": limit
            }
            async with httpx.AsyncClient(timeout=20.0) as client:
                resp = await client.get(endpoint, params=params)
                resp.raise_for_status()
                data = resp.json()
                
                events_raw = data.get("events", [])
                deadlines: List[DeadlineEvent] = []
                for item in events_raw:
                    ts = item.get("timestart") or item.get("timesort") or 0
                    if ts <= 0:
                        continue
                    dt = datetime.fromtimestamp(ts, tz=MSK_TZ)
                    course_info = item.get("course") or {}
                    course_name = course_info.get("fullname") or "Без курса"
                    name = item.get("name", "Без названия")
                    is_opening = "открывается" in name.lower()
                    deadlines.append(DeadlineEvent(
                        event_id=str(item.get("id", 0)),
                        name=name,
                        course_name=course_name,
                        due_timestamp=ts,
                        due_datetime=dt,
                        url=item.get("url", self.base_url),
                        is_opening=is_opening
                    ))
                deadlines.sort(key=lambda x: x.due_timestamp)
                return deadlines

        raise ValueError("Не задан ни CALENDAR_URL, ни MOODLE_TOKEN в файле .env")
