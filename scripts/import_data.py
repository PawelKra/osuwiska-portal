#!/usr/bin/env python3
"""
Skrypt importu danych do PostGIS.

Uruchomienie wewnątrz kontenera dash:
    docker compose exec dash python /scripts/import_data.py

Opcjonalnie można importować wybrane warstwy:
    docker compose exec dash python /scripts/import_data.py --only osuwiska
    docker compose exec dash python /scripts/import_data.py --only inspectorate
    docker compose exec dash python /scripts/import_data.py --only bdl
"""

import os
import sys
import zipfile
import tempfile
import argparse
from pathlib import Path

import geopandas as gpd
import pandas as pd
from sqlalchemy import create_engine, text

DANE_DIR = Path("/dane")

DB_URL = (
    "postgresql://{user}:{password}@{host}:{port}/{db}".format(
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        db=os.environ["DB_NAME"],
    )
)

# Mapowanie: nazwa nadleśnictwa → fragment nazwy w pliku ZIP
NADLESNICTWA_ZIP_MAP = {
    "Bielsko":            "BIELSKO",
    "Ustroń":             "USTRON",
    "Głogów Małopolski":  "GLOGOW",
    "Leżajsk":            "LEZAJSK",
    "Taczanów":           "TACZANOW",
    "Trzcianka":          "TRZCIANKA",
    "Rybnik":             "RYBNIK",
    "Węgierska Górka":    "WEGIERSKA_GORKA",
    "Gorlice":            "GORLICE",
    "Herby":              "HERBY",
    "Katrynka":           "KATRYNKA",
    "Milicz":             "MILICZ",
    "Pieńsk":             "PIENSK",
    "Supraśl":            "SUPRASL",
}


def find_zip(fragment: str) -> Path | None:
    for z in DANE_DIR.glob("BDL_*.zip"):
        stem_upper = z.stem.upper()
        # Fragment musi być otoczony separatorami (_, cyfry lub koniec) żeby
        # np. GLOGOW nie trafiło w ZLOTY_POTOK
        if f"_{fragment}_" in stem_upper or stem_upper.endswith(f"_{fragment}"):
            return z
    return None


