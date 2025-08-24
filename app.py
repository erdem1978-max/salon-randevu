import os
from datetime import datetime, date, time, timedelta
from dataclasses import dataclass

from flask import Flask, request, redirect, url_for, make_response
from flask import render_template_string, abort
from sqlalchemy import (create_engine, Column, Integer, String, DateTime, Boolean,
                        ForeignKey, UniqueConstraint)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, scoped_session
from apscheduler.schedulers.background import BackgroundScheduler
from pytz import timezone
from twilio.rest import Client

# -------------------- Ayarlar --------------------
TZ_NAME = os.getenv("TIMEZONE", "Europe/Istanbul")
TZ = timezone(TZ_NAME)
DB_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DB_DIR, exist_ok=True)
DB_URL = f"sqlite:///{os.path.join(DB_DIR, 'salon.db')}"

SERVICES = [
    "Lazer epilasyon", "Kirpik", "Tırnak", "Manikür", "Pedikür",
    "Nail art", "Sigara bırakma", "İştah kapatma", "Botox",
    "Dolgu", "Dövme silme", "Kaş", "Cilt bakımı"
]

EMPLOYEE_NAMES = ["Merve", "Zeynep", "İrem", "X"]
SLOT_MINUTES = 60
START_TIME = time(9, 0)      # 09:00
END_TIME = time(19, 30)      # 19:30 (kapanış)

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

    __table_args__ = (
        UniqueConstraint("employee_id", "start_time", name="uq_employee_slot"),
    )

Base.metadata.create_all(engine)

# Seed employees
with Session() as s:
    existing = {e.name for e in s.query(Employee).all()}
    for n in EMPLOYEE_NAMES:
        if n not in existing:
            s.add(Employee(name=n))
    s.commit()

# -------------------- WhatsApp --------------------
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_WHATSAPP_FROM")  # örn: whatsapp:+14155238886
TWILIO_ENABLED = bool(TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM)
client = Client(TWILIO_SID, TWILIO_TOKEN) if TWILIO_ENABLED else None

# -------------------- Flask --------------------
app = Flask(__name__)

# -------------------- Yardımcılar --------------------
TR_DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]

@dataclass
class Slot:
    dt: datetime
    label: str  # "HH:MM"

def week_start_for(d: date) -> date:
    # Haftabaşı: Pazartesi
    return d - timedelta(days=(d.weekday()))

def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def time_to_dt(day: date, t: time) -> datetime:
    return TZ.localize(datetime.combine(day, t))

def iter_slots_for_day(day: date) -> list[Slot]:
    """09:00'dan 19:30'a kadar 60 dk slotlar; son özel slot 18:30-19:30."""
    slots = []
    cur = datetime.combine(day, START_TIME)
    end_dt = datetime.combine(day, END_TIME)
    # 60 dakikalık adımlarla 18:00'a kadar ilerle
    while cur + timedelta(minutes=SLOT_MINUTES) <= end_dt:
        slots.append(Slot(dt=TZ.localize(cur), label=cur.strftime("%H:%M")))
        cur += timedelta(minutes=SLOT_MINUTES)
    # Özel: 18:30 slotu (eğer henüz eklenmediyse ve gün cumartesi/haftaiçi ise)
    special = datetime.combine(day, time(18, 30))
    special_dt = TZ.localize(special)
    if all(s.dt.time() != time(18,30) for s in slots) and special_dt + timedelta(minutes=60) <= TZ.localize(end_dt):
        # Pazar hariç günler için ekle
        if day.weekday() != 6:
            slots.append(Slot(dt=special_dt, label=special.strftime("%H:%M")))
    # Pazar (kapalı): slot listesi boş kalsın
    if day.weekday() == 6:
        return []
    return slots

def week_days(start: date) -> list[date]:
    return [start + timedelta(days=i) for i in range(7)]

def fetch_week_appointments(ses, week_days_list: list[date]):
    start_dt = TZ.localize(datetime.combine(week_days_list[0], time(0,0)))
    end_dt = TZ.localize(datetime.combine(week_days_list[-1], time(23,59)))
    items = ses.query(Appointment).filter(Appointment.start_time >= start_dt,
                                         Appointment.start_time <= end_dt).all()
    return items

def appt_key(emp_id: int, dt: datetime) -> tuple:
    return (emp_id, dt.strftime("%Y-%m-%d %H:%M"))

