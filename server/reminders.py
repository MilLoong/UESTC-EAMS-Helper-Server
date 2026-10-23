import time
import secrets

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field


class Course(BaseModel):
    id: str = Field(min_length=1, max_length=300)
    course_name: str = Field(min_length=1, max_length=120)
    room: str = Field(default='', max_length=120)
    start_at: int


class Plan(BaseModel):
    lead_minutes: int = Field(default=20, ge=1, le=1440)
    courses: list[Course] = Field(max_length=200)


def install_routes(app, db, device, admin):
    with db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS reminders(
            device TEXT NOT NULL, id TEXT NOT NULL, course_name TEXT NOT NULL,
            room TEXT NOT NULL, start_at INTEGER NOT NULL, remind_at INTEGER NOT NULL,
            PRIMARY KEY(device,id))''')

    @app.put('/devices/me/courses')
    def replace_courses(plan: Plan, device_id=Depends(device)):
        now = int(time.time())
        if any(not now < course.start_at <= now + 32 * 86400 for course in plan.courses):
            raise HTTPException(422, 'Course start must be within the next 32 days')
        if len({course.id for course in plan.courses}) != len(plan.courses):
            raise HTTPException(422, 'Duplicate course ids')
        with db() as c:
            c.execute('DELETE FROM reminders WHERE device=?', (device_id,))
            c.executemany('INSERT INTO reminders VALUES(?,?,?,?,?,?)', [
                (device_id, course.id, course.course_name, course.room, course.start_at,
                 course.start_at - plan.lead_minutes * 60) for course in plan.courses
            ])
        return {'saved': len(plan.courses)}

    @app.get('/devices/me/reminders')
    def reminders(device_id=Depends(device)):
        with db() as c:
            rows = c.execute('SELECT id,course_name,room,start_at,remind_at FROM reminders '
                             'WHERE device=? AND start_at>? ORDER BY remind_at,id LIMIT 200',
                             (device_id, int(time.time()))).fetchall()
        return {'server_time': int(time.time()), 'reminders': [dict(row) for row in rows]}

    @app.delete('/devices/me/reminders')
    def clear(device_id=Depends(device)):
        with db() as c:
            c.execute('DELETE FROM reminders WHERE device=?', (device_id,))
        return {'cleared': True}

    @app.post('/admin/devices/{device_id}/test-reminder', dependencies=[Depends(admin)])
    def test_reminder(device_id: str):
        now = int(time.time())
        with db() as c:
            if not c.execute('SELECT id FROM devices WHERE id=? AND enabled=1', (device_id,)).fetchone():
                raise HTTPException(404)
            c.execute('INSERT OR REPLACE INTO reminders VALUES(?,?,?,?,?,?)',
                      (device_id, 'server-test-' + secrets.token_hex(8), 'Server reminder test', 'Test room', now + 120, now + 30))
        return {'remind_at': now + 30, 'instruction': 'No App sync required. Keep the server running and lock the phone.'}
