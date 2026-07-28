"""
Retards Trains SNCF - plusieurs gares, filtre TGV INOUI, export Excel
=======================================================================

Installation préalable :
    pip install requests gtfs-realtime-bindings pandas openpyxl

Usage :
    python retards_gares.py "Lyon Part Dieu" "Paris Gare de Lyon" "Marseille St Charles"
    python retards_gares.py "Lyon Part Dieu" --all-trains
    python retards_gares.py --stop-ids StopArea:OCE87547000 StopArea:OCE87686006

Par défaut, seuls les trains dont le nom de route (route_long_name /
route_short_name) contient "INOUI" sont conservés. Utilisez --all-trains
pour désactiver ce filtre et voir tous les trains (TER, Intercités, TGV, etc.).

Le résultat est affiché dans le terminal ET exporté dans un fichier
retards_YYYYMMDD_HHMM.xlsx dans le dossier courant.
"""

import argparse
import csv
import datetime
import io
import os
import sys
import zipfile

import requests
import pandas as pd

# --- Sources de données -----------------------------------------------------

GTFS_STATIC_URL = "https://eu.ftp.opendatasoft.com/sncf/gtfs/export_gtfs_voyages.zip"
GTFS_RT_TRIP_UPDATES_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates"

CACHE_DIR = ".gtfs_cache"
STOPS_CACHE_FILE = os.path.join(CACHE_DIR, "stops.txt")
TRIPS_CACHE_FILE = os.path.join(CACHE_DIR, "trips.txt")
ROUTES_CACHE_FILE = os.path.join(CACHE_DIR, "routes.txt")

# Mot-clé utilisé pour repérer les TGV INOUI dans routes.txt.
# Si le filtre ne remonte rien, lancez avec --all-trains pour voir les noms
# de route réels dans la colonne "route" du fichier Excel, et ajustez ce
# mot-clé si besoin (ex: "INOUI", "TGV INOUI", etc.)
INOUI_KEYWORD = "INOUI"


# --- Étape 1 : télécharger et mettre en cache le GTFS statique --------------

def ensure_static_gtfs():
    os.makedirs(CACHE_DIR, exist_ok=True)

    if all(os.path.exists(p) for p in (STOPS_CACHE_FILE, TRIPS_CACHE_FILE, ROUTES_CACHE_FILE)):
        return

    print(f"Téléchargement du GTFS statique depuis {GTFS_STATIC_URL} ...")
    resp = requests.get(GTFS_STATIC_URL, timeout=60)
    resp.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        for member, target in [
            ("stops.txt", STOPS_CACHE_FILE),
            ("trips.txt", TRIPS_CACHE_FILE),
            ("routes.txt", ROUTES_CACHE_FILE),
        ]:
            with z.open(member) as f_in, open(target, "wb") as f_out:
                f_out.write(f_in.read())

    print("GTFS statique mis en cache dans", CACHE_DIR)


# --- Étape 2 : trouver les stop_id pour une liste de noms de gares ----------

