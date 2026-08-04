"""
Collecte des retards TGV INOUI - TOUTES les gares du trajet si retard détecté
================================================================================

Principe (en 3 fichiers) :

  1. data/arrets_en_cours.csv (écrasé à chaque run)
     -> les arrêts encore À VENIR pour chaque TGV INOUI actif.

  2. data/historique_temp.csv (écrasé à chaque run, invisible pour vous normalement)
     -> au fur et à mesure qu'un arrêt est "passé" (disparaît des arrêts à venir),
        il est ajouté ici, pour TOUS les trains encore en circulation, qu'il y ait
        du retard ou non. C'est la mémoire complète du trajet en cours de constitution.

  3. data/retards_par_gare_YYYY-MM-DD.csv (jamais écrasé, s'enrichit au fil du jour)
     -> UNE FOIS qu'un train a totalement disparu du flux (trajet terminé), on
        regarde tout son historique : s'il y a eu au moins un retard sur UNE
        gare, alors TOUTES les gares de son trajet sont écrites ici. Sinon,
        rien n'est écrit pour ce train (trajet 100% à l'heure = pas intéressant).

Dépendances : requests, gtfs-realtime-bindings, pandas
"""

import datetime
import io
import os
import zipfile
from zoneinfo import ZoneInfo

import requests
import pandas as pd

GTFS_STATIC_URL = "https://eu.ftp.opendatasoft.com/sncf/plandata/Export_OpenData_SNCF_GTFS_NewTripId.zip"
GTFS_RT_TRIP_UPDATES_URL = "https://proxy.transport.data.gouv.fr/resource/sncf-gtfs-rt-trip-updates"

CACHE_DIR = ".gtfs_cache"
STOPS_CACHE_FILE = os.path.join(CACHE_DIR, "stops.txt")
TRIPS_CACHE_FILE = os.path.join(CACHE_DIR, "trips.txt")
ROUTES_CACHE_FILE = os.path.join(CACHE_DIR, "routes.txt")

DATA_DIR = "data"
STATE_FILE = os.path.join(DATA_DIR, "arrets_en_cours.csv")
HISTORIQUE_FILE = os.path.join(DATA_DIR, "historique_temp.csv")

# Fenêtre de silence (heure locale Paris) : pas de collecte entre ces heures
SILENCE_START_HOUR = 1
SILENCE_END_HOUR = 4

# Mot-clé identifiant les TGV INOUI dans les stop_id (gardé pour référence/diagnostic)
INOUI_KEYWORD = "INOUI"

# Gares surveillées : un train n'est retenu que s'il passe par AU MOINS UNE
# de ces gares (identifiées par leur stop_id exact du flux GTFS-RT).
GARES_SUIVIES = {
    "StopPoint:OCETGV INOUI-87581009",  # Bordeaux
    "StopPoint:OCETGV INOUI-87391003",  # Paris Montparnasse
    "StopPoint:OCETGV INOUI-87393702",  # Massy
    "StopPoint:OCETGV INOUI-87575001",  # Poitiers
    "StopPoint:OCETGV INOUI-87583005",  # Angoulême
    "StopPoint:OCETGV INOUI-87481002",  # Nantes
    "StopPoint:OCETGV INOUI-87471003",  # Rennes
    "StopPoint:OCETGV INOUI-87396002",  # Le Mans
    "StopPoint:OCETGV INOUI-87484006",  # Angers
    "StopPoint:OCETGV INOUI-87481788",  # Le Croisic
    "StopPoint:OCETGV INOUI-87486449",  # Les Sables d'Olonne
    "StopPoint:OCETGV INOUI-87396408",  # Sablé-sur-Sarthe
    "StopPoint:OCETGV INOUI-87481192",  # Ancenis
    "StopArea:OCE87437798",             # La Rochelle
}

# Seuil de retard en secondes : un TRAIN entier n'est publié que si AU MOINS
# UN de ses arrêts dépasse ce seuil (arrivée ou départ). 0 = tout retard, même 1 sec.
DELAY_THRESHOLD_SEC = 0

PARIS_TZ = ZoneInfo("Europe/Paris")

COLONNES_SORTIE = [
    "trip_id",
    "numero_train",
    "destination",
    "stop_sequence",
    "gare",
    "arrival_time",
    "arrival_delay_sec",
    "departure_time",
    "departure_delay_sec",
    "derniere_maj",
]


def is_in_silence_window(now_paris: datetime.datetime) -> bool:
    return SILENCE_START_HOUR <= now_paris.hour < SILENCE_END_HOUR


def ensure_static_gtfs():
    os.makedirs(CACHE_DIR, exist_ok=True)
    if all(os.path.exists(p) for p in (STOPS_CACHE_FILE, TRIPS_CACHE_FILE, ROUTES_CACHE_FILE)):
        return

    print("Téléchargement du GTFS statique SNCF (mis en cache)...")
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


