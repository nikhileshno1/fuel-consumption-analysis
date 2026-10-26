import os
import io
import math
import base64
from datetime import date
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter
import numpy as np
import pandas as pd
import mysql.connector
from flask import Flask, jsonify, request, render_template, Response
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "3306")),
        user=os.getenv("DB_USER", "root"),
        password=os.getenv("DB_PASSWORD", ""),
        database=os.getenv("DB_NAME", "fuel_consumption_db")
    )


def query(sql, params=None):
    connection = get_db_connection()
    try:
        cursor = connection.cursor(dictionary=True)
        cursor.execute(sql, params or ())
        rows = cursor.fetchall()
        cursor.close()
        return rows
    finally:
        connection.close()


def get_vehicles():
    return query("SELECT * FROM vehicles ORDER BY vehicle_id")


def get_fuel_data():
    return query("""
        SELECT fr.record_id, fr.vehicle_id, v.vehicle_code,
               v.vehicle_type, v.fuel_type, fr.fill_date,
               fr.odometer_km, fr.litres, fr.total_cost,
               fr.full_tank_flag
        FROM fuel_records fr
        JOIN vehicles v ON fr.vehicle_id = v.vehicle_id
        ORDER BY fr.vehicle_id, fr.fill_date, fr.record_id
    """)