def normalize_geometry(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Ujednolica nazwę kolumny geometrii do 'geometry'."""
    if gdf.geometry.name != "geometry":
        gdf = gdf.rename_geometry("geometry")
    return gdf


# --------------------------------------------------------------------------- #
#  1. osuwiska_pl                                                              #
# --------------------------------------------------------------------------- #

def import_osuwiska(engine) -> bool:
    print("\n=== [1/3] Importowanie osuwiska_pl ===")
    gpkg = DANE_DIR / "osuwiska_pl.gpkg"
    if not gpkg.exists():
        print(f"  BRAKUJE: {gpkg}")
        return False

    print("  Wczytywanie pliku GPKG...")
    import fiona
    available = fiona.listlayers(str(gpkg))
    layer_name = "osuwiska_pl" if "osuwiska_pl" in available else available[0]
    print(f"  Dostępne warstwy: {available} → używam: {layer_name}")
    gdf = gpd.read_file(gpkg, layer=layer_name)
    gdf = normalize_geometry(gdf)
    print(f"  Obiektów: {len(gdf)}, CRS: {gdf.crs}")

    print("  Zapis do PostGIS...")
    gdf.to_postgis("osuwiska_pl", engine, if_exists="replace", index=False)
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS osuwiska_pl_geom_idx "
            "ON osuwiska_pl USING GIST (geometry)"
        ))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS osuwiska_pl_gid_idx "
            "ON osuwiska_pl (gid)"
        ))
        conn.execute(text("ANALYZE osuwiska_pl"))
        conn.commit()
    print(f"  OK — zaimportowano {len(gdf)} obiektów do tabeli osuwiska_pl")
    return True


# --------------------------------------------------------------------------- #
#  2. g_inspectorate                                                           #
# --------------------------------------------------------------------------- #

def import_g_inspectorate(engine) -> bool:
    print("\n=== [2/3] Importowanie g_inspectorate ===")
    shp = DANE_DIR / "g_inspectorate.shp"
    if not shp.exists():
        print(f"  BRAKUJE: {shp}")
        return False

    print("  Wczytywanie shapefile...")
    gdf = gpd.read_file(shp)
    gdf = normalize_geometry(gdf)
    # Shapefile używa nazwy PUWG_92 — geopandas nie rozpoznaje EPSG; ustawiamy ręcznie
    if gdf.crs is None or gdf.crs.to_epsg() is None:
        gdf = gdf.set_crs(2180, allow_override=True)
        print("  CRS ustawiony ręcznie na EPSG:2180")
    print(f"  Obiektów: {len(gdf)}, CRS: {gdf.crs}")

    print("  Zapis do PostGIS...")
    gdf.to_postgis("g_inspectorate", engine, if_exists="replace", index=False)
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS g_inspectorate_geom_idx "
            "ON g_inspectorate USING GIST (geometry)"
        ))
        conn.execute(text("ANALYZE g_inspectorate"))
        conn.commit()
    print(f"  OK — zaimportowano {len(gdf)} obiektów do tabeli g_inspectorate")
    return True


# --------------------------------------------------------------------------- #
#  3. BDL: G_SUBAREA + f_storey_species                                        #
# --------------------------------------------------------------------------- #

def _read_storey_species(txt_path: Path) -> pd.DataFrame:
    df = pd.read_csv(
        txt_path,
        sep="\t",
        encoding="utf-8",
        dtype=str,
        na_values=[""],
        keep_default_na=False,
        encoding_errors="replace",
    )
    # Usuń białe znaki z wartości tekstowych
    for col in df.select_dtypes(include="object").columns:
        df[col] = df[col].str.strip()

    # Konwersja typów numerycznych
    int_cols = ["arodes_int_num", "a_year", "sp_rank_order_act",
                "species_age", "height", "bhd", "rotat_age"]
    float_cols = ["volume"]
    for col in int_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
    for col in float_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    return df


def import_bdl(engine) -> bool:
    print("\n=== [3/3] Importowanie warstw BDL ===")

    # Kasujemy i odtwarzamy tabele od zera (pełny reimport)
    with engine.connect() as conn:
        conn.execute(text("DROP TABLE IF EXISTS g_subarea CASCADE"))
        conn.execute(text("DROP TABLE IF EXISTS f_storey_species CASCADE"))
        conn.commit()

    first_subarea = True
    first_storey = True
    stats = {"ok": 0, "missing_zip": 0, "missing_shp": 0, "missing_txt": 0}

    for nadl_name, zip_fragment in NADLESNICTWA_ZIP_MAP.items():
        zip_path = find_zip(zip_fragment)

        if zip_path is None:
            print(f"  POMINIĘTO (brak ZIP): {nadl_name} [{zip_fragment}]")
            stats["missing_zip"] += 1
            continue

        print(f"\n  Przetwarzanie: {nadl_name}  ({zip_path.name})")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with zipfile.ZipFile(zip_path, "r") as zf:
                zf.extractall(tmp_path)

            # ---- G_SUBAREA ----
            shp_files = list(tmp_path.rglob("G_SUBAREA.shp"))
            if shp_files:
                gdf = gpd.read_file(shp_files[0])
                gdf = normalize_geometry(gdf)
                gdf["nadlesnictwo_name"] = nadl_name
                print(f"    G_SUBAREA: {len(gdf)} obiektów, CRS: {gdf.crs}")

                gdf.to_postgis(
                    "g_subarea",
                    engine,
                    if_exists="replace" if first_subarea else "append",
                    index=False,
                )
                first_subarea = False
            else:
                print(f"    BRAK G_SUBAREA.shp w {zip_path.name}")
                stats["missing_shp"] += 1

            # ---- f_storey_species ----
            txt_files = list(tmp_path.rglob("f_storey_species.txt"))
            if txt_files:
                df = _read_storey_species(txt_files[0])
                df["nadlesnictwo_name"] = nadl_name
                print(f"    f_storey_species: {len(df)} wierszy")

                df.to_sql(
                    "f_storey_species",
                    engine,
                    if_exists="replace" if first_storey else "append",
                    index=False,
                )
                first_storey = False
            else:
                print(f"    BRAK f_storey_species.txt w {zip_path.name}")
                stats["missing_txt"] += 1

        stats["ok"] += 1

    # Indeksy przestrzenne i relacyjne
    if not first_subarea:
        with engine.connect() as conn:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS g_subarea_geom_idx "
                "ON g_subarea USING GIST (geometry)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS g_subarea_a_i_num_idx "
                "ON g_subarea (a_i_num)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS g_subarea_nadl_idx "
                "ON g_subarea (nadlesnictwo_name)"
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS g_subarea_nadl_ai_idx "
                "ON g_subarea (nadlesnictwo_name, a_i_num)"
            ))
            conn.execute(text("ANALYZE g_subarea"))
            conn.commit()

    if not first_storey:
        with engine.connect() as conn:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS f_storey_species_arodes_idx "
                "ON f_storey_species (arodes_int_num)"
            ))
            conn.execute(text("ANALYZE f_storey_species"))
            conn.commit()

    print(f"\n  Podsumowanie BDL:")
    print(f"    Zaimportowane:     {stats['ok']}")
    print(f"    Brak ZIP:          {stats['missing_zip']}")
    print(f"    Brak G_SUBAREA:    {stats['missing_shp']}")
    print(f"    Brak storey_spec.: {stats['missing_txt']}")
    return True


# --------------------------------------------------------------------------- #
#  Punkt wejścia                                                               #
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description="Import danych do PostGIS")
    parser.add_argument(
        "--only",
        choices=["osuwiska", "inspectorate", "bdl"],
        help="Importuj tylko wybraną warstwę",
    )
    args = parser.parse_args()

    print("Łączenie z bazą danych...")
    engine = create_engine(DB_URL)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT PostGIS_Version()"))
        print("Połączono.\n")
    except Exception as exc:
        print(f"BŁĄD połączenia: {exc}")
        sys.exit(1)

    run_all = args.only is None

    if run_all or args.only == "osuwiska":
        import_osuwiska(engine)

    if run_all or args.only == "inspectorate":
        import_g_inspectorate(engine)

    if run_all or args.only == "bdl":
        import_bdl(engine)

    print("\n=== Import zakończony ===")


if __name__ == "__main__":
    main()