def fmt_time(epoch):
    if not epoch:
        return ""
    return datetime.datetime.fromtimestamp(epoch, tz=PARIS_TZ).strftime("%Y-%m-%d %H:%M:%S")


def load_static_data():
    trips_df = pd.read_csv(TRIPS_CACHE_FILE, dtype=str).add_prefix("trip_")
    routes_df = pd.read_csv(ROUTES_CACHE_FILE, dtype=str).add_prefix("route_")
    stops_df = pd.read_csv(STOPS_CACHE_FILE, dtype=str).add_prefix("stop_")

    trips_df = trips_df.merge(
        routes_df, left_on="trip_route_id", right_on="route_route_id", how="left"
    )
    return trips_df, stops_df


def fetch_current_stops(trips_df: pd.DataFrame, stops_df: pd.DataFrame) -> pd.DataFrame:
    """Retourne une ligne par (train, arrêt à venir) pour tous les TGV INOUI actifs."""
    from google.transit import gtfs_realtime_pb2

    print(f"Téléchargement du flux temps réel : {GTFS_RT_TRIP_UPDATES_URL}")
    resp = requests.get(GTFS_RT_TRIP_UPDATES_URL, timeout=30)
    resp.raise_for_status()

    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(resp.content)

    now_ts = datetime.datetime.now(tz=PARIS_TZ).strftime("%Y-%m-%d %H:%M:%S")

    all_rows = []
    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue

        trip_update = entity.trip_update
        trip = trip_update.trip

        for stu in trip_update.stop_time_update:
            arr_delay = stu.arrival.delay if stu.HasField("arrival") and stu.arrival.HasField("delay") else None
            dep_delay = stu.departure.delay if stu.HasField("departure") and stu.departure.HasField("delay") else None
            arr_time = stu.arrival.time if stu.HasField("arrival") and stu.arrival.HasField("time") else None
            dep_time = stu.departure.time if stu.HasField("departure") and stu.departure.HasField("time") else None

            all_rows.append({
                "trip_id": trip.trip_id,
                "stop_sequence": stu.stop_sequence if stu.HasField("stop_sequence") else -1,
                "stop_id_rt": stu.stop_id,
                "arrival_time": fmt_time(arr_time),
                "arrival_delay_sec": arr_delay,
                "departure_time": fmt_time(dep_time),
                "departure_delay_sec": dep_delay,
            })

    if not all_rows:
        return pd.DataFrame()

    rt_df = pd.DataFrame(all_rows)

    # Ne garder QUE les arrêts dans les gares surveillées (pas les gares
    # intermédiaires non listées, même si elles font partie du même trajet).
    rt_df = rt_df[rt_df["stop_id_rt"].isin(GARES_SUIVIES)]

    if rt_df.empty:
        return pd.DataFrame()

    full_df = rt_df.merge(trips_df, left_on="trip_id", right_on="trip_trip_id", how="left")
    full_df = full_df.merge(stops_df, left_on="stop_id_rt", right_on="stop_stop_id", how="left")

    full_df["numero_train"] = full_df.get("trip_trip_short_name", "")
    full_df["destination"] = full_df.get("trip_trip_headsign", "")
    full_df["gare"] = full_df.get("stop_stop_name", "")
    full_df["derniere_maj"] = now_ts

    full_df = full_df.reindex(columns=COLONNES_SORTIE)

    return full_df


def load_csv_or_empty(path, sep=";"):
    if os.path.exists(path):
        return pd.read_csv(path, sep=sep, dtype=str)
    return pd.DataFrame(columns=COLONNES_SORTIE)