def num(value):
    """Float or None (NaN / inf / non-numeric become None) so JSON stays valid."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def ratio(numerator, denominator, digits=2):
    """numerator / denominator, or None when it cannot be calculated."""
    a, b = num(numerator), num(denominator)
    if a is None or b is None or b <= 0:
        return None
    return round(float(np.divide(a, b)), digits)


def rounded(value, digits=2):
    value = num(value)
    return None if value is None else round(value, digits)



def validate_row(row, last_valid_odometer):
    litres, cost, odometer = row["litres"], row["total_cost"], row["odometer_km"]

    if pd.isna(litres) or litres <= 0:
        return "Litres must be greater than zero"
    if pd.isna(odometer) or odometer < 0:
        return "Odometer reading is missing or negative"
    if pd.isna(cost) or cost < 0:
        return "Total cost is missing or negative"
    if last_valid_odometer is not None and odometer <= last_valid_odometer:
        return (f"Odometer {odometer:g} km is not greater than the previous "
                f"valid reading ({last_valid_odometer:g} km)")
    return None


PAIR_COLUMNS = ["vehicle_id", "vehicle_code", "month", "distance", "litres", "cost"]
SEGMENT_COLUMNS = ["vehicle_id", "vehicle_code", "month", "start_date", "end_date",
                   "distance", "litres", "cost", "partial_fills"]


def analyze():
    rows = get_fuel_data()
    vehicles = get_vehicles()

    df = pd.DataFrame(rows)
    if not df.empty:
        df["fill_date"] = pd.to_datetime(df["fill_date"])
        for column in ("odometer_km", "litres", "total_cost"):
            df[column] = pd.to_numeric(df[column], errors="coerce")
        df["full_tank_flag"] = pd.to_numeric(df["full_tank_flag"], errors="coerce").fillna(0).astype(int)
        df["month"] = df["fill_date"].dt.strftime("%Y-%m")
        df = df.sort_values(["vehicle_id", "fill_date", "record_id"]).reset_index(drop=True)

    annotations = {}
    invalid = []
    pairs = []
    segments = []

    for _, group in (df.groupby("vehicle_id", sort=False) if not df.empty else []):
        last_valid_odometer = None
        previous = None
        anchor = None
        fuel_since = cost_since = 0.0
        partial_fills = 0

        for _, r in group.iterrows():
            record_id = int(r["record_id"])
            reason = validate_row(r, last_valid_odometer)

            if reason:
                annotations[record_id] = {"status": "invalid", "reason": reason,
                                          "distance": None, "mileage": None, "basis": None}
                invalid.append({
                    "record_id": record_id,
                    "vehicle_code": r["vehicle_code"],
                    "fill_date": r["fill_date"].strftime("%Y-%m-%d"),
                    "odometer_km": rounded(r["odometer_km"]),
                    "litres": rounded(r["litres"], 3),
                    "total_cost": rounded(r["total_cost"]),
                    "full_tank": bool(r["full_tank_flag"]),
                    "reason": reason
                })
             
                previous = anchor = None
                fuel_since = cost_since = 0.0
                partial_fills = 0
                continue

            odometer = float(r["odometer_km"])
            litres = float(r["litres"])
            cost = float(r["total_cost"])
            last_valid_odometer = odometer
            note = {"status": "valid", "reason": None,
                    "distance": None, "mileage": None, "basis": None}

            if previous is not None:
                distance = odometer - previous["odometer"]
                note["distance"] = round(distance, 2)
                note["basis"] = "Approximate"
                note["mileage"] = ratio(distance, litres)
                pairs.append({"vehicle_id": int(r["vehicle_id"]), "vehicle_code": r["vehicle_code"],
                              "month": r["month"], "distance": distance,
                              "litres": litres, "cost": cost})

            if r["full_tank_flag"] == 1:
                if anchor is not None:
                    distance = odometer - anchor["odometer"]
                    seg_litres = fuel_since + litres
                    segments.append({
                        "vehicle_id": int(r["vehicle_id"]), "vehicle_code": r["vehicle_code"],
                        "month": r["month"],
                        "start_date": anchor["date"].strftime("%Y-%m-%d"),
                        "end_date": r["fill_date"].strftime("%Y-%m-%d"),
                        "distance": distance, "litres": seg_litres,
                        "cost": cost_since + cost, "partial_fills": partial_fills
                    })
                    note["basis"] = "Full-tank"
                    note["distance"] = round(distance, 2)
                    note["mileage"] = ratio(distance, seg_litres)
                anchor = {"odometer": odometer, "date": r["fill_date"]}
                fuel_since = cost_since = 0.0
                partial_fills = 0
            elif anchor is not None:
                fuel_since += litres
                cost_since += cost
                partial_fills += 1

            annotations[record_id] = note
            previous = {"odometer": odometer}

    pairs_df = pd.DataFrame(pairs, columns=PAIR_COLUMNS)
    segments_df = pd.DataFrame(segments, columns=SEGMENT_COLUMNS)
    for frame, columns in ((pairs_df, ["distance", "litres", "cost"]),
                           (segments_df, ["distance", "litres", "cost"])):
        for column in columns:
            frame[column] = frame[column].astype(float)

    if not segments_df.empty:
        litres_arr = segments_df["litres"].to_numpy(dtype=float)
        segments_df["mileage"] = np.divide(
            segments_df["distance"].to_numpy(dtype=float), litres_arr,
            out=np.full(len(segments_df), np.nan), where=litres_arr > 0)
        cost_arr = segments_df["distance"].to_numpy(dtype=float)
        segments_df["cost_per_km"] = np.divide(
            segments_df["cost"].to_numpy(dtype=float), cost_arr,
            out=np.full(len(segments_df), np.nan), where=cost_arr > 0)
    else:
        segments_df["mileage"] = pd.Series(dtype=float)
        segments_df["cost_per_km"] = pd.Series(dtype=float)

    return df, vehicles, annotations, invalid, pairs_df, segments_df


def efficiency(seg, pr):
    """Full-tank and approximate metrics for a subset of segments / pairs."""
    ft_distance = float(seg["distance"].sum()) if len(seg) else 0.0
    ft_litres = float(seg["litres"].sum()) if len(seg) else 0.0
    ft_cost = float(seg["cost"].sum()) if len(seg) else 0.0
    ap_distance = float(pr["distance"].sum()) if len(pr) else 0.0
    ap_litres = float(pr["litres"].sum()) if len(pr) else 0.0
    ap_cost = float(pr["cost"].sum()) if len(pr) else 0.0

    return {
        "fulltank_segments": int(len(seg)),
        "fulltank_distance": round(ft_distance, 2),
        "fulltank_litres": round(ft_litres, 2),
        "fulltank_mileage": ratio(ft_distance, ft_litres),
        "fulltank_cost_per_km": ratio(ft_cost, ft_distance),
        "approx_pairs": int(len(pr)),
        "approx_distance": round(ap_distance, 2),
        "approx_mileage": ratio(ap_distance, ap_litres),
        "approx_cost_per_km": ratio(ap_cost, ap_distance),
    }


def spend_rows(frame):
    """Rows that represent a real purchase (used for expense totals)."""
    return frame[(frame["litres"] > 0) & (frame["total_cost"] >= 0)]


def calculate_analytics():
    df, vehicles, annotations, invalid, pairs_df, segments_df = analyze()

    empty_eff = efficiency(segments_df, pairs_df)
    if df.empty:
        return {
            "vehicles": [], "monthly": [], "segments": [], "invalid_readings": [],
            "monthly_vehicle_mileage": {"months": [], "vehicles": [], "values": []},
            "totals": {**empty_eff, "total_litres": 0, "total_cost": 0,
                       "avg_price_per_litre": None, "total_records": 0,
                       "valid_records": 0, "invalid_records": 0,
                       "total_vehicles": len(vehicles),
                       "total_distance": 0, "average_mileage": None, "cost_per_km": None}
        }

    spend = spend_rows(df)
    total_litres = float(spend["litres"].sum())
    total_cost = float(spend["total_cost"].sum())

    vehicle_results = []
    for vehicle in vehicles:
        vid = vehicle["vehicle_id"]
        sub = df[df["vehicle_id"] == vid]
        sub_spend = spend_rows(sub)
        eff = efficiency(segments_df[segments_df["vehicle_id"] == vid],
                         pairs_df[pairs_df["vehicle_id"] == vid])
        invalid_count = sum(1 for item in invalid if item["vehicle_code"] == vehicle["vehicle_code"])
        litres = float(sub_spend["litres"].sum())
        cost = float(sub_spend["total_cost"].sum())

        vehicle_results.append({
            "vehicle_id": int(vid),
            "vehicle_code": vehicle["vehicle_code"],
            "vehicle_type": vehicle["vehicle_type"],
            "fuel_type": vehicle["fuel_type"],
            "records": int(len(sub)),
            "invalid_records": invalid_count,
            "total_litres": round(litres, 2),
            "total_cost": round(cost, 2),
            "avg_price_per_litre": ratio(cost, litres),
            **eff,
            
            "total_distance": eff["approx_distance"],
            "average_mileage": eff["fulltank_mileage"],
        })


    ranked = sorted((v for v in vehicle_results if v["fulltank_mileage"] is not None),
                    key=lambda v: v["fulltank_mileage"], reverse=True)
    for position, item in enumerate(ranked, start=1):
        item["rank"] = position
    for item in vehicle_results:
        item.setdefault("rank", None)


    months = sorted(set(df["month"]) | set(segments_df["month"]) | set(pairs_df["month"]))
    monthly_results = []
    for month in months:
        month_spend = spend_rows(df[df["month"] == month])
        eff = efficiency(segments_df[segments_df["month"] == month],
                         pairs_df[pairs_df["month"] == month])
        litres = float(month_spend["litres"].sum())
        cost = float(month_spend["total_cost"].sum())
        monthly_results.append({
            "month": month,
            "records": int((df["month"] == month).sum()),
            "total_litres": round(litres, 2),
            "total_cost": round(cost, 2),
            "avg_price_per_litre": ratio(cost, litres),
            **eff
        })

   
    codes = [v["vehicle_code"] for v in vehicles]
    matrix = []
    for month in months:
        row = []
        for code in codes:
            seg = segments_df[(segments_df["month"] == month) & (segments_df["vehicle_code"] == code)]
            row.append(ratio(seg["distance"].sum(), seg["litres"].sum()) if len(seg) else None)
        matrix.append(row)

    segment_rows = [
        {**{k: rounded(s[k], 3) if k in ("distance", "litres", "cost") else s[k]
            for k in ("vehicle_code", "start_date", "end_date", "distance",
                      "litres", "cost", "partial_fills")},
         "mileage": rounded(s["mileage"]), "cost_per_km": rounded(s["cost_per_km"])}
        for _, s in segments_df.sort_values("end_date", ascending=False).iterrows()
    ]

    totals_eff = efficiency(segments_df, pairs_df)
    totals = {
        **totals_eff,
        "total_litres": round(total_litres, 2),
        "total_cost": round(total_cost, 2),
        "avg_price_per_litre": ratio(total_cost, total_litres),
        "total_records": int(len(df)),
        "valid_records": int(len(df) - len(invalid)),
        "invalid_records": len(invalid),
        "total_vehicles": len(vehicles),
        "total_distance": totals_eff["approx_distance"],
        "average_mileage": totals_eff["fulltank_mileage"],
        "cost_per_km": totals_eff["fulltank_cost_per_km"],
    }

    return {
        "vehicles": vehicle_results,
        "monthly": monthly_results,
        "monthly_vehicle_mileage": {"months": months, "vehicles": codes, "values": matrix},
        "segments": segment_rows,
        "invalid_readings": invalid,
        "totals": totals,
    }



@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/health")
def health():
    try:
        connection = get_db_connection()
        connection.close()
        return jsonify({"status": "connected"})
    except mysql.connector.Error as error:
        return jsonify({"error": str(error)}), 500


@app.route("/api/vehicles", methods=["GET", "POST"])
def vehicles():
    if request.method == "GET":
        return jsonify(query("SELECT * FROM vehicles ORDER BY vehicle_id DESC"))

    data = request.get_json(silent=True) or {}
    vehicle_code = str(data.get("vehicle_code", "")).strip()
    vehicle_type = str(data.get("vehicle_type", "")).strip()
    fuel_type = str(data.get("fuel_type", "")).strip()

    if not vehicle_code or not vehicle_type or not fuel_type:
        return jsonify({"error": "All vehicle fields are required."}), 400

    connection = get_db_connection()
    cursor = connection.cursor()

    try:
        cursor.execute(
            "INSERT INTO vehicles (vehicle_code, vehicle_type, fuel_type) VALUES (%s, %s, %s)",
            (vehicle_code, vehicle_type, fuel_type)
        )
        connection.commit()
        return jsonify({"message": "Vehicle added successfully."}), 201
    except mysql.connector.IntegrityError:
        return jsonify({"error": "This vehicle code already exists."}), 409
    finally:
        cursor.close()
        connection.close()


@app.route("/api/vehicles/<int:vehicle_id>", methods=["DELETE"])
def delete_vehicle(vehicle_id):
    count = query("SELECT COUNT(*) AS n FROM fuel_records WHERE vehicle_id = %s", (vehicle_id,))[0]["n"]
    if count:
        return jsonify({"error": f"This vehicle has {count} fuel record(s). Delete them first."}), 409

    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("DELETE FROM vehicles WHERE vehicle_id = %s", (vehicle_id,))
        connection.commit()
        if cursor.rowcount == 0:
            return jsonify({"error": "Vehicle not found."}), 404
        return jsonify({"message": "Vehicle deleted."})
    finally:
        cursor.close()
        connection.close()


@app.route("/api/fuel-records", methods=["GET", "POST"])
def fuel_records():
    if request.method == "GET":
        _, _, annotations, _, _, _ = analyze()
        rows = get_fuel_data()
        rows.sort(key=lambda r: (r["fill_date"], r["record_id"]), reverse=True)
        for row in rows:
            note = annotations.get(row["record_id"], {})
            row["fill_date"] = str(row["fill_date"])
            row["odometer_km"] = float(row["odometer_km"])
            row["litres"] = float(row["litres"])
            row["total_cost"] = float(row["total_cost"])
            row["full_tank_flag"] = int(row["full_tank_flag"])
            row["status"] = note.get("status")
            row["reason"] = note.get("reason")
            row["distance"] = note.get("distance")
            row["mileage"] = note.get("mileage")
            row["basis"] = note.get("basis")
        return jsonify(rows)

    data = request.get_json(silent=True) or {}

    try:
        vehicle_id = int(data["vehicle_id"])
        fill_date = date.fromisoformat(str(data["fill_date"]))
        odometer_km = float(data["odometer_km"])
        litres = float(data["litres"])
        total_cost = float(data["total_cost"])
        full_tank_flag = 1 if data.get("full_tank_flag", True) else 0
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Enter valid values in all required fields."}), 400

    if not all(math.isfinite(x) for x in (odometer_km, litres, total_cost)):
        return jsonify({"error": "Enter valid numeric values."}), 400
    if litres <= 0:
        return jsonify({"error": "Fuel quantity must be greater than zero."}), 400
    if odometer_km < 0 or total_cost < 0:
        return jsonify({"error": "Odometer reading and cost cannot be negative."}), 400
    if fill_date > date.today():
        return jsonify({"error": "Fill date cannot be in the future."}), 400

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        cursor.execute("SELECT 1 FROM vehicles WHERE vehicle_id = %s", (vehicle_id,))
        if cursor.fetchone() is None:
            return jsonify({"error": "Selected vehicle does not exist."}), 400

     
        cursor.execute(
            """SELECT fill_date, odometer_km FROM fuel_records
               WHERE vehicle_id = %s AND fill_date <= %s
               ORDER BY fill_date DESC, record_id DESC LIMIT 1""",
            (vehicle_id, fill_date))
        previous = cursor.fetchone()

        cursor.execute(
            """SELECT fill_date, odometer_km FROM fuel_records
               WHERE vehicle_id = %s AND fill_date > %s
               ORDER BY fill_date ASC, record_id ASC LIMIT 1""",
            (vehicle_id, fill_date))
        following = cursor.fetchone()

        if previous and odometer_km <= float(previous["odometer_km"]):
            return jsonify({"error": (
                f"Odometer must be greater than the previous reading "
                f"({float(previous['odometer_km']):g} km on {previous['fill_date']}). "
                f"If that earlier reading is wrong, delete or correct it first.")}), 400

        if following and odometer_km >= float(following["odometer_km"]):
            return jsonify({"error": (
                f"Odometer must be less than the next reading "
                f"({float(following['odometer_km']):g} km on {following['fill_date']}). "
                f"Readings must follow date order.")}), 400

        cursor.execute(
            """
            INSERT INTO fuel_records
            (vehicle_id, fill_date, odometer_km, litres, total_cost, full_tank_flag)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (vehicle_id, fill_date, odometer_km, litres, total_cost, full_tank_flag)
        )
        connection.commit()
        return jsonify({"message": "Fuel record added successfully."}), 201
    finally:
        cursor.close()
        connection.close()