# -------------------- Rotalar --------------------
@app.route("/")
def index():
    with Session() as s:
        today = datetime.now(TZ).date()
        ws_param = request.args.get("week_start")
        if ws_param:
            ws = parse_date(ws_param)
        else:
            ws = week_start_for(today)
        days = week_days(ws)
        employees = s.query(Employee).order_by(Employee.id).all()

        # Tüm randevuları map'le
        appointments = fetch_week_appointments(s, days)
        appt_map = {}
        for a in appointments:
            appt_map[appt_key(a.employee_id, a.start_time)] = a

        # Slot matrisi
        day_slots = {d: iter_slots_for_day(d) for d in days}

        prev_w = (ws - timedelta(days=7)).strftime("%Y-%m-%d")
        next_w = (ws + timedelta(days=7)).strftime("%Y-%m-%d")
        this_w = week_start_for(today).strftime("%Y-%m-%d")

        return render_template_string(INDEX_HTML,
                                      ws=ws,
                                      TR_DAYS=TR_DAYS,
                                      days=days,
                                      employees=employees,
                                      day_slots=day_slots,
                                      appt_map=appt_map,
                                      appt_key=appt_key,
                                      prev_w=prev_w,
                                      next_w=next_w,
                                      this_w=this_w,
                                      services=SERVICES)

@app.get("/slot")
def slot_modal():
    date_str = request.args.get("date")
    time_str = request.args.get("time")
    emp_id = int(request.args.get("employee_id"))
    if not (date_str and time_str and emp_id):
        abort(400)
    day = parse_date(date_str)
    dt = TZ.localize(datetime.combine(day, datetime.strptime(time_str, "%H:%M").time()))

    with Session() as s:
        emp = s.get(Employee, emp_id)
        if not emp:
            abort(404)
        appt = s.query(Appointment).filter_by(employee_id=emp_id, start_time=dt).one_or_none()
        return render_template_string(SLOT_HTML, emp=emp, dt=dt, appt=appt, services=SERVICES)

@app.post("/appointments")
def create_appointment():
    emp_id = int(request.form.get("employee_id"))
    date_str = request.form.get("date")
    time_str = request.form.get("time")
    customer_name = request.form.get("customer_name", "").strip()
    phone = request.form.get("phone", "").strip()
    service = request.form.get("service", "").strip()

    if not (customer_name and service and date_str and time_str):
        return ("Zorunlu alanlar eksik.", 400)

    day = parse_date(date_str)
    dt = TZ.localize(datetime.combine(day, datetime.strptime(time_str, "%H:%M").time()))

    # Pazar kapalı
    if day.weekday() == 6:
        return ("Pazar günü randevu alınamaz.", 400)

    # Slot uygun mu?
    valid_labels = [s.label for s in iter_slots_for_day(day)]
    if time_str not in valid_labels:
        return ("Geçersiz saat dilimi.", 400)

    with Session() as s:
        emp = s.get(Employee, emp_id)
        if not emp:
            return ("Çalışan bulunamadı.", 404)
        # Çakışma kontrolü
        exists = s.query(Appointment).filter_by(employee_id=emp_id, start_time=dt).first()
        if exists:
            return ("Bu slot zaten dolu.", 400)
        appt = Appointment(
            employee_id=emp_id,
            customer_name=customer_name,
            phone=phone,
            service=service,
            start_time=dt,
            end_time=dt + timedelta(minutes=SLOT_MINUTES)
        )
        s.add(appt)
        s.commit()

    # HX-Redirect ile sayfayı aynı haftaya tazele
    ws = week_start_for(day).strftime("%Y-%m-%d")
    resp = make_response("", 204)
    resp.headers["HX-Redirect"] = url_for("index", week_start=ws)
    return resp

@app.post("/appointments/<int:appt_id>/delete")
def delete_appointment(appt_id: int):
    with Session() as s:
        appt = s.get(Appointment, appt_id)
        if not appt:
            return ("Randevu bulunamadı.", 404)
        day = appt.start_time.date()
        s.delete(appt)
        s.commit()
    ws = week_start_for(day).strftime("%Y-%m-%d")
    resp = make_response("", 204)
    resp.headers["HX-Redirect"] = url_for("index", week_start=ws)
    return resp

