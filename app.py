import os
from datetime import datetime, date, time, timedelta
from dataclasses import dataclass

from flask import Flask, request, make_response, render_template_string, abort, url_for
from sqlalchemy import (create_engine, Column, Integer, String, DateTime, Boolean,
                        ForeignKey, UniqueConstraint)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, scoped_session
from pytz import timezone

# -------------------- Ayarlar --------------------
TZ_NAME = os.getenv("TIMEZONE", "Europe/Istanbul")
TZ = timezone(TZ_NAME)
DB_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DB_DIR, exist_ok=True)
DB_URL = f"sqlite:///{os.path.join(DB_DIR, 'salon.db')}"

# SADE: sadece "Cilt bakımı" hizmetini ekledik
SERVICES = [
    "Lazer epilasyon", "Kirpik", "Tırnak", "Manikür", "Pedikür",
    "Nail art", "Sigara bırakma", "İştah kapatma", "Botox",
    "Dolgu", "Dövme silme", "Kaş", "Cilt bakımı"
]

EMPLOYEE_NAMES = ["Merve", "Zeynep", "İrem", "X"]
SLOT_MINUTES = 60
START_TIME = time(9, 0)
END_TIME   = time(19, 30)

# -------------------- DB --------------------
Base = declarative_base()
engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
Session = scoped_session(sessionmaker(bind=engine, expire_on_commit=False))

class Employee(Base):
    __tablename__ = "employees"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    appointments = relationship("Appointment", back_populates="employee")

class Appointment(Base):
    __tablename__ = "appointments"
    id = Column(Integer, primary_key=True)
    employee_id = Column(Integer, ForeignKey("employees.id"), nullable=False)
    customer_name = Column(String, nullable=False)
    phone = Column(String, nullable=True)
    service = Column(String, nullable=False)
    start_time = Column(DateTime, nullable=False)
    end_time = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(TZ))
    reminder_sent = Column(Boolean, default=False)

    employee = relationship("Employee", back_populates="appointments")
    __table_args__ = (UniqueConstraint("employee_id", "start_time", name="uq_employee_slot"),)

Base.metadata.create_all(engine)

# Seed employees (varsa atlar)
with Session() as s:
    existing = {e.name for e in s.query(Employee).all()}
    for n in EMPLOYEE_NAMES:
        if n not in existing:
            s.add(Employee(name=n))
    s.commit()

# -------------------- Flask --------------------
app = Flask(__name__)

@app.get("/healthz")
def healthz():
    return "ok", 200

# -------------------- Yardımcılar --------------------
TR_DAYS = ["Pazartesi","Salı","Çarşamba","Perşembe","Cuma","Cumartesi","Pazar"]

@dataclass
class Slot:
    dt: datetime
    label: str

def week_start_for(d: date) -> date:
    return d - timedelta(days=d.weekday())

def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def iter_slots_for_day(day: date) -> list[Slot]:
    if day.weekday() == 6:  # Pazar
        return []
    slots, cur = [], datetime.combine(day, START_TIME)
    end_dt = datetime.combine(day, END_TIME)
    while cur + timedelta(minutes=SLOT_MINUTES) <= end_dt:
        slots.append(Slot(dt=TZ.localize(cur), label=cur.strftime("%H:%M")))
        cur += timedelta(minutes=SLOT_MINUTES)
    # 18:30 gerekiyorsa
    special = datetime.combine(day, time(18,30))
    if special + timedelta(minutes=60) <= end_dt and all(s.label!="18:30" for s in slots):
        slots.append(Slot(dt=TZ.localize(special), label="18:30"))
    return slots

def week_days(start: date) -> list[date]:
    return [start + timedelta(days=i) for i in range(7)]

def fetch_week_appointments(ses, week_days_list: list[date]):
    start_dt = TZ.localize(datetime.combine(week_days_list[0], time(0,0)))
    end_dt   = TZ.localize(datetime.combine(week_days_list[-1], time(23,59)))
    return ses.query(Appointment).filter(Appointment.start_time >= start_dt,
                                         Appointment.start_time <= end_dt).all()

def appt_key(emp_id: int, dt: datetime) -> tuple:
    return (emp_id, dt.strftime("%Y-%m-%d %H:%M"))

