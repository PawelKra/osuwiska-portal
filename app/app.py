"""
Portal Osuwisk – dashboard Dash z mapą Leaflet.
"""

import os
import json
import re
import math

import dash
from dash import html, dcc, Input, Output, State, ALL, no_update, ctx
import dash_leaflet as dl
import pandas as pd
from sqlalchemy import create_engine, text

# ── DB ───────────────────────────────────────────────────────────────────────

DB_URL = "postgresql://{user}:{password}@{host}:{port}/{db}".format(
    user=os.environ.get("DB_USER", "osuwiska"),
    password=os.environ.get("DB_PASSWORD", "osuwiska_pass"),
    host=os.environ.get("DB_HOST", "db"),
    port=os.environ.get("DB_PORT", "5432"),
    db=os.environ.get("DB_NAME", "osuwiska"),
)

engine = create_engine(DB_URL, pool_pre_ping=True, pool_size=3, max_overflow=5)

# ── Column discovery ─────────────────────────────────────────────────────────

def _find_col(cols, *candidates):
    lo = {c.lower(): c for c in cols}
    for c in candidates:
        if c.lower() in lo:
            return lo[c.lower()]
    return None


try:
    with engine.connect() as _c:
        _rows = _c.execute(
            text("SELECT column_name FROM information_schema.columns WHERE table_name='osuwiska_pl'")
        ).fetchall()
    _osuwiska_cols = [r[0] for r in _rows]
    COL_AREA   = _find_col(_osuwiska_cols, "OS_POWIERZCHNIA", "shape_area", "area")
    COL_MON    = _find_col(_osuwiska_cols, "MONITORING_OPIS", "monitoring_opis", "monitoring")
    COL_STOP_A = _find_col(_osuwiska_cols, "STOP_A", "stop_a")
    COL_STOP_O = _find_col(_osuwiska_cols, "STOP_O", "stop_o")
    COL_STOP_N = _find_col(_osuwiska_cols, "STOP_N", "stop_n")
    COL_MIAZSZ = _find_col(_osuwiska_cols, "KO_MIAZSZOSC_POM", "ko_miazszosc_pom")
    print(f"[startup] cols: area={COL_AREA} mon={COL_MON} stop_a={COL_STOP_A} ko={COL_MIAZSZ}")
except Exception as _e:
    print(f"[startup] WARNING column discovery failed: {_e}")
    _osuwiska_cols = []
    COL_AREA = COL_MON = COL_STOP_A = COL_STOP_O = COL_STOP_N = COL_MIAZSZ = None

# ── Ensure gid ───────────────────────────────────────────────────────────────

try:
    with engine.connect() as _c:
        _c.execute(text("""
            DO $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name='osuwiska_pl' AND column_name='gid'
                ) THEN
                    ALTER TABLE osuwiska_pl ADD COLUMN gid SERIAL;
                    CREATE INDEX osuwiska_pl_gid_idx ON osuwiska_pl (gid);
                END IF;
            END $$;
        """))
        _c.commit()
    print("[startup] gid column ready on osuwiska_pl")
except Exception as _e:
    print(f"[startup] WARNING gid ensure failed: {_e}")

# ── Species colors (LP palette – keys match DB uppercase codes) ───────────────

SPECIES_COLORS = {
    "SO": "#DEB887", "MD": "#DC9F63", "LB": "#D68640",
    "SW": "#D5D2FE", "ŚW": "#D5D2FE",
    "JD": "#87CEFA", "DG": "#52AFFC",
    "DB": "#D2D2D2",
    "BK": "#FEEAB4", "GB": "#FED45A",
    "JS": "#BCE889", "KL": "#99BB68", "WZ": "#769348", "JW": "#769348",
    "OL": "#DDFEDD",
    "BRZ": "#D2FEFB", "AK": "#86F3EF",
    "TP": "#FFE1E1", "OS": "#FFB4B4", "WB": "#FF8787", "LP": "#FF8787",
}
DEFAULT_COLOR = "#AAAAAA"


def get_species_color(species_cd):
    """Case-insensitive lookup; handles subspecies like DB.C → DB."""
    if not species_cd:
        return DEFAULT_COLOR
    sp = str(species_cd).strip().upper()
    if sp in SPECIES_COLORS:
        return SPECIES_COLORS[sp]
    base = sp.split(".")[0]
    return SPECIES_COLORS.get(base, DEFAULT_COLOR)

# ── DB helpers ────────────────────────────────────────────────────────────────

def get_nadl_options():
    with engine.connect() as conn:
        df = pd.read_sql(
            text("SELECT DISTINCT nadlesnictwo_name FROM g_subarea ORDER BY nadlesnictwo_name"),
            conn,
        )
    return [{"label": r, "value": r} for r in df["nadlesnictwo_name"]]


def get_inspectorate_geojson(nadl_name):
    sql = text("""
        SELECT ST_AsGeoJSON(ST_Transform(gi.geometry, 4326)) AS geojson
        FROM g_inspectorate gi
        WHERE ST_Contains(gi.geometry, (
            SELECT ST_Centroid(ST_Union(s.geometry))
            FROM g_subarea s WHERE s.nadlesnictwo_name = :nadl
        ))
        LIMIT 1
    """)
    with engine.connect() as conn:
        row = conn.execute(sql, {"nadl": nadl_name}).fetchone()
    if not row:
        return None
    return {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "geometry": json.loads(row[0]), "properties": {}}],
    }