# -------------------- Scheduler --------------------
def send_whatsapp_reminders():
    if not TWILIO_ENABLED:
        # Kurulum yapılmadıysa sessizce çık (geliştirme için konsola yazılabilir)
        return
    now = datetime.now(TZ)
    window_start = now + timedelta(minutes=60)
    window_end = now + timedelta(minutes=61)
    with Session() as s:
        upcoming = (
            s.query(Appointment)
             .filter(Appointment.start_time >= window_start,
                     Appointment.start_time < window_end,
                     Appointment.reminder_sent == False)
             .all()
        )
        for a in upcoming:
            if not a.phone:
                a.reminder_sent = True  # numara yoksa atla ama tekrar denememek için işaretle
                continue
            try:
                to = a.phone
                if not to.startswith("whatsapp:"):
                    to = f"whatsapp:{to}"
                body = (
                    f"Merhaba {a.customer_name},\n"
                    f"{a.start_time.strftime('%d.%m.%Y %H:%M')} saatindeki '{a.service}' randevunuzu hatırlatırız.\n"
                    f"(Gönderen: {a.employee.name})"
                )
                client.messages.create(from_=TWILIO_FROM, to=to, body=body)
                a.reminder_sent = True
            except Exception as e:
                # Hata olursa tekrar denemek için işaretlemeyi kaldırma (log atılabilir)
                print("WhatsApp gönderim hatası:", e)
        s.commit()

scheduler = BackgroundScheduler(timezone=TZ_NAME)
scheduler.add_job(send_whatsapp_reminders, "interval", minutes=1, id="wa_reminders", replace_existing=True)
scheduler.start()