@app.route("/api/fuel-records/<int:record_id>", methods=["DELETE"])
def delete_fuel_record(record_id):
    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("DELETE FROM fuel_records WHERE record_id = %s", (record_id,))
        connection.commit()
        if cursor.rowcount == 0:
            return jsonify({"error": "Record not found."}), 404
        return jsonify({"message": "Fuel record deleted."})
    finally:
        cursor.close()
        connection.close()


@app.route("/api/analytics")
def analytics():
    try:
        return jsonify(calculate_analytics())
    except Exception as error:
        app.logger.exception("Analytics error")
        return jsonify({"error": str(error)}), 500


EXPORTS = {
    "vehicle-mileage": ("vehicles", "vehicle_mileage_report.csv"),
    "monthly-summary": ("monthly", "monthly_fuel_summary.csv"),
    "invalid-readings": ("invalid_readings", "invalid_readings_report.csv"),
    "full-tank-segments": ("segments", "full_tank_segments.csv"),
}


@app.route("/api/export/<name>.csv")
def export_csv(name):
    if name not in EXPORTS:
        return jsonify({"error": "Unknown report."}), 404
    key, filename = EXPORTS[name]
    try:
        frame = pd.DataFrame(calculate_analytics()[key])
        return Response(frame.to_csv(index=False), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={filename}"})
    except Exception as error:
        app.logger.exception("Export error")
        return jsonify({"error": str(error)}), 500


def figure_to_base64(fig):
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=120, bbox_inches="tight",
                pad_inches=0.25, facecolor="white")
    plt.close(fig)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