def get_landslides(nadl_name):
    opt_parts = [f'o."{c}"' for c in filter(None, [COL_AREA, COL_MON, COL_STOP_A, COL_STOP_O, COL_STOP_N, COL_MIAZSZ])]
    opt_select = (", " + ", ".join(opt_parts)) if opt_parts else ""

    sql = text(f"""
        WITH nadl AS (
            SELECT ST_Union(geometry) AS geom FROM g_subarea WHERE nadlesnictwo_name = :nadl
        )
        SELECT
            o.gid
            {opt_select},
            ST_Area(o.geometry)                           AS area_m2,
            ST_AsGeoJSON(ST_Transform(o.geometry, 4326)) AS geojson_wgs84,
            ST_XMin(ST_Transform(o.geometry, 4326))      AS xmin,
            ST_YMin(ST_Transform(o.geometry, 4326))      AS ymin,
            ST_XMax(ST_Transform(o.geometry, 4326))      AS xmax,
            ST_YMax(ST_Transform(o.geometry, 4326))      AS ymax,
            COALESCE((
                SELECT ROUND(
                    SUM(ST_Area(ST_Intersection(o.geometry, s.geometry))) /
                    NULLIF(ST_Area(o.geometry), 0) * 100
                )::int
                FROM g_subarea s
                WHERE ST_Intersects(o.geometry, s.geometry)
                  AND s.nadlesnictwo_name = :nadl
            ), 0) AS forest_pct
        FROM osuwiska_pl o, nadl n
        WHERE ST_Intersects(o.geometry, n.geom)
        ORDER BY area_m2 DESC
        LIMIT 200
    """)
    with engine.connect() as conn:
        return pd.read_sql(sql, conn, params={"nadl": nadl_name})


def get_landslide_at_point(lat, lng, nadl_name):
    sql = text("""
        WITH pt AS (
            SELECT ST_Transform(ST_SetSRID(ST_Point(:lng, :lat), 4326), 2180) AS geom
        ),
        nadl AS (
            SELECT ST_Union(geometry) AS geom
            FROM g_subarea
            WHERE nadlesnictwo_name = :nadl
        )
        SELECT
            o.gid,
            ST_XMin(ST_Transform(o.geometry, 4326)) AS xmin,
            ST_YMin(ST_Transform(o.geometry, 4326)) AS ymin,
            ST_XMax(ST_Transform(o.geometry, 4326)) AS xmax,
            ST_YMax(ST_Transform(o.geometry, 4326)) AS ymax,
            ST_Area(o.geometry) AS area_m2
        FROM osuwiska_pl o, pt, nadl n
        WHERE ST_Intersects(o.geometry, pt.geom)
          AND ST_Intersects(o.geometry, n.geom)
        ORDER BY area_m2 ASC
        LIMIT 1
    """)
    with engine.connect() as conn:
        return pd.read_sql(sql, conn, params={"lat": lat, "lng": lng, "nadl": nadl_name})


def get_subareas(landslide_gid, nadl_name):
    sql = text("""
        SELECT
            s.*,
            ST_AsGeoJSON(ST_Transform(s.geometry, 4326)) AS geojson_wgs84,
            ST_XMin(ST_Transform(s.geometry, 4326))      AS xmin,
            ST_YMin(ST_Transform(s.geometry, 4326))      AS ymin,
            ST_XMax(ST_Transform(s.geometry, 4326))      AS xmax,
            ST_YMax(ST_Transform(s.geometry, 4326))      AS ymax
        FROM g_subarea s
        JOIN osuwiska_pl o ON o.gid = :gid
        WHERE ST_Intersects(s.geometry, o.geometry)
          AND s.nadlesnictwo_name = :nadl
    """)
    with engine.connect() as conn:
        return pd.read_sql(sql, conn, params={"gid": int(landslide_gid), "nadl": nadl_name})


def get_subarea_at_point(lat, lng, nadl_name):
    """Point-in-polygon query — used because dl 1.x GeoJSON clickData has only latlng."""
    sql = text("""
        SELECT * FROM g_subarea
        WHERE ST_Intersects(geometry,
              ST_Transform(ST_SetSRID(ST_Point(:lng, :lat), 4326), 2180))
          AND nadlesnictwo_name = :nadl
        ORDER BY sub_area
        LIMIT 1
    """)
    with engine.connect() as conn:
        return pd.read_sql(sql, conn, params={"lat": lat, "lng": lng, "nadl": nadl_name})


def get_subarea_at_point_for_landslide(lat, lng, landslide_gid, nadl_name):
    sql = text("""
        WITH pt AS (
            SELECT ST_Transform(ST_SetSRID(ST_Point(:lng, :lat), 4326), 2180) AS geom
        )
        SELECT s.*
        FROM g_subarea s
        JOIN osuwiska_pl o ON o.gid = :gid
        JOIN pt ON TRUE
        WHERE ST_Intersects(s.geometry, pt.geom)
          AND ST_Intersects(s.geometry, o.geometry)
          AND s.nadlesnictwo_name = :nadl
        ORDER BY s.sub_area
        LIMIT 1
    """)
    with engine.connect() as conn:
        return pd.read_sql(
            sql,
            conn,
            params={"lat": lat, "lng": lng, "gid": int(landslide_gid), "nadl": nadl_name},
        )