# -------------------- Rotalar --------------------
@app.route("/")
def index():
    with Session() as s:
        today = datetime.now(TZ).date()
        ws_param = request.args.get("week_start")
        ws = parse_date(ws_param) if ws_param else week_start_for(today)
        days = week_days(ws)
        employees = s.query(Employee).order_by(Employee.id).all()

        # Takvim KALDIRILDI; sadece bilgiler gösterilecek
        prev_w = (ws - timedelta(days=7)).strftime("%Y-%m-%d")
        next_w = (ws + timedelta(days=7)).strftime("%Y-%m-%d")
        this_w = week_start_for(today).strftime("%Y-%m-%d")

        html = render_template_string(INDEX_HTML,
                                      ws=ws,
                                      TR_DAYS=TR_DAYS,
                                      days=days,
                                      employees=employees,
                                      prev_w=prev_w,
                                      next_w=next_w,
                                      this_w=this_w,
                                      services=SERVICES)
        resp = make_response(html)
        # Tarayıcı ve ara katman önbelleklerini devre dışı bırak
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
        return resp

# Eski takvim aksiyonlarını tamamen kapatıyoruz
@app.get("/slot")
def slot_modal():
    return abort(404)

@app.post("/appointments")
def create_appointment():
    return abort(404)

@app.post("/appointments/<int:appt_id>/delete")
def delete_appointment(appt_id: int):
    return abort(404)

# -------------------- Şablon (takvim YOK) --------------------
INDEX_HTML = r"""
<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Randevu Takvimi</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-white text-gray-900 min-h-screen">
  <header class="bg-gradient-to-r from-rose-500 via-fuchsia-500 to-violet-500 text-white">
    <div class="max-w-6xl mx-auto px-4 py-5 flex flex-col sm:flex-row sm:items-end sm:justify-between gap-3">
      <div>
        <h1 class="text-2xl sm:text-3xl font-extrabold tracking-tight drop-shadow">Güzellik Merkezi Randevu Sistemi</h1>
        <p class="text-xs sm:text-sm text-white/90 mt-1">
          Haftalık görünüm • Dolu: <span class="inline-block w-3 h-3 bg-red-500 rounded-sm align-middle"></span>
          • Boş: <span class="inline-block w-3 h-3 bg-white/90 border border-white/50 rounded-sm align-middle"></span>
        </p>
      </div>
      <nav class="flex items-center gap-2">
        <a class="px-3 py-2 rounded-xl bg-white/15 hover:bg-white/25 border border-white/20 backdrop-blur transition" href="?week_start={{ prev_w }}">◀ Önceki</a>
        <a class="px-3 py-2 rounded-xl bg-white text-rose-700 font-semibold hover:bg-rose-50 border border-white/0 transition" href="?week_start={{ this_w }}">Bugün</a>
        <a class="px-3 py-2 rounded-xl bg-white/15 hover:bg-white/25 border border-white/20 backdrop-blur transition" href="?week_start={{ next_w }}">Sonraki ▶</a>
        <span class="px-3 py-2 rounded-xl bg-white/15 border border-white/20 text-white/90">{{ ws.strftime('%d.%m.%Y') }}</span>
      </nav>
    </div>
  </header>

  <main class="max-w-6xl mx-auto px-4 py-6 space-y-4">
    <section class="bg-white rounded-2xl shadow-lg ring-1 ring-black/5 p-4">
      <h2 class="text-lg font-semibold mb-2">Bilgi</h2>
      <p class="text-sm text-gray-700">
        Bu sürümde <strong>takvim ızgarası kaldırıldı</strong>. Yalnızca bilgiler gösterilir.
        (Eski PWA/önbellek kalıntılarını otomatik temizliyoruz.)
      </p>
    </section>

    <section class="bg-white rounded-2xl shadow-lg ring-1 ring-black/5 p-4">
      <h3 class="text-base font-semibold mb-2">Hizmetler</h3>
      <p class="text-sm text-gray-700">{{ ", ".join(services) }}</p>
      <h3 class="text-base font-semibold mt-4 mb-2">Çalışanlar</h3>
      <ul class="list-disc pl-5 text-sm text-gray-700">
        {% for e in employees %}<li>{{ e.name }}</li>{% endfor %}
      </ul>
    </section>
  </main>

  <!-- KILL-SW: Eski Service Worker ve cache'leri temizle -->
  <script>
    (async () => {
      try {
        // SW'leri kapat
        if ('serviceWorker' in navigator) {
          const regs = await navigator.serviceWorker.getRegistrations();
          for (const r of regs) { try { await r.unregister(); } catch {} }
        }
        // Cache Storage'ı temizle
        if ('caches' in window) {
          const keys = await caches.keys();
          await Promise.all(keys.map(k => caches.delete(k)));
        }
      } catch (e) {
        console.warn('SW/cache temizlenemedi:', e);
      }
    })();
  </script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
