import os
import re
import base64
import tempfile
from datetime import datetime
from dateutil.relativedelta import relativedelta

import pdfplumber
import pandas as pd
from flask import Flask, request, jsonify

app = Flask(__name__)

API_KEY = os.environ.get("WEEKLY_STOCK_API_KEY", "")

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12
}

def clean(x):
    return re.sub(r"\s+", " ", str(x or "").replace("\n", " ")).strip()

def parse_expiry_date(x):
    x = clean(x).upper()
    m = re.search(r"(\d{2})-([A-Z]{3})-(\d{2,4})", x)
    if not m:
        return None

    year = int(m.group(3))
    if year < 100:
        year += 2000

    return datetime(year, MONTHS[m.group(2)], int(m.group(1)))

def months_between(start_date, end_date):
    rd = relativedelta(end_date, start_date)
    return round(rd.years * 12 + rd.months + (rd.days / 30), 1)

def extract_document_date(text):
    m = re.search(r"(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2})", text)
    if not m:
        return None, ""

    dt = datetime.strptime(m.group(1), "%d/%m/%Y")
    return dt, f"{m.group(1)} {m.group(2)}"

def analyse_pdf_file(pdf_path):
    rows = []
    document_date = None
    document_datetime_text = ""

    with pdfplumber.open(pdf_path) as pdf:
        first_text = pdf.pages[0].extract_text() or ""
        document_date, document_datetime_text = extract_document_date(first_text)

        if document_date is None:
            raise ValueError("Document date not found in PDF header.")

        for page_no, page in enumerate(pdf.pages, start=1):
            tables = page.extract_tables()

            for table in tables:
                for r in table:
                    if not r or len(r) < 10:
                        continue

                    seq = clean(r[0])
                    item_code = clean(r[1])
                    item_desc = clean(r[2])
                    expiry = clean(r[5])
                    qty = clean(r[8])

                    if not seq.isdigit():
                        continue
                    if not item_code or not item_desc:
                        continue

                    expiry_dates = re.findall(r"\d{2}-[A-Z]{3}-\d{2,4}", expiry.upper())
                    qty_values = re.findall(r"-?\d+(?:\.\d+)?", qty.replace(",", ""))

                    if not expiry_dates or not qty_values:
                        continue

                    max_len = max(len(expiry_dates), len(qty_values))

                    for i in range(max_len):
                        exp_text = expiry_dates[i] if i < len(expiry_dates) else expiry_dates[-1]
                        qty_text = qty_values[i] if i < len(qty_values) else qty_values[-1]

                        exp_date = parse_expiry_date(exp_text)
                        if not exp_date:
                            continue

                        rows.append({
                            "Item Code": item_code,
                            "Item Description": item_desc,
                            "Expiry Date": exp_date,
                            "Quantity": float(qty_text),
                            "Page": page_no
                        })

    if not rows:
        raise ValueError("No item expiry/quantity records found from PDF.")

    raw_df = pd.DataFrame(rows)

    summary_rows = []

    for (item_code, item_desc), g in raw_df.groupby(["Item Code", "Item Description"], sort=False):
        total_qty = int(round(g["Quantity"].sum()))
        nearest_date = g["Expiry Date"].min()
        nearest_qty = int(round(g.loc[g["Expiry Date"] == nearest_date, "Quantity"].sum()))
        months_to_expiry = months_between(document_date, nearest_date)

        summary_rows.append({
            "Item Code": item_code,
            "Item Description": item_desc,
            "Item Total Quantity": total_qty,
            "Nearest Expiry Date": nearest_date.strftime("%Y-%m-%d"),
            "Quantity of the Nearest Expiry Date": nearest_qty,
            "Months to the Nearest Expiry Date": months_to_expiry
        })

    return {
        "documentDate": document_date.strftime("%Y-%m-%d"),
        "documentDateDisplay": document_date.strftime("%d/%m/%Y"),
        "documentDateTime": document_datetime_text,
        "itemCount": len(summary_rows),
        "items": summary_rows
    }

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "ok",
        "message": "Weekly Stock Analysis backend is running."
    })

@app.route("/analyse", methods=["POST"])
def analyse():
    if API_KEY:
        received_key = request.headers.get("X-API-Key", "")
        if received_key != API_KEY:
            return jsonify({
                "success": False,
                "message": "Unauthorised request."
            }), 401

    try:
        data = request.get_json(silent=True)

        if not data or "pdfBase64" not in data:
            return jsonify({
                "success": False,
                "message": "Missing pdfBase64."
            }), 400

        pdf_base64 = data["pdfBase64"]

        if "," in pdf_base64:
            pdf_base64 = pdf_base64.split(",", 1)[1]

        pdf_bytes = base64.b64decode(pdf_base64)

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name

        result = analyse_pdf_file(tmp_path)

        try:
            os.remove(tmp_path)
        except Exception:
            pass

        return jsonify({
            "success": True,
            "message": "Analysis completed.",
            **result
        })

    except Exception as e:
        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