def get_subarea_by_a_i_num(a_i_num, nadl_name):
    sql = text("""
        SELECT * FROM g_subarea
        WHERE a_i_num = :a_i_num
          AND nadlesnictwo_name = :nadl
        LIMIT 1
    """)
    with engine.connect() as conn:
        return pd.read_sql(sql, conn, params={"a_i_num": int(a_i_num), "nadl": nadl_name})


def latlng_from_click_data(click_data):
    latlng = (click_data or {}).get("latlng")
    if isinstance(latlng, dict):
        lat = latlng.get("lat")
        lng = latlng.get("lng")
        if lat is not None and lng is not None:
            return float(lat), float(lng)
    if isinstance(latlng, (list, tuple)) and len(latlng) >= 2:
        return float(latlng[0]), float(latlng[1])
    return None


def get_storey_species(a_i_num):
    if a_i_num is None or pd.isna(a_i_num):
        return pd.DataFrame()
    sql = text("""
        SELECT * FROM f_storey_species
        WHERE arodes_int_num = :anum
        ORDER BY
            CASE WHEN storey_cd IS NOT NULL AND LEFT(LOWER(storey_cd), 1) = 'd' THEN 0 ELSE 1 END,
            sp_rank_order_act ASC NULLS LAST
    """)
    with engine.connect() as conn:
        return pd.read_sql(sql, conn, params={"anum": int(a_i_num)})


def bbox_to_geojson_rect(ymin, xmin, ymax, xmax, padding=0.0):
    """Padded bounding-box rectangle as a minimal GeoJSON — used for zoomToBounds."""
    dy = (ymax - ymin) * padding / 2
    dx = (xmax - xmin) * padding / 2
    coords = [[
        [xmin - dx, ymin - dy], [xmax + dx, ymin - dy],
        [xmax + dx, ymax + dy], [xmin - dx, ymax + dy],
        [xmin - dx, ymin - dy],
    ]]
    return {
        "type": "FeatureCollection",
        "features": [{"type": "Feature",
                       "geometry": {"type": "Polygon", "coordinates": coords},
                       "properties": {}}],
    }


def bbox_to_view(ymin, xmin, ymax, xmax, padding=0.0):
    """Convert WGS84 bbox → (center, zoom) for dl.Map center/zoom outputs."""
    lp = padding / 2
    y1, y2 = ymin - (ymax - ymin) * lp, ymax + (ymax - ymin) * lp
    x1, x2 = xmin - (xmax - xmin) * lp, xmax + (xmax - xmin) * lp
    center = [(y1 + y2) / 2, (x1 + x2) / 2]
    lat_span = max(y2 - y1, 1e-9)
    lng_span = max(x2 - x1, 1e-9)
    zoom = max(1, min(18, int(min(math.log2(170 / lat_span), math.log2(360 / lng_span)))))
    return center, zoom


def df_to_view(df, padding=0.0):
    if df.empty:
        return no_update, no_update
    center, zoom = bbox_to_view(
        float(df["ymin"].min()), float(df["xmin"].min()),
        float(df["ymax"].max()), float(df["xmax"].max()),
        padding=padding,
    )
    return center, zoom


def get_nadl_bbox(nadl_name):
    sql = text("""
        SELECT
            MIN(ST_YMin(ST_Transform(geometry, 4326))) AS ymin,
            MIN(ST_XMin(ST_Transform(geometry, 4326))) AS xmin,
            MAX(ST_YMax(ST_Transform(geometry, 4326))) AS ymax,
            MAX(ST_XMax(ST_Transform(geometry, 4326))) AS xmax
        FROM g_subarea WHERE nadlesnictwo_name = :nadl
    """)
    with engine.connect() as conn:
        r = conn.execute(sql, {"nadl": nadl_name}).fetchone()
    if r and r[0] is not None:
        return float(r[0]), float(r[1]), float(r[2]), float(r[3])
    return None

# ── GeoJSON builders ──────────────────────────────────────────────────────────

def landslides_to_geojson(df):
    features = [
        {
            "type": "Feature",
            "geometry": json.loads(row["geojson_wgs84"]),
            "properties": {"gid": int(row["gid"])},
        }
        for _, row in df.iterrows()
    ]
    return {"type": "FeatureCollection", "features": features}


_SUBAREA_STYLE_FN  = {"variable": "dashExtensions.default.subareaStyle"}
_SUBAREA_ONEACH_FN = {"variable": "dashExtensions.default.onEachSubarea"}


def subareas_to_geojson(df):
    """Single FeatureCollection with per-feature style in properties."""
    features = []
    for _, row in df.iterrows():
        sp = str(row.get("species_cd") or "").strip().upper() or "_"
        color = get_species_color(sp)
        features.append({
            "type": "Feature",
            "geometry": json.loads(row["geojson_wgs84"]),
            "properties": {
                "a_i_num": None if pd.isna(row.get("a_i_num")) else int(row.get("a_i_num")),
                "adr_for": "" if pd.isna(row.get("adr_for")) else str(row.get("adr_for")),
                "species_cd": sp,
                "style": {"fillColor": color, "color": "#555",
                           "weight": 0.8, "fillOpacity": 0.7},
            },
        })
    return {"type": "FeatureCollection", "features": features}