# ---------------------------------------------------------------------------
# Chart styling: white cards, teal/navy palette matching the dashboard
# ---------------------------------------------------------------------------
CHART_COLORS = {"navy": "#102a43", "muted": "#526575", "amber": "#e0a030"}
SERIES_COLORS = ["#146b67", "#e0a030", "#2e86ab", "#c0392b", "#7d5ba6", "#5b8c3a", "#d35400"]
TEAL_SCALE = LinearSegmentedColormap.from_list("teal_scale", ["#8bd3c7", "#146b67", "#0d514e"])

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "font.size": 10,
    "figure.facecolor": "white",
    "axes.facecolor": "white",
    "axes.edgecolor": "#d5dee5",
    "axes.linewidth": 0.8,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "axes.axisbelow": True,
    "axes.titlesize": 13,
    "axes.titleweight": "bold",
    "axes.titlecolor": CHART_COLORS["navy"],
    "axes.titlepad": 14,
    "axes.labelcolor": CHART_COLORS["muted"],
    "axes.labelsize": 10,
    "grid.color": "#e6edf2",
    "grid.linewidth": 0.8,
    "xtick.color": CHART_COLORS["muted"],
    "ytick.color": CHART_COLORS["muted"],
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
    "legend.frameon": False,
    "legend.fontsize": 9,
})