def main():
    now_paris = datetime.datetime.now(tz=PARIS_TZ)

    if is_in_silence_window(now_paris):
        print(f"Heure actuelle Paris {now_paris.strftime('%H:%M')} : fenêtre de silence, on ne fait rien.")
        return

    ensure_static_gtfs()
    trips_df, stops_df = load_static_data()
    current_stops = fetch_current_stops(trips_df, stops_df)

    os.makedirs(DATA_DIR, exist_ok=True)

    previous_stops = load_csv_or_empty(STATE_FILE)
    historique_df = load_csv_or_empty(HISTORIQUE_FILE)

    def make_keys(df):
        if df.empty:
            return set()
        return set(zip(df["trip_id"], df["stop_sequence"].astype(str)))

    current_keys = make_keys(current_stops)
    previous_keys = make_keys(previous_stops)
    keys_passes = previous_keys - current_keys

    print(f"{len(current_keys)} arrêt(s) à venir actuellement, pour des TGV INOUI actifs.")
    print(f"{len(keys_passes)} arrêt(s) détecté(s) comme passés depuis la dernière collecte.")

    # --- Étape 1 : ajouter les arrêts qui viennent d'être passés à l'historique temporaire ---
    if keys_passes and not previous_stops.empty:
        previous_stops["_cle"] = list(zip(previous_stops["trip_id"], previous_stops["stop_sequence"].astype(str)))
        nouveaux_passes = previous_stops[previous_stops["_cle"].isin(keys_passes)].drop(columns=["_cle"]).copy()
        historique_df = pd.concat([historique_df, nouveaux_passes], ignore_index=True)

    # --- Étape 2 : détecter les trains entièrement terminés (plus aucun arrêt à venir) ---
    current_trip_ids = set(current_stops["trip_id"]) if not current_stops.empty else set()
    historique_trip_ids = set(historique_df["trip_id"]) if not historique_df.empty else set()
    trips_termines = historique_trip_ids - current_trip_ids

    print(f"{len(trips_termines)} train(s) entièrement terminé(s) à traiter.")

    if trips_termines:
        lignes_par_date = {}  # date_str (première gare du trajet) -> liste de DataFrames

        for trip_id in trips_termines:
            trajet_df = historique_df[historique_df["trip_id"] == trip_id].sort_values("stop_sequence")

            arr_num = pd.to_numeric(trajet_df["arrival_delay_sec"], errors="coerce")
            dep_num = pd.to_numeric(trajet_df["departure_delay_sec"], errors="coerce")
            a_du_retard_qqpart = ((arr_num.abs() > DELAY_THRESHOLD_SEC) | (dep_num.abs() > DELAY_THRESHOLD_SEC)).any()

            if not a_du_retard_qqpart:
                continue

            # Déterminer la date de la TOUTE PREMIÈRE gare du trajet (départ, ou arrivée à défaut)
            premiere_ligne = trajet_df.iloc[0]
            date_str = None
            for champ in ("departure_time", "arrival_time"):
                valeur = premiere_ligne.get(champ, "")
                if isinstance(valeur, str) and len(valeur) >= 10:
                    date_str = valeur[:10]
                    break
            if date_str is None:
                date_str = now_paris.strftime("%Y-%m-%d")  # secours si aucune heure connue

            lignes_par_date.setdefault(date_str, []).append(trajet_df)

        # Retirer tous les trains terminés de l'historique temporaire (traités, qu'ils soient publiés ou non)
        historique_df = historique_df[~historique_df["trip_id"].isin(trips_termines)]

        if lignes_par_date:
            for date_str, groupes in lignes_par_date.items():
                finaux_filepath = os.path.join(DATA_DIR, f"retards_par_gare_{date_str}.csv")

                a_publier_df = pd.concat(groupes, ignore_index=True)
                a_publier_df["heure_detection_fin_trajet"] = now_paris.strftime("%Y-%m-%d %H:%M:%S")
                a_publier_df = a_publier_df.sort_values(["trip_id", "stop_sequence"])

                # --- Conversion des retards de secondes en minutes (arrondi) pour le fichier final ---
                arr_sec = pd.to_numeric(a_publier_df["arrival_delay_sec"], errors="coerce")
                dep_sec = pd.to_numeric(a_publier_df["departure_delay_sec"], errors="coerce")
                a_publier_df["arrival_delay_min"] = (arr_sec / 60).round().astype("Int64")
                a_publier_df["departure_delay_min"] = (dep_sec / 60).round().astype("Int64")
                a_publier_df = a_publier_df.drop(columns=["arrival_delay_sec", "departure_delay_sec"])
                # -------------------------------------------------------------------------------------

                if os.path.exists(finaux_filepath):
                    existing = pd.read_csv(finaux_filepath, sep=";", dtype=str)
                    combined = pd.concat([existing, a_publier_df], ignore_index=True)
                else:
                    combined = a_publier_df

                combined.to_csv(finaux_filepath, index=False, sep=";", encoding="utf-8")
                print(f"✅ {len(groupes)} train(s) en retard publiés ({len(a_publier_df)} gares) dans {finaux_filepath} (date de départ du trajet)")
        else:
            print("Tous les trains terminés étaient 100% à l'heure, rien à publier.")

    # --- Sauvegarder les états pour le prochain run ---
    if not current_stops.empty:
        current_stops.to_csv(STATE_FILE, index=False, sep=";", encoding="utf-8")
    else:
        pd.DataFrame(columns=COLONNES_SORTIE).to_csv(STATE_FILE, index=False, sep=";", encoding="utf-8")

    if not historique_df.empty:
        historique_df.to_csv(HISTORIQUE_FILE, index=False, sep=";", encoding="utf-8")
    else:
        pd.DataFrame(columns=COLONNES_SORTIE).to_csv(HISTORIQUE_FILE, index=False, sep=";", encoding="utf-8")


if __name__ == "__main__":
    main()