def subareas_to_border_geojson(df):
    """FeatureCollection for the yellow-border overlay (no fill)."""
    features = [
        {"type": "Feature", "geometry": json.loads(row["geojson_wgs84"]), "properties": {}}
        for _, row in df.iterrows()
    ]
    return {"type": "FeatureCollection", "features": features}

# ── UI component builders ─────────────────────────────────────────────────────

def _val(row, col, default=""):
    if col is None:
        return default
    v = row.get(col, default)
    if v is None:
        return default
    try:
        if pd.isna(v):
            return default
    except Exception:
        pass
    s = str(v).strip()
    return default if s in ("nan", "None", "NAN", "<NA>") else s


def _fmt_num(val_str):
    """Format a numeric string: drop trailing .0, keep 1 decimal otherwise."""
    if not val_str:
        return ""
    try:
        f = float(val_str)
        return str(int(f)) if f == int(f) else f"{f:.1f}"
    except Exception:
        return val_str


def empty_feature_collection():
    return {"type": "FeatureCollection", "features": []}


def filter_landslides_df(df, filters):
    filters = set(filters or [])
    out = df.copy()

    if "active" in filters:
        if COL_STOP_A and COL_STOP_A in out.columns:
            active = pd.to_numeric(out[COL_STOP_A], errors="coerce").fillna(0) > 0
            out = out[active]
        else:
            out = out.iloc[0:0]

    if "monitoring" in filters:
        if COL_MON and COL_MON in out.columns:
            mon = out[COL_MON].fillna("").astype(str).str.strip().str.rstrip(".,").str.upper()
            out = out[(mon != "") & (mon != "NIE")]
        else:
            out = out.iloc[0:0]

    if "forest50" in filters:
        out = out[pd.to_numeric(out["forest_pct"], errors="coerce").fillna(0) >= 50]

    return out


def make_landslide_item(i, row):
    area_ha = float(row.get("area_m2") or 0) / 10000
    forest_pct = int(row.get("forest_pct") or 0)

    mon = _val(row, COL_MON)
    mon_clean = mon.strip().rstrip(".,").upper() if mon else ""
    mon_badge = html.Span()
    if mon and mon_clean not in ("NIE", ""):
        mon_badge = html.Span("M", title=f"Monitoring: {mon}", style={
            "background": "#e67e00", "color": "#fff", "borderRadius": "3px",
            "padding": "0 3px", "fontSize": "10px", "fontWeight": "bold", "cursor": "help",
        })

    def dot(color, col, title):
        v = _val(row, col)
        if not v:
            return html.Span()
        try:
            pct = int(float(v))
        except Exception:
            return html.Span()
        if pct == 0:
            return html.Span()
        return html.Span(
            [html.Span("●", style={"color": color, "fontSize": "20px", "lineHeight": "10px"}), f"{pct}%"],
            title=f"{title}: {pct}%",
            style={"fontSize": "10px", "marginRight": "3px", "whiteSpace": "nowrap"},
        )

    ko = _fmt_num(_val(row, COL_MIAZSZ))

    r1 = html.Div([
        html.Span(f"{area_ha:.2f} ha",
                  title="Powierzchnia osuwiska",
                  style={"fontWeight": "bold", "fontSize": "12px", "flex": "1"}),
        html.Span(f"🌲{forest_pct}%",
                  title=f"Udział powierzchni leśnej: {forest_pct}%",
                  style={"fontSize": "10px", "color": "#2d6a2d", "marginRight": "3px"}),
        mon_badge,
    ], style={"display": "flex", "alignItems": "center", "gap": "2px"})

    r2 = html.Div([
        html.Div([
            dot("#27ae60", COL_STOP_A, "Obszar aktywny"),
            dot("#e67e00", COL_STOP_O, "Obszar okresowo aktywny"),
            dot("#c0392b", COL_STOP_N, "Obszar nieaktywny"),
        ], style={"display": "flex", "flex": "1"}),
        html.Span([ko, " 📏"] if ko else "",
                  title="Miąższość koluwiów" if ko else None,
                  style={"fontSize": "10px", "color": "#555"}),
    ], style={"display": "flex", "alignItems": "center"})

    return html.Div([r1, r2],
                    id={"type": "landslide-item", "index": i},
                    n_clicks=0,
                    style={"padding": "5px 8px 5px 8px",
                           "paddingRight": "18px",
                           "borderBottom": "1px solid #e8e8e8",
                           "cursor": "pointer"})


SUBAREA_LABELS = {
    "adr_for": "Adres leśny",
    "nadlesnictwo_name": "Nadleśnictwo",
    "a_i_num": "Nr wydzielenia",
    "area_type": "Typ powierzchni",
    "site_type": "Typ siedliskowy",
    "silvicult": "Gospodarka leśna",
    "forest_fun": "Funkcja lasu",
    "stand_stru": "Budowa drzewostanu",
    "rotat_age": "Wiek rębności",
    "sub_area": "Pododdział",
    "prot_categ": "Kat. ochronna",
    "species_cd": "Gatunek panujący",
    "part_cd": "Kod części",
    "spec_age": "Wiek gatunku",
}
SKIP_COLS = {"geometry", "geojson_wgs84", "xmin", "ymin", "xmax", "ymax", "index"}


