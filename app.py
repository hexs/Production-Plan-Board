import io
import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import hexss

hexss.check_packages('flask>=3.0', 'flask-sqlalchemy>=3.1', 'openpyxl', 'tzdata', auto_install=True)

from flask import (Flask, abort, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///plan.db")
# DATABASE_URL (เช่น postgresql+psycopg2://user:pw@host/db), PLANNER_PIN, SECRET_KEY, APP_TZ
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-me")
db = SQLAlchemy(app)

PIN = os.getenv("PLANNER_PIN", "1234")  # ว่าง = ไม่ล็อกหน้าแก้ไข
TZ = ZoneInfo(os.getenv("APP_TZ", "Asia/Bangkok"))
LINES = ["Line2", "Line3"]
CAUSES = ["เครื่องจักรขัดข้อง", "รอวัตถุดิบ", "รอชิ้นส่วน", "เปลี่ยนรุ่น", "คุณภาพ/NG", "ไฟฟ้า/ระบบ", "อื่นๆ"]

CHANGEOVER = 15  # นาที เมื่อเปลี่ยน Model (ไม่นับ job แรกของวัน)
DAY0 = 480  # วันผลิตเริ่ม 08:00 (นาทีนับจาก 00:00 เกิน 1440 = ข้ามเที่ยงคืน)
# ช่วงทำงานตามหัวตาราง Excel เดิม (นาที) ช่องว่างระหว่างช่อง = เวลาพัก
# พักที่ผู้ใช้ระบุ: 09:50-10:00, 11:45-12:35, 14:50-15:00, 17:10-17:30 / พักกะดึกอ่านจาก Excel
WORK = [(480, 540), (540, 590), (600, 660), (660, 705), (755, 840), (840, 890), (900, 960), (960, 1030),
        (1050, 1200), (1200, 1260), (1260, 1320), (1330, 1380), (1380, 1430), (1480, 1560), (1560, 1620),
        (1630, 1680), (1680, 1750), (1770, 1860), (1860, 1920)]
MODES = {"day": dict(limit=1030, label="กะเช้า เลิก 17:10"),
         "ot": dict(limit=1200, label="OT ถึง 20:00"),
         "full": dict(limit=1920, label="ต่อกะดึก ถึง 08:00")}


# ---------------------------------------------------------------- models
class Product(db.Model):
    model = db.Column(db.String(80), primary_key=True)
    cycle_time = db.Column(db.Float, nullable=False)


class Plan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    day = db.Column(db.Date, nullable=False)
    line = db.Column(db.String(20), nullable=False)
    rev = db.Column(db.Integer, default=0)
    mode = db.Column(db.String(10), default="day")
    __table_args__ = (db.UniqueConstraint("day", "line"),)


class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), index=True)
    seq = db.Column(db.Integer, default=0)
    model = db.Column(db.String(80))
    wo = db.Column(db.String(60), default="")
    qty = db.Column(db.Integer)  # จำนวนที่ทำวันนี้
    lot = db.Column(db.Integer)  # LOT ทั้ง WO
    ct = db.Column(db.Float)  # cycle time (วินาที)
    remark = db.Column(db.String(120), default="")
    act_start = db.Column(db.String(5), default="")  # เวลาเริ่มผลิตจริง HH:MM
    act_end = db.Column(db.String(5), default="")