def to_array(values):
    """List with None -> float array with NaN for missing values."""
    return np.array([np.nan if v is None else v for v in values], dtype=float)


def style_axes(ax, title, ylabel, decimals=0):
    ax.set_title(title, loc="left")
    ax.set_ylabel(ylabel)
    ax.xaxis.grid(False)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:,.{decimals}f}"))


def empty_state(ax, message):
    ax.set_xticks([])
    ax.set_yticks([])
    ax.grid(False)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.text(0.5, 0.5, message, ha="center", va="center", fontsize=11,
            color=CHART_COLORS["muted"], transform=ax.transAxes)


def bar_chart(labels, values, title, ylabel, decimals=0):
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    data = to_array(values)

    if len(labels) and np.isfinite(data).any():
        x = np.arange(len(labels))
        heights = np.where(np.isfinite(data), data, 0.0)
        colors = TEAL_SCALE(np.linspace(0.2, 0.9, len(labels)))
        bars = ax.bar(x, heights, width=0.62, color=colors, zorder=3)

        top = float(np.nanmax(data))
        ax.set_ylim(0, (top if top > 0 else 1) * 1.3)
        ax.bar_label(bars, labels=[f"{v:,.{decimals}f}" if np.isfinite(v) else "" for v in data],
                     padding=3, fontsize=8.5, color=CHART_COLORS["navy"], fontweight="semibold")

        if len(labels) > 1:
            average = float(np.nanmean(data))
            ax.axhline(average, color=CHART_COLORS["amber"], linestyle=(0, (4, 3)),
                       linewidth=1.4, zorder=4)
            ax.legend(handles=[Line2D([], [], color=CHART_COLORS["amber"], linestyle="--",
                                      label=f"Average: {average:,.{decimals}f}")],
                      loc="upper right")

        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=30, ha="right")
    else:
        empty_state(ax, "No data available")

    style_axes(ax, title, ylabel, decimals)
    fig.tight_layout()
    return figure_to_base64(fig)