def make_subarea_attrs(series):
    rows_html = []
    for col in series.index:
        if col in SKIP_COLS:
            continue
        v = series[col]
        if v is None:
            continue
        try:
            if pd.isna(v):
                continue
        except Exception:
            pass
        s = str(v).strip()
        if s in ("nan", "None", "NAN", "<NA>", ""):
            continue
        label = SUBAREA_LABELS.get(col, col)
        rows_html.append(html.Tr([
            html.Td(label, style={"color": "#666", "paddingRight": "8px", "fontSize": "11px",
                                  "whiteSpace": "nowrap", "verticalAlign": "top"}),
            html.Td(s, style={"fontWeight": "bold", "fontSize": "11px"}),
        ]))
    if not rows_html:
        return html.Div("Brak danych.", style={"color": "#888", "fontStyle": "italic"})
    return html.Table(rows_html, style={"borderCollapse": "collapse", "width": "100%"})


STOREY_COLS = [
    ("storey_cd", "Piętro"),
    ("species_cd", "Gatunek"),
    ("part_cd_act", "Udział"),
    ("sp_rank_order_act", "Kol."),
    ("species_age", "Wiek"),
    ("height", "Wys."),
    ("bhd", "BHD"),
    ("volume", "Miąższ."),
]


def make_storey_table(df):
    if df.empty:
        return html.Div("Brak danych gatunków.", style={"color": "#888", "fontStyle": "italic"})
    disp = [(c, l) for c, l in STOREY_COLS if c in df.columns]
    th_s = {"fontSize": "10px", "padding": "2px 5px",
             "background": "#f0f2f0", "borderBottom": "1px solid #ccc", "whiteSpace": "nowrap"}
    header = html.Tr([html.Th(l, style=th_s) for _, l in disp])
    body = []
    for _, row in df.iterrows():
        tds = []
        for col, _ in disp:
            v = row.get(col)
            s = "" if (v is None or str(v) in ("nan", "None", "<NA>")) else str(v)
            if col == "species_age" and s:
                s = _fmt_num(s)
            tds.append(html.Td(s, style={"fontSize": "10px", "padding": "2px 5px",
                                          "borderBottom": "1px solid #eee"}))
        body.append(html.Tr(tds))
    table = html.Table([html.Thead(header), html.Tbody(body)],
                       style={"borderCollapse": "collapse", "width": "100%"})

    if {"storey_cd", "part_cd_act"}.issubset(df.columns):
        drzew = df[df["storey_cd"].astype(str).str.strip().str.upper().eq("DRZEW")]
        vals = pd.to_numeric(drzew["part_cd_act"].astype(str).str.strip(), errors="coerce")
        total = int(vals.dropna().sum()) if not vals.dropna().empty else 0
        summary = html.Div(
            f"Suma udziału DRZEW: {total}",
            style={"fontSize": "10px", "fontWeight": "bold", "padding": "4px 2px",
                   "color": "#2d6a2d" if total == 10 else "#b00020"},
        )
        return html.Div([summary, table])

    return table

# ── Layout ────────────────────────────────────────────────────────────────────

PANEL = {
    "background": "#fff",
    "boxShadow": "0 1px 4px rgba(0,0,0,0.15)",
    "borderRadius": "6px",
    "display": "flex",
    "flexDirection": "column",
    "overflow": "hidden",
}
HDR = {
    "padding": "6px 10px", "background": "#2d6a2d", "color": "#fff",
    "fontWeight": "bold", "fontSize": "12px", "flexShrink": "0",
}


def ph(t):
    return html.Div(t, style=HDR)


def left_panel():
    return html.Div([
        ph("Nadleśnictwo"),
        dcc.Dropdown(id="nadl-dropdown", options=[], placeholder="Wybierz…", clearable=True,
                     style={"borderRadius": "0", "fontSize": "13px", "flexShrink": "0"}),
        ph("Osuwiska"),
        dcc.Checklist(
            id="landslide-filters",
            options=[
                {"label": "aktywne", "value": "active"},
                {"label": "monitoring", "value": "monitoring"},
                {"label": ">=50% las", "value": "forest50"},
            ],
            value=[],
            inputStyle={"marginRight": "4px"},
            labelStyle={"display": "block", "fontSize": "11px", "lineHeight": "16px"},
            style={"padding": "5px 8px", "borderBottom": "1px solid #e8e8e8", "flexShrink": "0"},
        ),
        html.Div(id="landslide-list",
                 style={"overflowY": "auto", "flex": "1", "padding": "2px 0"}),
    ], style={**PANEL, "width": "200px", "minWidth": "200px", "maxWidth": "200px"})