class Actual(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id"), index=True)
    slot = db.Column(db.Integer)
    qty = db.Column(db.Integer)
    __table_args__ = (db.UniqueConstraint("item_id", "slot"),)


class Delay(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    plan_id = db.Column(db.Integer, db.ForeignKey("plan.id"), index=True)
    model = db.Column(db.String(80), default="")
    start = db.Column(db.String(5))
    end = db.Column(db.String(5), default="")
    cause = db.Column(db.String(80), default="")
    action = db.Column(db.String(200), default="")


with app.app_context():
    db.create_all()


# ---------------------------------------------------------------- logic
def now():
    return datetime.now(TZ)


def prod_day():  # วันผลิตเริ่ม 08:00 ตี 2 ยังนับเป็นวันของเมื่อวาน
    return (now() - timedelta(hours=8)).date()


def hm(s):
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def ab(s):  # HH:MM -> นาทีในวันผลิต (ก่อน 08:00 = วันถัดไป)
    m = hm(s)
    return m + 1440 if m < DAY0 else m


def fmt(m):
    m = int(round(m)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def label(m):
    m %= 1440
    return f"{m // 60}:{m % 60:02d}"


@app.template_filter("hhmm")
def _hhmm(m):
    return label(m)


def timeline(mode):
    """ช่องกว้างตามเวลาจริง: slots = ช่วงทำงาน, gaps = เวลาพัก (left/width เป็น %)"""
    end = MODES[mode]["limit"]
    total = end - DAY0
    ws = [(i, s, e) for i, (s, e) in enumerate(WORK) if e <= end]
    pct = lambda m: (m - DAY0) / total * 100
    slots = [dict(i=i, start=s, end=e, label=f"{label(s)}~{label(e)}", left=pct(s), width=pct(e) - pct(s))
             for i, s, e in ws]
    gaps = [dict(start=a[2], end=b[1], left=pct(a[2]), width=pct(b[1]) - pct(a[2]))
            for a, b in zip(ws, ws[1:]) if b[1] > a[2]]
    cols = sorted([dict(kind="w", **x) for x in slots] + [dict(kind="g", **x) for x in gaps],
                  key=lambda c: c["start"])  # ช่องทำงานและช่องพักเรียงตามเวลา
    return dict(start=DAY0, total=total, slots=slots, gaps=gaps, cols=cols)


def schedule(plan, items):
    """เทงานตามลำดับตามเวลาจริง (uph = 3600/CT) ข้ามเวลาพัก หักเปลี่ยนรุ่น 15 นาที
    คืน {item_id: {per (ต่อช่อง), start, end, s_abs, e_abs, co, over}}"""
    iv = [(i, s, e) for i, (s, e) in enumerate(WORK) if e <= MODES[plan.mode]["limit"]]
    k, pos, res = 0, float(DAY0), {}
    for n, it in enumerate(items):
        co = None
        if n and it.model != items[n - 1].model:
            need, cs, ce = float(CHANGEOVER), None, None
            while need > 1e-9 and k < len(iv):
                _, s, e = iv[k]
                pos = max(pos, s)
                if pos >= e - 1e-9:
                    k += 1
                    continue
                cs = pos if cs is None else cs
                t = min(need, e - pos)
                pos += t
                need -= t
                ce = pos
            if cs is not None:
                co = dict(start=fmt(cs), end=fmt(ce), s_abs=round(cs), e_abs=round(ce))
        per, uph = [0] * len(WORK), (3600 / it.ct if it.ct else 0)
        rem, done, given, st = float(it.qty), 0.0, 0, None
        while rem > 1e-9 and uph and k < len(iv):
            i, s, e = iv[k]
            pos = max(pos, s)
            if pos >= e - 1e-9:
                k += 1
                continue
            take = min(rem, (e - pos) * uph / 60)
            st = pos if st is None else st
            pos += take * 60 / uph
            rem -= take
            done += take
            tot = round(done)
            per[i] += tot - given  # ปัดสะสม ผลรวมจึงเท่า qty พอดี
            given = tot
        ok = st is not None
        res[it.id] = dict(per=per, start=fmt(st) if ok else None, end=fmt(pos) if ok else None,
                          s_abs=round(st) if ok else None, e_abs=round(pos) if ok else None,
                          co=co, over=round(rem) if rem > 1e-9 else 0)
    return res


def payload(plan):
    items = Item.query.filter_by(plan_id=plan.id).order_by(Item.seq).all()
    sch = schedule(plan, items)
    acts = {}
    if items:
        for a in Actual.query.filter(Actual.item_id.in_([i.id for i in items])):
            acts[(a.item_id, a.slot)] = a.qty
    jobs = []
    for it in items:
        s = sch[it.id]
        a = [acts.get((it.id, i)) for i in range(len(WORK))]
        planned = sum(p for p, x in zip(s["per"], a) if x is not None)
        done = sum(x for x in a if x is not None)
        diff = done - planned
        if all(x is None for x in a):
            state = "wait"
        elif diff >= 0:
            state = "ok"
        else:
            state = "warn" if planned and diff / planned > -0.05 else "bad"
        sd = ab(it.act_start) - s["s_abs"] if it.act_start and s["s_abs"] else None
        ed = ab(it.act_end) - s["e_abs"] if it.act_end and s["e_abs"] else None
        jobs.append(dict(id=it.id, model=it.model, wo=it.wo, qty=it.qty, lot=it.lot, ct=it.ct,
                         uph=round(3600 / it.ct) if it.ct else 0, remark=it.remark, plan=s["per"],
                         actual=a, start=s["start"], end=s["end"], s_abs=s["s_abs"], e_abs=s["e_abs"],
                         co=s["co"], over=s["over"], act_start=it.act_start, act_end=it.act_end,
                         start_delta=sd, end_delta=ed,
                         act_s_abs=ab(it.act_start) if it.act_start else None,
                         act_e_abs=ab(it.act_end) if it.act_end else None, done=done, diff=diff, state=state))
    delays, total = [], 0
    for d in Delay.query.filter_by(plan_id=plan.id).order_by(Delay.id):
        m = ab(d.end) - ab(d.start) if d.start and d.end else None
        total += m or 0
        delays.append(dict(id=d.id, model=d.model, start=d.start, end=d.end, cause=d.cause,
                           action=d.action, minutes=m))
    return dict(exists=True, line=plan.line, day=plan.day.isoformat(), rev=plan.rev, mode=plan.mode,
                tl=timeline(plan.mode), jobs=jobs, delays=delays, loss=total,
                open_delay=any(not d["end"] for d in delays))


def get_plan(line, d, create=False):
    if line not in LINES:
        abort(404)
    try:
        day = date.fromisoformat(d)
    except ValueError:
        abort(404)
    p = Plan.query.filter_by(line=line, day=day).first()
    if not p and create:
        p = Plan(line=line, day=day)
        db.session.add(p)
        db.session.commit()
    return p


def back(p):
    return redirect(url_for("plan_page", line=p.line, d=p.day.isoformat()))


@app.before_request
def gate():
    if PIN and request.endpoint not in (None, "static", "display", "api_plan", "login") \
            and not session.get("ok"):
        return redirect(url_for("login", next=request.path))


# ---------------------------------------------------------------- pages
@app.route("/login", methods=["GET", "POST"])
def login():
    err = False
    if request.method == "POST":
        if request.form.get("pin") == PIN:
            session["ok"] = True
            nxt = request.args.get("next", "/")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
        err = True
    return render_template("login.html", err=err)


@app.route("/")
def index():
    d = request.args.get("d") or prod_day().isoformat()
    return render_template("index.html", d=d, lines=LINES,
                           products=Product.query.order_by(Product.model).all())


@app.post("/products")
def products():
    m = request.form["model"].strip()
    p = db.session.get(Product, m) or Product(model=m)
    p.cycle_time = float(request.form["ct"])
    db.session.add(p)
    db.session.commit()
    return redirect(url_for("index"))


@app.post("/products/import")
def products_import():
    """นำเข้า Cycle time จากชีต 'data tact' (MODEL | SIDE | QTY/1H | CT)"""
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(request.files["file"].read()), read_only=True, data_only=True)
    ws = wb["data tact"] if "data tact" in wb.sheetnames else wb.worksheets[0]
    seen = {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        if len(r) > 3 and isinstance(r[0], str) and isinstance(r[3], (int, float)) and r[3] > 0:
            seen[r[0].strip()] = float(r[3])
    for m, ct in seen.items():
        p = db.session.get(Product, m) or Product(model=m)
        p.cycle_time = ct
        db.session.add(p)
    db.session.commit()
    return redirect(url_for("index"))


@app.route("/plan/<line>/<d>")
def plan_page(line, d):
    p = get_plan(line, d, True)
    day = p.day
    return render_template("plan.html", p=p, d=payload(p), causes=CAUSES, modes=MODES,
                           products=Product.query.order_by(Product.model).all(),
                           prev=date.fromordinal(day.toordinal() - 1).isoformat(),
                           nxt=date.fromordinal(day.toordinal() + 1).isoformat(),
                           now=now().strftime("%H:%M"))


@app.route("/plan/<line>/<d>/export")
def export_excel(line, d):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    p = get_plan(line, d, True)
    data = payload(p)
    slots = data["tl"]["slots"]

    wb = Workbook()
    ws = wb.active
    ws.title = f"HW {p.line}"
    ws.views.sheetView[0].showGridLines = True

    # สไตล์พื้นฐาน
    font_name = "Tahoma"
    f_title = Font(name=font_name, size=14, bold=True, color="FFFFFF")
    f_sub = Font(name=font_name, size=10, color="E2E8F0")
    f_sec = Font(name=font_name, size=11, bold=True, color="0F172A")
    f_hdr = Font(name=font_name, size=10, bold=True, color="FFFFFF")
    f_shdr = Font(name=font_name, size=9, bold=True, color="1E293B")
    f_cell = Font(name=font_name, size=10, color="0F172A")
    f_bold = Font(name=font_name, size=10, bold=True, color="0F172A")
    f_mute = Font(name=font_name, size=9, color="64748B")
    f_ok = Font(name=font_name, size=10, bold=True, color="166534")
    f_warn = Font(name=font_name, size=10, bold=True, color="92400E")
    f_bad = Font(name=font_name, size=10, bold=True, color="991B1B")

    fill_navy = PatternFill("solid", fgColor="1E293B")
    fill_blue = PatternFill("solid", fgColor="1D4ED8")
    fill_slot = PatternFill("solid", fgColor="E2E8F0")
    fill_plan = PatternFill("solid", fgColor="F8FAFC")
    fill_act = PatternFill("solid", fgColor="FFFFFF")
    fill_sum = PatternFill("solid", fgColor="EFF6FF")
    fill_ok = PatternFill("solid", fgColor="DCFCE7")
    fill_warn = PatternFill("solid", fgColor="FEF3C7")
    fill_bad = PatternFill("solid", fgColor="FEE2E2")

    thin = Side(style="thin", color="CBD5E1")
    med = Side(style="medium", color="94A3B8")
    b_cell = Border(left=thin, right=thin, top=thin, bottom=thin)
    b_job_bottom = Border(left=thin, right=thin, top=thin, bottom=med)
    b_hdr = Border(left=thin, right=thin, top=med, bottom=med)

    al_c = Alignment(horizontal="center", vertical="center", wrap_text=True)
    al_l = Alignment(horizontal="left", vertical="center", wrap_text=True)
    al_r = Alignment(horizontal="right", vertical="center", wrap_text=True)

    # โครงสร้างคอลัมน์:
    # 1:ลำดับ 2:Model 3:WO 4:Qty 5:LOT 6:CT 7:UPH 8:เวลาตามแผน 9:เวลาจริง 10:ประเภท
    # 11..(10+N): ช่วงเวลา, ถัดไป: รวม, Diff, Remark
    fixed_left = 10
    n_slots = len(slots)
    col_total = fixed_left + n_slots + 1
    col_diff = col_total + 1
    col_remark = col_diff + 1
    last_col = col_remark

    # --- แถบหัวรายงาน ---
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
    c1 = ws.cell(1, 1, f"PRODUCTION PLAN & ACTUAL — HW {p.line}")
    c1.font, c1.fill, c1.alignment = f_title, fill_navy, al_l
    ws.row_dimensions[1].height = 28

    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_col)
    meta_txt = (
        f"วันที่: {p.day.strftime('%d %b %Y')}   |   Rev.{p.rev:02d}   |   "
        f"ช่วงเวลา: {MODES[p.mode]['label']}   |   "
        f"ส่งออกเมื่อ: {now().strftime('%d/%m/%Y %H:%M')}"
    )
    c2 = ws.cell(2, 1, meta_txt)
    c2.font, c2.fill, c2.alignment = f_sub, fill_navy, al_l
    ws.row_dimensions[2].height = 20

    # --- ส่วนที่ 1: แผนการผลิต & Actual ---
    row = 4
    ws.cell(row, 1, "1. แผนการผลิตและผลผลิตจริงรายชั่วโมง (Plan vs Actual)").font = f_sec
    ws.row_dimensions[row].height = 22
    row += 1

    hdr_row = row
    left_headers = ["ลำดับ", "Model", "WO", "Qty วันนี้", "LOT", "CT (s)", "UPH",
                    "เวลาตามแผน", "เวลาจริง", "ประเภท"]
    for ci, h in enumerate(left_headers, 1):
        cell = ws.cell(hdr_row, ci, h)
        cell.font, cell.fill, cell.alignment, cell.border = f_hdr, fill_blue, al_c, b_hdr

    for idx, sl in enumerate(slots):
        ci = fixed_left + 1 + idx
        cell = ws.cell(hdr_row, ci, f"{label(sl['start'])}\n-\n{label(sl['end'])}")
        cell.font, cell.fill, cell.alignment, cell.border = f_shdr, fill_slot, al_c, b_hdr

    for ci, h in [(col_total, "รวม"), (col_diff, "Diff"), (col_remark, "Remark")]:
        cell = ws.cell(hdr_row, ci, h)
        cell.font, cell.fill, cell.alignment, cell.border = f_hdr, fill_blue, al_c, b_hdr
    ws.row_dimensions[hdr_row].height = 38

    row += 1
    first_data_row = row
    slot_start_col = get_column_letter(fixed_left + 1)
    slot_end_col = get_column_letter(fixed_left + n_slots)

    if not data["jobs"]:
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=last_col)
        ce = ws.cell(row, 1, "ยังไม่มีรายการแผนการผลิต")
        ce.font, ce.alignment = f_mute, al_c
        for ci in range(1, last_col + 1):
            ws.cell(row, ci).border = b_cell
        ws.row_dimensions[row].height = 24
        row += 1
    else:
        for seq_no, j in enumerate(data["jobs"], 1):
            r_plan = row
            r_act = row + 1

            plan_time_parts = []
            if j["co"]:
                plan_time_parts.append(f"เปลี่ยนรุ่น {j['co']['start']}–{j['co']['end']}")
            if j["start"]:
                plan_time_parts.append(f"{j['start']}–{j['end']}")
            if j["over"]:
                plan_time_parts.append(f"เกินเวลา {j['over']} ชิ้น")
            plan_time_str = "\n".join(plan_time_parts) or "-"

            act_time_str = (
                f"{j['act_start'] or '--:--'}–{j['act_end'] or '--:--'}"
                if (j["act_start"] or j["act_end"]) else "-"
            )

            shared_vals = [
                (1, seq_no, al_c, "0"),
                (2, j["model"], al_l, None),
                (3, j["wo"], al_c, None),
                (4, j["qty"], al_r, "#,##0"),
                (5, j["lot"], al_r, "#,##0"),
                (6, j["ct"], al_r, "0.00"),
                (7, j["uph"], al_r, "#,##0"),
                (8, plan_time_str, al_c, None),
                (9, act_time_str, al_c, None),
                (col_remark, j["remark"], al_l, None),
            ]
            for ci, val, al, num_fmt in shared_vals:
                ws.merge_cells(start_row=r_plan, start_column=ci, end_row=r_act, end_column=ci)
                c = ws.cell(r_plan, ci, val)
                c.font = f_bold if ci == 2 else f_cell
                c.alignment = al
                c.fill = fill_act
                if num_fmt:
                    c.number_format = num_fmt
                ws.cell(r_plan, ci).border = b_cell
                ws.cell(r_act, ci).border = b_job_bottom

            # ป้ายประเภท Plan / Actual
            cp = ws.cell(r_plan, 10, "Plan")
            cp.font, cp.fill, cp.alignment, cp.border = f_mute, fill_plan, al_c, b_cell
            ca = ws.cell(r_act, 10, "Actual")
            ca.font, ca.fill, ca.alignment, ca.border = f_bold, fill_act, al_c, b_job_bottom

            # ข้อมูลรายชั่วโมง
            for idx, sl in enumerate(slots):
                ci = fixed_left + 1 + idx
                pv = j["plan"][sl["i"]]
                av = j["actual"][sl["i"]]

                cell_p = ws.cell(r_plan, ci, pv if pv else None)
                cell_p.font, cell_p.fill, cell_p.alignment, cell_p.border = f_cell, fill_plan, al_r, b_cell
                cell_p.number_format = "#,##0"

                cell_a = ws.cell(r_act, ci, av if av is not None else None)
                cell_a.alignment, cell_a.border = al_r, b_job_bottom
                cell_a.number_format = "#,##0"
                if av is None:
                    cell_a.font, cell_a.fill = f_cell, fill_act
                elif av < pv:
                    cell_a.font, cell_a.fill = f_bad, fill_bad
                else:
                    cell_a.font, cell_a.fill = f_ok, fill_ok

            # สูตรรวม Plan / Actual
            if n_slots:
                c_tp = ws.cell(r_plan, col_total, f"=SUM({slot_start_col}{r_plan}:{slot_end_col}{r_plan})")
                c_ta = ws.cell(r_act, col_total, f"=SUM({slot_start_col}{r_act}:{slot_end_col}{r_act})")
                diff_formula = (
                    f'=IF(COUNT({slot_start_col}{r_act}:{slot_end_col}{r_act})=0,"",'
                    f'SUM({slot_start_col}{r_act}:{slot_end_col}{r_act})'
                    f'-SUMIFS({slot_start_col}{r_plan}:{slot_end_col}{r_plan},'
                    f'{slot_start_col}{r_act}:{slot_end_col}{r_act},"<>"))'
                )
            else:
                c_tp = ws.cell(r_plan, col_total, 0)
                c_ta = ws.cell(r_act, col_total, 0)
                diff_formula = ""

            c_tp.font, c_tp.fill, c_tp.alignment, c_tp.border = f_bold, fill_plan, al_r, b_cell
            c_tp.number_format = "#,##0"
            c_ta.font, c_ta.fill, c_ta.alignment, c_ta.border = f_bold, fill_act, al_r, b_job_bottom
            c_ta.number_format = "#,##0"

            # Diff (merge 2 แถว)
            ws.merge_cells(start_row=r_plan, start_column=col_diff, end_row=r_act, end_column=col_diff)
            c_df = ws.cell(r_plan, col_diff, diff_formula)
            c_df.alignment = al_c
            c_df.number_format = "+#,##0;-#,##0;0"
            if j["state"] == "ok":
                c_df.font, c_df.fill = f_ok, fill_ok
            elif j["state"] == "warn":
                c_df.font, c_df.fill = f_warn, fill_warn
            elif j["state"] == "bad":
                c_df.font, c_df.fill = f_bad, fill_bad
            else:
                c_df.font, c_df.fill = f_mute, fill_act
            ws.cell(r_plan, col_diff).border = b_cell
            ws.cell(r_act, col_diff).border = b_job_bottom

            h_row = 24 if len(plan_time_parts) <= 2 else 28
            ws.row_dimensions[r_plan].height = h_row
            ws.row_dimensions[r_act].height = h_row
            row += 2

        # แถวสรุปรวม (Summary Plan & Actual)
        last_data_row = row - 1
        sum_p, sum_a = row, row + 1
        ws.merge_cells(start_row=sum_p, start_column=1, end_row=sum_a, end_column=3)
        cs = ws.cell(sum_p, 1, "รวมทั้งหมด (Total)")
        cs.font, cs.fill, cs.alignment = f_bold, fill_sum, al_c

        ws.merge_cells(start_row=sum_p, start_column=4, end_row=sum_a, end_column=4)
        cq = ws.cell(sum_p, 4, f"=SUM(D{first_data_row}:D{last_data_row})")
        cq.font, cq.fill, cq.alignment, cq.number_format = f_bold, fill_sum, al_r, "#,##0"

        ws.merge_cells(start_row=sum_p, start_column=5, end_row=sum_a, end_column=9)
        ws.cell(sum_p, 5, "").fill = fill_sum

        ws.cell(sum_p, 10, "Plan").font = f_bold
        ws.cell(sum_a, 10, "Actual").font = f_bold
        for r_s in (sum_p, sum_a):
            ws.cell(r_s, 10).alignment = al_c
            ws.cell(r_s, 10).fill = fill_sum

        for ci in range(fixed_left + 1, col_total + 1):
            col_l = get_column_letter(ci)
            cp = ws.cell(sum_p, ci,
                         f'=SUMIF($J${first_data_row}:$J${last_data_row},"Plan",{col_l}{first_data_row}:{col_l}{last_data_row})')
            ca = ws.cell(sum_a, ci,
                         f'=SUMIF($J${first_data_row}:$J${last_data_row},"Actual",{col_l}{first_data_row}:{col_l}{last_data_row})')
            for c in (cp, ca):
                c.font, c.fill, c.alignment, c.number_format = f_bold, fill_sum, al_r, "#,##0"

        ws.merge_cells(start_row=sum_p, start_column=col_diff, end_row=sum_a, end_column=col_diff)
        col_d_l = get_column_letter(col_diff)
        cd = ws.cell(sum_p, col_diff, f"=SUM({col_d_l}{first_data_row}:{col_d_l}{last_data_row})")
        cd.font, cd.fill, cd.alignment, cd.number_format = f_bold, fill_sum, al_c, "+#,##0;-#,##0;0"

        ws.merge_cells(start_row=sum_p, start_column=col_remark, end_row=sum_a, end_column=col_remark)
        ws.cell(sum_p, col_remark, "").fill = fill_sum

        for ci in range(1, last_col + 1):
            ws.cell(sum_p, ci).border = b_cell
            ws.cell(sum_a, ci).border = b_job_bottom
            if ws.cell(sum_p, ci).fill.fill_type is None:
                ws.cell(sum_p, ci).fill = fill_sum
            if ws.cell(sum_a, ci).fill.fill_type is None:
                ws.cell(sum_a, ci).fill = fill_sum

        ws.row_dimensions[sum_p].height = 22
        ws.row_dimensions[sum_a].height = 22
        row += 2

    # --- ส่วนที่ 2: Cause of delay ---
    row += 1
    ws.cell(row, 1, "2. บันทึกการหยุดการผลิต (Cause of delay)").font = f_sec
    ws.row_dimensions[row].height = 22
    row += 1

    delay_headers = [
        (1, 1, "ลำดับ"), (2, 2, "Model"), (3, 3, "เริ่ม"), (4, 4, "จบ"),
        (5, 5, "เวลาสูญเสีย (นาที)"), (6, 8, "สาเหตุ"), (9, last_col, "Action")
    ]
    for c1_i, c2_i, title in delay_headers:
        if c2_i > c1_i:
            ws.merge_cells(start_row=row, start_column=c1_i, end_row=row, end_column=c2_i)
        c = ws.cell(row, c1_i, title)
        c.font, c.fill, c.alignment = f_hdr, fill_navy, al_c
        for ci in range(c1_i, c2_i + 1):
            ws.cell(row, ci).border = b_hdr
            ws.cell(row, ci).fill = fill_navy
    ws.row_dimensions[row].height = 24
    row += 1

    if not data["delays"]:
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=last_col)
        ce = ws.cell(row, 1, "ไม่มีรายการหยุดการผลิต")
        ce.font, ce.alignment = f_mute, al_c
        for ci in range(1, last_col + 1):
            ws.cell(row, ci).border = b_cell
        ws.row_dimensions[row].height = 22
    else:
        d_start = row
        for idx, dl in enumerate(data["delays"], 1):
            vals = [
                (1, 1, idx, al_c),
                (2, 2, dl["model"], al_l),
                (3, 3, dl["start"], al_c),
                (4, 4, dl["end"] or "กำลังหยุด", al_c),
                (5, 5, dl["minutes"], al_r),
                (6, 8, dl["cause"], al_l),
                (9, last_col, dl["action"], al_l),
            ]
            for c1_i, c2_i, val, al in vals:
                if c2_i > c1_i:
                    ws.merge_cells(start_row=row, start_column=c1_i, end_row=row, end_column=c2_i)
                c = ws.cell(row, c1_i, val)
                c.font, c.alignment = f_cell, al
                if c1_i == 5:
                    c.number_format = "#,##0"
                for ci in range(c1_i, c2_i + 1):
                    ws.cell(row, ci).border = b_cell
            ws.row_dimensions[row].height = 22
            row += 1

        d_end = row - 1
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
        ct = ws.cell(row, 1, "รวมเวลาสูญเสียทั้งหมด (นาที)")
        ct.font, ct.fill, ct.alignment = f_bold, fill_sum, al_r
        cm = ws.cell(row, 5, f"=SUM(E{d_start}:E{d_end})")
        cm.font, cm.fill, cm.alignment, cm.number_format = f_bad, fill_bad, al_r, "#,##0"
        ws.merge_cells(start_row=row, start_column=6, end_row=row, end_column=last_col)
        ws.cell(row, 6, "").fill = fill_sum
        for ci in range(1, last_col + 1):
            ws.cell(row, ci).border = b_job_bottom
            if ws.cell(row, ci).fill.fill_type is None:
                ws.cell(row, ci).fill = fill_sum
        ws.row_dimensions[row].height = 24

    # ความกว้างคอลัมน์
    widths = {1: 7, 2: 24, 3: 18, 4: 11, 5: 10, 6: 9, 7: 9, 8: 20, 9: 15, 10: 9}
    for ci, w in widths.items():
        ws.column_dimensions[get_column_letter(ci)].width = w
    for idx in range(n_slots):
        ws.column_dimensions[get_column_letter(fixed_left + 1 + idx)].width = 11
    ws.column_dimensions[get_column_letter(col_total)].width = 10
    ws.column_dimensions[get_column_letter(col_diff)].width = 10
    ws.column_dimensions[get_column_letter(col_remark)].width = 22

    # ตั้งค่าการพิมพ์แนวนอน พอดี 1 หน้ากว้าง + Freeze header
    ws.freeze_panes = "D6"
    ws.page_setup.orientation = ws.ORIENTATION_LANDSCAPE
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"Production_Plan_{p.line}_{p.day.isoformat()}_Rev{p.rev:02d}.xlsx"
    return send_file(
        buf,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/plan/<line>/<d>/meta")
def meta(line, d):
    p = get_plan(line, d, True)
    if request.form["op"] == "mode":
        if request.form["mode"] in MODES:
            p.mode = request.form["mode"]
    else:
        p.rev += 1
    db.session.commit()
    return back(p)


@app.post("/plan/<line>/<d>/add")
def add_item(line, d):
    p = get_plan(line, d, True)
    f = request.form
    model = f["model"].strip()
    ct = float(f["ct"]) if f.get("ct") else None
    if ct is None:
        prod = db.session.get(Product, model)
        if not prod:
            return f"ไม่พบ Cycle time ของ {model} กรุณากรอก CT หรือเพิ่มใน Product master", 400
        ct = prod.cycle_time
    qty = int(f["qty"])
    last = db.session.query(db.func.max(Item.seq)).filter_by(plan_id=p.id).scalar() or 0
    db.session.add(Item(plan_id=p.id, seq=last + 1, model=model, wo=f.get("wo", "").strip(),
                        qty=qty, lot=int(f["lot"]) if f.get("lot") else qty, ct=ct))
    db.session.commit()
    return back(p)


@app.post("/item/<int:iid>")
def item_op(iid):
    it = db.get_or_404(Item, iid)
    p = db.session.get(Plan, it.plan_id)
    op, f = request.form["op"], request.form
    if op == "save":
        it.model, it.wo, it.remark = f["model"].strip(), f["wo"].strip(), f.get("remark", "")
        it.qty, it.lot, it.ct = int(f["qty"]), int(f["lot"] or f["qty"]), float(f["ct"])
    elif op == "del":
        Actual.query.filter_by(item_id=iid).delete()
        db.session.delete(it)
    else:
        q = Item.query.filter(Item.plan_id == p.id)
        nb = (q.filter(Item.seq < it.seq).order_by(Item.seq.desc()) if op == "up"
              else q.filter(Item.seq > it.seq).order_by(Item.seq)).first()
        if nb:
            it.seq, nb.seq = nb.seq, it.seq
    db.session.commit()
    return back(p)


@app.post("/plan/<line>/<d>/actual")
def save_actual(line, d):
    p = get_plan(line, d)
    ids = {i.id for i in Item.query.filter_by(plan_id=p.id)}
    for k, v in request.form.items():
        if k[:2] in ("s_", "e_") and int(k[2:]) in ids:  # เวลาเริ่ม/จบจริง
            v = v.strip()
            setattr(db.session.get(Item, int(k[2:])), "act_start" if k[0] == "s" else "act_end",
                    v if re.fullmatch(r"\d\d:\d\d", v) else "")
        if not k.startswith("a_"):
            continue
        _, iid, si = k.split("_")
        iid, si = int(iid), int(si)
        if iid not in ids:
            continue
        row = Actual.query.filter_by(item_id=iid, slot=si).first()
        if v.strip() == "":
            if row:
                db.session.delete(row)
        elif row:
            row.qty = int(v)
        else:
            db.session.add(Actual(item_id=iid, slot=si, qty=int(v)))
    db.session.commit()
    return back(p)


@app.post("/plan/<line>/<d>/delay")
def add_delay(line, d):
    p = get_plan(line, d)
    f = request.form
    db.session.add(Delay(plan_id=p.id, model=f.get("model", ""), start=f["start"], end=f.get("end", ""),
                         cause=f.get("cause", ""), action=f.get("action", "")))
    db.session.commit()
    return back(p)


@app.post("/delay/<int:did>")
def delay_op(did):
    x = db.get_or_404(Delay, did)
    p = db.session.get(Plan, x.plan_id)
    if request.form["op"] == "end":
        x.end = now().strftime("%H:%M")
    else:
        db.session.delete(x)
    db.session.commit()
    return back(p)


# ---------------------------------------------------------------- TV
@app.route("/display/<line>")
def display(line):
    if line not in LINES:
        abort(404)
    return render_template("display.html", line=line)


@app.route("/api/plan/<line>")
def api_plan(line):
    p = get_plan(line, request.args.get("d") or prod_day().isoformat())
    r = jsonify(payload(p) if p else dict(exists=False))
    r.headers["Cache-Control"] = "no-store"
    return r


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5004, debug=False)