def grouped_bar_chart(labels, series, title, ylabel, decimals=1):
    """series: {legend label: values}"""
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    arrays = {name: to_array(vals) for name, vals in series.items()}

    if len(labels) and any(np.isfinite(a).any() for a in arrays.values()):
        x = np.arange(len(labels))
        width = 0.8 / len(arrays)
        top = max(float(np.nanmax(a)) for a in arrays.values() if np.isfinite(a).any())
        ax.set_ylim(0, (top if top > 0 else 1) * 1.3)

        for i, (name, data) in enumerate(arrays.items()):
            offset = x + i * width - 0.4 + width / 2
            bars = ax.bar(offset, np.where(np.isfinite(data), data, 0.0), width * 0.92,
                          label=name, color=SERIES_COLORS[i % len(SERIES_COLORS)], zorder=3)
            ax.bar_label(bars, labels=[f"{v:,.{decimals}f}" if np.isfinite(v) else "" for v in data],
                         padding=2, fontsize=8, color=CHART_COLORS["navy"], fontweight="semibold")

        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=30, ha="right")
        ax.legend(loc="upper right", ncol=len(arrays))
    else:
        empty_state(ax, "No data available")

    style_axes(ax, title, ylabel, decimals)
    fig.tight_layout()
    return figure_to_base64(fig)