def map_panel():
    base = [
        dl.BaseLayer(
            dl.TileLayer(url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
                         attribution="© OpenStreetMap", maxZoom=19),
            name="OpenStreetMap", checked=True,
        ),
        dl.BaseLayer(
            dl.TileLayer(url="https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}",
                         attribution="© Google", maxZoom=21),
            name="Google Satellite",
        ),
        dl.BaseLayer(
            dl.WMSTileLayer(
                url="https://mapy.geoportal.gov.pl/wss/service/PZGIK/NMT/GRID1/WMS/ShadedRelief",
                layers="Raster", format="image/png", transparent=False,
                attribution="© GUGiK ISOK",
            ),
            name="ISOK Cieniowanie",
        ),
        dl.BaseLayer(
            dl.TileLayer(
                url="https://server.arcgisonline.com/ArcGIS/rest/services/Elevation/World_Hillshade/MapServer/tile/{z}/{y}/{x}",
                attribution="Tiles © Esri",
            ),
            name="Esri World Hillshade",
        ),
    ]
    overlays = [
        dl.Overlay(
            dl.GeoJSON(id="geojson-inspectorate", data=None,
                       options={"style": {"color": "#1a6b1a", "weight": 2.5, "fillOpacity": 0}}),
            name="Granica nadleśnictwa", checked=True,
        ),
        dl.Overlay(
            dl.GeoJSON(id="geojson-landslides", data=None,
                       options={"style": {"color": "#cc0000", "weight": 1.5,
                                          "fillOpacity": 0}}),
            name="Osuwiska", checked=True,
        ),
        dl.Overlay(
            dl.GeoJSON(id="geojson-subareas", data=None,
                       interactive=True,
                       bubblingMouseEvents=True,
                       style=_SUBAREA_STYLE_FN,
                       onEachFeature=_SUBAREA_ONEACH_FN),
            name="Wydzielenia (wypełnienie)", checked=True,
        ),
        dl.Overlay(
            dl.GeoJSON(id="geojson-subarea-border", data=None,
                       interactive=False,
                       options={"style": {"fillOpacity": 0, "color": "#FFD700", "weight": 1.5}}),
            name="Wydzielenia (granice)", checked=False,
        ),
    ]
    return html.Div([
        dl.Map(
            id="main-map",
            center=[50.5, 20.0],
            zoom=7,
            children=[
                dl.LayersControl(base + overlays, position="topright"),
                dl.ScaleControl(position="bottomleft"),
                dl.GeoJSON(
                    id="geojson-zoom",
                    data=None,
                    zoomToBounds=True,
                    interactive=False,
                    options={"style": {"opacity": 0, "fillOpacity": 0}},
                ),
            ],
            style={"height": "100%", "width": "100%"},
        ),
    ], style={**PANEL, "flex": "1", "minWidth": "0"})


def right_panel():
    e_attrs  = html.Div("Wybierz osuwisko.",
                        style={"color": "#888", "fontStyle": "italic", "padding": "8px"})
    e_storey = html.Div("Kliknij wydzielenie na mapie.",
                        style={"color": "#888", "fontStyle": "italic", "padding": "8px"})
    return html.Div([
        ph("Wydzielenie leśne"),
        html.Div(id="subarea-attrs", children=e_attrs,
                 style={"padding": "6px 8px", "overflowY": "auto", "flex": "1",
                        "borderBottom": "1px solid #e0e0e0"}),
        ph("Gatunki drzew (piętra)"),
        html.Div(id="storey-table", children=e_storey,
                 style={"padding": "4px 6px", "overflowY": "auto", "flex": "1"}),
    ], style={**PANEL, "width": "320px", "minWidth": "280px", "maxWidth": "360px"})


app = dash.Dash(__name__, title="Portal Osuwisk", suppress_callback_exceptions=True)

app.layout = html.Div([
    html.Div("Portal Osuwisk — Lasy Państwowe", style={
        "background": "#1a4a1a", "color": "#fff", "padding": "6px 16px",
        "fontSize": "15px", "fontWeight": "bold", "flexShrink": "0",
    }),
    html.Div([
        left_panel(),
        map_panel(),
        right_panel(),
    ], style={
        "display": "flex", "flex": "1", "gap": "6px",
        "padding": "6px", "minHeight": "0", "background": "#f0f2f0",
    }),
    dcc.Store(id="store-nadl-data"),
    dcc.Store(id="store-landslide"),
    dcc.Store(id="store-subarea"),
    dcc.Store(id="store-click-debug"),
], style={
    "display": "flex", "flexDirection": "column", "height": "100vh",
    "fontFamily": "'Segoe UI', Arial, sans-serif",
    "margin": "0", "boxSizing": "border-box",
})

# ── Callbacks ─────────────────────────────────────────────────────────────────

app.clientside_callback(
    """
    function(subareaClickData, mapClickData) {
        const ctx = dash_clientside.callback_context;
        const changed = ctx.triggered.map(t => t.prop_id);
        if (subareaClickData) {
            console.log("[dash subarea clickData]", subareaClickData);
        }
        if (mapClickData) {
            console.log("[dash map clickData]", mapClickData);
        }
        return {
            changed: changed,
            subareaClickData: subareaClickData,
            mapClickData: mapClickData,
            ts: Date.now()
        };
    }
    """,
    Output("store-click-debug", "data"),
    Input("geojson-subareas", "clickData"),
    Input("main-map", "clickData"),
    prevent_initial_call=True,
)


@app.callback(
    Output("nadl-dropdown", "options"),
    Input("nadl-dropdown", "id"),
)
def load_options(_):
    try:
        return get_nadl_options()
    except Exception as e:
        print(f"[load_options] {e}")
        return []