# -------------------- Şablonlar (ESTETİK DOKUNUŞLU) --------------------
INDEX_HTML = r"""
<!doctype html>
<html lang="tr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Randevu Takvimi</title>
  <script src="https://unpkg.com/htmx.org@1.9.12"></script>
  <script src="https://unpkg.com/hyperscript.org@0.9.12"></script>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-gradient-to-b from-rose-50 to-white text-gray-900 min-h-screen">
  <!-- Degrade başlık + modern butonlar -->
  <header class="bg-gradient-to-r from-rose-500 via-fuchsia-500 to-violet-500 text-white">
    <div class="max-w-7xl mx-auto px-4 sm:px-6 py-6">
      <div class="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-3">
        <div>
          <h1 class="text-3xl font-extrabold tracking-tight drop-shadow">Güzellik Merkezi Randevu Sistemi</h1>
          <p class="text-sm/6 text-white/90">
            Haftalık görünüm • Dolu:
            <span class="inline-block w-3 h-3 bg-red-500 rounded-sm align-middle"></span>
            • Boş:
            <span class="inline-block w-3 h-3 bg-white/90 border border-white/50 rounded-sm align-middle"></span>
          </p>
        </div>
        <nav class="flex items-center gap-2">
          <a class="px-3 py-2 rounded-xl bg-white/15 hover:bg-white/25 border border-white/20 backdrop-blur transition"
             href="?week_start={{ prev_w }}">◀ Önceki</a>
          <a class="px-3 py-2 rounded-xl bg-white text-rose-700 font-semibold hover:bg-rose-50 border border-white/0 transition"
             href="?week_start={{ this_w }}">Bugün</a>
          <a class="px-3 py-2 rounded-xl bg-white/15 hover:bg-white/25 border border-white/20 backdrop-blur transition"
             href="?week_start={{ next_w }}">Sonraki ▶</a>
        </nav>
      </div>
    </div>
  </header>

  <main class="max-w-7xl mx-auto px-4 sm:px-6 py-6 space-y-4">
    {% set emp_colors = ['bg-rose-500','bg-violet-500','bg-emerald-500','bg-amber-500'] %}

    <!-- Üst bilgi kartı -->
    <section class="bg-white/80 backdrop-blur rounded-2xl shadow-lg ring-1 ring-black/5 p-4">
      <div class="flex flex-wrap items-center gap-x-6 gap-y-2">
        <div class="text-sm text-gray-700 flex items-center gap-3">
          <span class="font-semibold">Çalışanlar:</span>
          {% for e in employees %}
            <span class="inline-flex items-center gap-1 text-gray-800">
              <span class="w-2.5 h-2.5 rounded-full {{ emp_colors[loop.index0 % 4] }}"></span>
              <span class="font-medium">{{ e.name }}</span>
            </span>
          {% endfor %}
        </div>
        <div class="text-sm text-gray-700 flex-1">
          <span class="font-semibold">Hizmetler:</span>
          <span class="text-gray-600">{{ ", ".join(services) }}</span>
        </div>
      </div>
    </section>

    <!-- Takvim (kart: yuvarlatılmış + gölgeli, zebra satırlar) -->
    <section class="bg-white rounded-2xl shadow-xl ring-1 ring-black/5 overflow-x-auto">
      <table class="min-w-full text-sm">
        <thead class="sticky top-0 z-10 bg-white/90 backdrop-blur border-b">
          <tr>
            <th class="p-3 text-left w-24 font-semibold text-gray-700">Saat</th>
            {% for d in days %}
              <th class="p-3 text-center min-w-[220px] font-semibold text-gray-800">
                <div>{{ TR_DAYS[loop.index0] }}</div>
                <div class="text-xs text-gray-500 font-normal">{{ d.strftime('%d.%m.%Y') }}</div>
                {% if d.weekday() == 6 %}<div class="text-xs text-rose-600 font-semibold mt-1">Kapalı</div>{% endif %}
              </th>
            {% endfor %}
          </tr>
        </thead>
        <tbody class="[&_tr:nth-child(odd)]:bg-gray-50/40">
          {% set all_labels = [] %}
          {% for d in days %}
            {% for s in day_slots[d] %}
              {% if s.label not in all_labels %}{% set _ = all_labels.append(s.label) %}{% endif %}
            {% endfor %}
          {% endfor %}

          {% for label in all_labels %}
            <tr class="align-top">
              <th class="p-3 font-semibold text-gray-700 sticky left-0 bg-inherit">{{ label }}</th>
              {% for d in days %}
                <td class="p-2">
                  {% if d.weekday() == 6 %}
                    <div class="text-center text-xs text-gray-400 py-8">Kapalı</div>
                  {% else %}
                    <div class="grid grid-cols-1 gap-1">
                      {% for e in employees %}
                        {% set slot_dt = (d.strftime('%Y-%m-%d') + ' ' + label) %}
                        {% set a = appt_map.get((e.id, slot_dt)) %}
                        {% set dot = emp_colors[loop.index0 % 4] %}
                        {% if a %}
                          <!-- DOLU: kırmızı gradient -->
                          <button
                            class="w-full text-left p-2.5 rounded-xl bg-gradient-to-r from-red-500 to-rose-600 text-white hover:opacity-95 shadow-sm transition"
                            hx-get="/slot?date={{ d.strftime('%Y-%m-%d') }}&time={{ label }}&employee_id={{ e.id }}"
                            hx-target="#modal" hx-swap="innerHTML">
                            <div class="flex items-center justify-between">
                              <div class="flex items-center gap-2">
                                <span class="w-2 h-2 rounded-full {{ dot }}"></span>
                                <span class="font-semibold">{{ e.name }}</span>
                              </div>
                              <span class="text-[10px] uppercase tracking-wide bg-white/20 px-2 py-0.5 rounded-full">DOLU</span>
                            </div>
                            <div class="text-xs/5 mt-1 opacity-95 truncate">{{ a.customer_name }} – {{ a.service }}</div>
                          </button>
                        {% else %}
                          <!-- BOŞ: beyaz kart + hover gölge -->
                          <button
                            class="w-full text-left p-2.5 rounded-xl bg-white border border-gray-200 hover:border-rose-300 hover:shadow-sm transition"
                            hx-get="/slot?date={{ d.strftime('%Y-%m-%d') }}&time={{ label }}&employee_id={{ e.id }}"
                            hx-target="#modal" hx-swap="innerHTML">
                            <div class="flex items-center justify-between">
                              <div class="flex items-center gap-2">
                                <span class="w-2 h-2 rounded-full {{ dot }}"></span>
                                <span class="font-semibold text-gray-800">{{ e.name }}</span>
                              </div>
                              <span class="text-[10px] uppercase tracking-wide text-gray-500">BOŞ</span>
                            </div>
                            <div class="text-xs text-gray-500 mt-1">Randevu ekle</div>
                          </button>
                        {% endif %}
                      {% endfor %}
                    </div>
                  {% endif %}
                </td>
              {% endfor %}
            </tr>
          {% endfor %}
        </tbody>
      </table>
    </section>
  </main>

  <div id="modal"></div>
</body>
</html>
"""