def line_chart(months, codes, values, title, ylabel):
    fig, ax = plt.subplots(figsize=(8.4, 4.4))
    matrix = np.array([[np.nan if v is None else v for v in row] for row in values], dtype=float)

    if matrix.size and np.isfinite(matrix).any():
        x = np.arange(len(months))
        for index, code in enumerate(codes):
            column = matrix[:, index]
            if np.isfinite(column).any():
                ax.plot(x, column, color=SERIES_COLORS[index % len(SERIES_COLORS)],
                        linewidth=2.4, marker="o", markersize=7, markerfacecolor="white",
                        markeredgewidth=2, label=code, zorder=3)
        ax.set_xticks(x)
        ax.set_xticklabels(months, rotation=30, ha="right")
        ax.legend(title="Vehicle", loc="upper left", bbox_to_anchor=(1.01, 1))
    else:
        empty_state(ax, "No full-tank mileage available yet")

    style_axes(ax, title, ylabel, decimals=1)
    fig.tight_layout()
    return figure_to_base64(fig)


@app.route("/api/analytics/charts")
def analytics_charts():
    try:
        data = calculate_analytics()
        monthly, vehicle_rows = data["monthly"], data["vehicles"]
        pivot = data["monthly_vehicle_mileage"]
        month_labels = [row["month"] for row in monthly]
        codes = [row["vehicle_code"] for row in vehicle_rows]

        charts = {
            "monthly_cost": bar_chart(month_labels, [r["total_cost"] for r in monthly],
                                      "Monthly Fuel Cost", "Cost (₹)", decimals=0),
            "monthly_fuel": bar_chart(month_labels, [r["total_litres"] for r in monthly],
                                      "Monthly Fuel Consumption", "Fuel (litres)", decimals=1),
            "monthly_mileage": line_chart(pivot["months"], pivot["vehicles"], pivot["values"],
                                          "Monthly Mileage Trend (full-tank)", "km per litre"),
            "vehicle_mileage": grouped_bar_chart(
                codes,
                {"Full-tank (accurate)": [r["fulltank_mileage"] for r in vehicle_rows],
                 "Approximate": [r["approx_mileage"] for r in vehicle_rows]},
                "Vehicle Mileage Comparison", "km per litre", decimals=1),
            "vehicle_cost_per_km": bar_chart(codes, [r["fulltank_cost_per_km"] for r in vehicle_rows],
                                             "Fuel Cost per km (full-tank)", "₹ per km", decimals=2),
            "vehicle_cost": bar_chart(codes, [r["total_cost"] for r in vehicle_rows],
                                      "Vehicle-wise Fuel Cost", "Cost (₹)", decimals=0),
        }
        return jsonify(charts)
    except Exception as error:
        app.logger.exception("Chart generation error")
        return jsonify({"error": str(error)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5001)