@app.callback(
    Output("landslide-list", "children"),
    Output("geojson-inspectorate", "data"),
    Output("geojson-landslides", "data"),
    Output("store-nadl-data", "data"),
    Output("geojson-zoom", "data", allow_duplicate=True),
    Output("geojson-subareas", "data", allow_duplicate=True),
    Output("geojson-subarea-border", "data", allow_duplicate=True),
    Output("store-landslide", "data", allow_duplicate=True),
    Output("subarea-attrs", "children", allow_duplicate=True),
    Output("storey-table", "children", allow_duplicate=True),
    Output("store-subarea", "data", allow_duplicate=True),
    Input("nadl-dropdown", "value"),
    Input("landslide-filters", "value"),
    prevent_initial_call=True,
)
def on_nadl_selected(nadl_name, filters):
    e_attrs = html.Div("Wybierz osuwisko.",
                       style={"color": "#888", "fontStyle": "italic", "padding": "8px"})
    e_storey = html.Div("Kliknij wydzielenie na mapie.",
                        style={"color": "#888", "fontStyle": "italic", "padding": "8px"})
    none11 = ([], None, empty_feature_collection(), None, no_update,
              None, None, None, e_attrs, e_storey, None)
    if not nadl_name:
        return none11
    try:
        insp  = get_inspectorate_geojson(nadl_name)
        df    = get_landslides(nadl_name)
        nadl_bbox = get_nadl_bbox(nadl_name)
    except Exception as e:
        print(f"[on_nadl_selected] {e}")
        return none11

    df = filter_landslides_df(df, filters)
    zoom_data = bbox_to_geojson_rect(*nadl_bbox, padding=0.05) if nadl_bbox else no_update

    if df.empty:
        msg = html.Div("Brak osuwisk dla wybranych filtrów.",
                       style={"color": "#888", "padding": "8px", "fontStyle": "italic"})
        store = {"nadl_name": nadl_name, "gids": [], "bboxes": []}
        return (msg, insp, empty_feature_collection(), store, zoom_data,
                None, None, None, e_attrs, e_storey, None)

    items  = [make_landslide_item(i, row) for i, (_, row) in enumerate(df.iterrows())]
    bboxes = [
        [float(row["ymin"]), float(row["xmin"]), float(row["ymax"]), float(row["xmax"])]
        for _, row in df.iterrows()
    ]
    store  = {"nadl_name": nadl_name, "gids": df["gid"].tolist(), "bboxes": bboxes}
    l_json = landslides_to_geojson(df)

    return (items, insp, l_json, store, zoom_data,
            None, None, None, e_attrs, e_storey, None)


@app.callback(
    Output("geojson-subareas", "data"),
    Output("geojson-subarea-border", "data"),
    Output("store-landslide", "data"),
    Output("geojson-zoom", "data", allow_duplicate=True),
    Output("subarea-attrs", "children", allow_duplicate=True),
    Output("storey-table", "children", allow_duplicate=True),
    Input({"type": "landslide-item", "index": ALL}, "n_clicks"),
    State("store-nadl-data", "data"),
    prevent_initial_call=True,
)
def on_landslide_clicked(n_clicks_list, nadl_data):
    nu = no_update
    if not ctx.triggered or not nadl_data:
        return nu, nu, nu, nu, nu, nu
    if not any(n_clicks_list):
        return nu, nu, nu, nu, nu, nu
    if ctx.triggered[0].get("value", 0) == 0:
        return nu, nu, nu, nu, nu, nu

    triggered = ctx.triggered[0]["prop_id"]
    m = re.search(r'"index"\s*:\s*(\d+)', triggered)
    if not m:
        return nu, nu, nu, nu, nu, nu

    idx    = int(m.group(1))
    gids   = nadl_data.get("gids", [])
    bboxes = nadl_data.get("bboxes", [])
    if idx >= len(gids):
        return nu, nu, nu, nu, nu, nu

    gid       = gids[idx]
    nadl_name = nadl_data["nadl_name"]

    zoom_data = bbox_to_geojson_rect(*bboxes[idx], padding=0.2) if idx < len(bboxes) else nu

    try:
        sub_df = get_subareas(gid, nadl_name)
    except Exception as e:
        print(f"[on_landslide_clicked] {e}")
        return nu, nu, None, zoom_data, nu, nu

    e_attrs = html.Div("Kliknij wydzielenie na mapie.",
                       style={"color": "#888", "fontStyle": "italic"})

    if sub_df.empty:
        no_sub = html.Div("Brak wydzieleń leśnych na tym osuwisku.",
                          style={"color": "#888", "fontStyle": "italic"})
        return None, None, {"gid": gid}, zoom_data, no_sub, html.Div()

    fill_data   = subareas_to_geojson(sub_df)
    border_data = subareas_to_border_geojson(sub_df)
    return fill_data, border_data, {"gid": gid, "nadl_name": nadl_name}, zoom_data, e_attrs, html.Div()