SLOT_HTML = r"""
<!-- Cam efektli (glass) modal -->
<div class="fixed inset-0 bg-black/40 backdrop-blur-sm flex items-center justify-center p-3" _="on click if event.target.matches('.fixed') then remove me">
  <div class="bg-white/95 w-full max-w-md rounded-2xl shadow-2xl ring-1 ring-black/5 p-5"
       _="on keydown[key=='Escape'] from window halt event then remove closest .fixed">
    {% if appt %}
      <h2 class="text-lg font-semibold mb-3">Randevu Detayı</h2>
      <dl class="text-sm grid grid-cols-3 gap-2 mb-4">
        <dt class="text-gray-500">Çalışan</dt><dd class="col-span-2">{{ appt.employee.name }}</dd>
        <dt class="text-gray-500">Tarih/Saat</dt><dd class="col-span-2">{{ appt.start_time.strftime('%d.%m.%Y %H:%M') }}</dd>
        <dt class="text-gray-500">Müşteri</dt><dd class="col-span-2">{{ appt.customer_name }}</dd>
        <dt class="text-gray-500">Telefon</dt><dd class="col-span-2">{{ appt.phone or '-' }}</dd>
        <dt class="text-gray-500">Hizmet</dt>
        <dd class="col-span-2">
          <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs bg-rose-100 text-rose-700 border border-rose-200">
            {{ appt.service }}
          </span>
        </dd>
      </dl>
      <div class="flex justify-end gap-2">
        <button class="px-3 py-2 rounded-xl border hover:bg-gray-50" onclick="this.closest('.fixed').remove()">Kapat</button>
        <form hx-post="/appointments/{{ appt.id }}/delete" hx-target="body" hx-swap="none">
          <button class="px-3 py-2 rounded-xl bg-red-600 text-white hover:bg-red-700 shadow-sm">Sil</button>
        </form>
      </div>
    {% else %}
      <h2 class="text-lg font-semibold mb-3">Yeni Randevu</h2>
      <form class="space-y-3" hx-post="/appointments" hx-target="body" hx-swap="none">
        <input type="hidden" name="employee_id" value="{{ emp.id }}" />
        <input type="hidden" name="date" value="{{ dt.strftime('%Y-%m-%d') }}" />
        <input type="hidden" name="time" value="{{ dt.strftime('%H:%M') }}" />
        <div class="text-sm text-gray-600">{{ emp.name }} – {{ dt.strftime('%d.%m.%Y %H:%M') }}</div>
        <div>
          <label class="block text-sm font-medium mb-1">Müşteri Adı Soyadı</label>
          <input required name="customer_name" class="w-full p-2.5 rounded-xl border border-gray-300 focus:outline-none focus:ring-2 focus:ring-rose-300" placeholder="Ad Soyad" />
        </div>
        <div>
          <label class="block text-sm font-medium mb-1">Telefon (WhatsApp)</label>
          <input name="phone" class="w-full p-2.5 rounded-xl border border-gray-300 focus:outline-none focus:ring-2 focus:ring-rose-300" placeholder="+905331112233" />
          <p class="text-xs text-gray-500 mt-1">E.164 formatı önerilir. Mesajlar randevudan 60 dk önce gönderilir.</p>
        </div>
        <div>
          <label class="block text-sm font-medium mb-1">Hizmet</label>
          <select required name="service" class="w-full p-2.5 rounded-xl border border-gray-300 bg-white focus:outline-none focus:ring-2 focus:ring-rose-300">
            <option value="" disabled selected>Seçiniz</option>
            {% for s in services %}<option value="{{ s }}">{{ s }}</option>{% endfor %}
          </select>
        </div>
        <div class="flex justify-end gap-2 pt-2">
          <button type="button" class="px-3 py-2 rounded-xl border hover:bg-gray-50" onclick="this.closest('.fixed').remove()">Vazgeç</button>
          <button class="px-3 py-2 rounded-xl bg-gradient-to-r from-rose-500 to-fuchsia-600 text-white hover:opacity-95 shadow-sm">Kaydet</button>
        </div>
      </form>
    {% endif %}
  </div>
</div>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