def find_stop_ids_for_station(station_name: str) -> set[str]:
    needle = station_name.strip().lower()
    stop_ids = set()

    with open(STOPS_CACHE_FILE, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if needle in row.get("stop_name", "").lower():
                if row.get("location_type", "0") == "0":
                    stop_ids.add(row["stop_id"])

    return stop_ids


def build_stop_id_to_station_name_map(all_stop_ids: set[str]) -> dict[str, str]:
    """Pour afficher un nom de gare lisible en face de chaque résultat."""
    mapping = {}
    with open(STOPS_CACHE_FILE, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["stop_id"] in all_stop_ids:
                mapping[row["stop_id"]] = row["stop_name"]
    return mapping


# --- Étape 3 : construire la table trip_id -> nom de route (pour filtrer INOUI) --

def build_trip_to_route_name_map() -> dict[str, str]:
    route_id_to_name = {}
    with open(ROUTES_CACHE_FILE, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("route_long_name") or row.get("route_short_name") or ""
            route_id_to_name[row["route_id"]] = name

    trip_to_route_name = {}
    with open(TRIPS_CACHE_FILE, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            route_name = route_id_to_name.get(row.get("route_id"), "")
            trip_to_route_name[row["trip_id"]] = route_name

    return trip_to_route_name


# --- Étape 4 : interroger le flux temps réel et filtrer ---------------------

def fetch_delays(stop_ids: set[str], trip_to_route_name: dict[str, str],
                  only_inoui: bool, station_names: dict[str, str]) -> list[dict]:
    from google.transit import gtfs_realtime_pb2

    print(f"\nTéléchargement du flux temps réel : {GTFS_RT_TRIP_UPDATES_URL}")
    resp = requests.get(GTFS_RT_TRIP_UPDATES_URL, timeout=30)
    resp.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)

    rows = []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue

        trip_update = entity.trip_update
        trip_id = trip_update.trip.trip_id
        route_name = trip_to_route_name.get(trip_id, "")

        if only_inoui and INOUI_KEYWORD.lower() not in route_name.lower():
            continue

        for stu in trip_update.stop_time_update:
            if stu.stop_id not in stop_ids:
                continue

            delay_arr = stu.arrival.delay if stu.HasField("arrival") else None
            delay_dep = stu.departure.delay if stu.HasField("departure") else None

            rows.append({
                "gare": station_names.get(stu.stop_id, stu.stop_id),
                "stop_id": stu.stop_id,
                "trip_id": trip_id,
                "route": route_name,
                "retard_arrivee_min": (delay_arr // 60) if delay_arr is not None else None,
                "retard_depart_min": (delay_dep // 60) if delay_dep is not None else None,
            })

    return rows


# --- Étape 5 : export Excel --------------------------------------------------

def export_to_excel(rows: list[dict]) -> str:
    df = pd.DataFrame(rows)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"retards_{timestamp}.xlsx"
    df.to_excel(filename, index=False)
    return filename


# --- Point d'entrée -----------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Retards de trains SNCF pour plusieurs gares")
    parser.add_argument("stations", nargs="*", help="Noms de gares, ex: \"Lyon Part Dieu\" \"Paris Gare de Lyon\"")
    parser.add_argument("--stop-ids", nargs="*", default=[], help="stop_id exacts à utiliser directement")
    parser.add_argument("--all-trains", action="store_true", help="Ne pas filtrer sur TGV INOUI, garder tous les trains")
    args = parser.parse_args()

    if not args.stations and not args.stop_ids:
        parser.print_help()
        sys.exit(1)

    ensure_static_gtfs()

    all_stop_ids = set(args.stop_ids)

    for station in args.stations:
        ids = find_stop_ids_for_station(station)
        if not ids:
            print(f"⚠️  Aucune gare trouvée pour '{station}', ignorée.")
            continue
        print(f"'{station}' -> {len(ids)} stop_id trouvé(s)")
        all_stop_ids |= ids

    if not all_stop_ids:
        sys.exit("Aucun stop_id valide au final, arrêt.")

    station_names = build_stop_id_to_station_name_map(all_stop_ids)
    trip_to_route_name = build_trip_to_route_name_map()

    rows = fetch_delays(
        stop_ids=all_stop_ids,
        trip_to_route_name=trip_to_route_name,
        only_inoui=not args.all_trains,
        station_names=station_names,
    )

    if not rows:
        print("\nAucun résultat. Si vous filtrez sur TGV INOUI, relancez avec --all-trains")
        print("pour vérifier les noms de route réels dans les données SNCF actuelles.")
        return

    print(f"\n{len(rows)} résultat(s) trouvé(s) :\n")
    for r in rows:
        print(
            f"  {r['gare']:<30} train={r['trip_id']:<35} route={r['route']:<15} "
            f"retard arr.={r['retard_arrivee_min']} min  retard dép.={r['retard_depart_min']} min"
        )

    filename = export_to_excel(rows)
    print(f"\n✅ Export Excel créé : {filename}")


if __name__ == "__main__":
    main()