@app.callback(
    Output("geojson-subareas", "data", allow_duplicate=True),
    Output("geojson-subarea-border", "data", allow_duplicate=True),
    Output("store-landslide", "data", allow_duplicate=True),
    Output("geojson-zoom", "data", allow_duplicate=True),
    Output("subarea-attrs", "children", allow_duplicate=True),
    Output("storey-table", "children", allow_duplicate=True),
    Output("store-subarea", "data"),
    Input("geojson-subareas", "clickData"),
    Input("main-map", "clickData"),
    State("store-nadl-data", "data"),
    State("store-landslide", "data"),
    prevent_initial_call=True,
)
def on_subarea_clicked(subarea_click_data, map_click_data, nadl_data, landslide_data):
    nu = no_update
    triggered = ctx.triggered_id
    click_data = subarea_click_data if triggered == "geojson-subareas" else map_click_data
    if not click_data:
        print(f"[on_subarea_clicked] empty click_data triggered={triggered}", flush=True)
        return nu, nu, nu, nu, nu, nu, nu

    nadl_name = ((landslide_data or {}).get("nadl_name")
                 or (nadl_data or {}).get("nadl_name"))
    if not nadl_name:
        print(f"[on_subarea_clicked] missing nadl_name triggered={triggered} click_data={click_data}", flush=True)
        return nu, nu, nu, nu, nu, nu, nu

    latlng = latlng_from_click_data(click_data)
    current_gid = (landslide_data or {}).get("gid")
    try:
        current_gid = int(current_gid) if current_gid is not None else None
    except Exception:
        current_gid = None

    try:
        print(f"[on_subarea_clicked] triggered={triggered} click_data={click_data}", flush=True)
        props = click_data.get("properties") or {}
        a_i_num = props.get("a_i_num")
        if a_i_num is not None:
            sub_df = get_subarea_by_a_i_num(a_i_num, nadl_name)
            print(f"[on_subarea_clicked] lookup by a_i_num={a_i_num} rows={len(sub_df)}", flush=True)
        else:
            if not latlng:
                print(f"[on_subarea_clicked] missing latlng click_data={click_data}", flush=True)
                return nu, nu, nu, nu, nu, nu, nu
            lat, lng = latlng
            if current_gid is None:
                sub_df = pd.DataFrame()
                print(
                    f"[on_subarea_clicked] no active landslide; skip subarea lookup lat={lat} lng={lng}",
                    flush=True,
                )
            else:
                sub_df = get_subarea_at_point_for_landslide(lat, lng, current_gid, nadl_name)
                print(
                    f"[on_subarea_clicked] lookup visible subarea lat={lat} lng={lng} "
                    f"gid={current_gid} rows={len(sub_df)}",
                    flush=True,
                )
    except Exception as e:
        print(f"[on_subarea_clicked] {e}")
        return nu, nu, nu, nu, nu, nu, nu

    if sub_df.empty:
        print(f"[on_subarea_clicked] no subarea found nadl={nadl_name}", flush=True)
        if latlng:
            lat, lng = latlng
            try:
                landslide_df = get_landslide_at_point(lat, lng, nadl_name)
            except Exception as e:
                print(f"[on_subarea_clicked landslide lookup] {e}", flush=True)
                landslide_df = pd.DataFrame()

            if not landslide_df.empty:
                landslide = landslide_df.iloc[0]
                gid = int(landslide["gid"])

                print(
                    f"[on_subarea_clicked] landslide under click gid={gid} current_gid={current_gid}",
                    flush=True,
                )
                if gid != current_gid:
                    try:
                        new_sub_df = get_subareas(gid, nadl_name)
                    except Exception as e:
                        print(f"[on_subarea_clicked load landslide] {e}", flush=True)
                        return nu, nu, nu, nu, nu, nu, nu

                    zoom_data = bbox_to_geojson_rect(
                        float(landslide["ymin"]),
                        float(landslide["xmin"]),
                        float(landslide["ymax"]),
                        float(landslide["xmax"]),
                        padding=0.2,
                    )
                    store_landslide = {"gid": gid, "nadl_name": nadl_name}

                    if new_sub_df.empty:
                        msg = html.Div("Kliknięte osuwisko nie ma wydzieleń leśnych.",
                                       style={"color": "#888", "fontStyle": "italic", "padding": "6px"})
                        return None, None, store_landslide, zoom_data, msg, html.Div(), None

                    fill_data = subareas_to_geojson(new_sub_df)
                    border_data = subareas_to_border_geojson(new_sub_df)
                    msg = html.Div("Kliknij wydzielenie na mapie.",
                                   style={"color": "#888", "fontStyle": "italic", "padding": "6px"})
                    return fill_data, border_data, store_landslide, zoom_data, msg, html.Div(), None

        msg = html.Div("Brak wydzielenia w tym miejscu.",
                       style={"color": "#888", "fontStyle": "italic", "padding": "6px"})
        return nu, nu, nu, nu, msg, html.Div(), None

    row     = sub_df.iloc[0]
    a_i_num = row.get("a_i_num")
    print(f"[on_subarea_clicked] selected a_i_num={a_i_num}", flush=True)

    try:
        storey_df = get_storey_species(a_i_num)
    except Exception as e:
        print(f"[on_subarea_clicked storey] {e}")
        storey_df = pd.DataFrame()

    attrs  = make_subarea_attrs(row)
    storey = make_storey_table(storey_df)
    return nu, nu, nu, nu, attrs, storey, {"a_i_num": str(a_i_num) if a_i_num is not None else None}


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8999,
        debug=True,
        dev_tools_hot_reload=True,
        dev_tools_hot_reload_interval=1000,
        dev_tools_hot_reload_watch_interval=500,
    )
