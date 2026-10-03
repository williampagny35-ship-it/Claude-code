"""Utilitaires du harnais B24/B26 (exécution réelle sur PostgreSQL)."""
import json, os, threading, time, datetime
import psycopg
from psycopg import errors

DSN = os.environ.get("B2426_DSN", "host=localhost dbname=b2426 user=tester password=tester")
LOGDIR = os.path.join(os.path.dirname(__file__), "..", "logs")


def conn(autocommit=True, app="b2426"):
    c = psycopg.connect(DSN, autocommit=autocommit, application_name=app)
    c.execute("set search_path = fx")
    return c


class Log:
    def __init__(self, name):
        os.makedirs(LOGDIR, exist_ok=True)
        self.path = os.path.join(LOGDIR, f"{name}.log")
        self.f = open(self.path, "w")
        self.w(f"# {name} — exécuté {datetime.datetime.now().isoformat()} sur {server_version()}")

    def w(self, *a):
        line = " ".join(str(x) for x in a)
        print(line)
        self.f.write(line + "\n")
        self.f.flush()

    def j(self, label, obj):
        self.w(f"{label}: {json.dumps(obj, default=str, ensure_ascii=False)}")


def server_version():
    with conn() as c:
        return c.execute("select version()").fetchone()[0].split(" on ")[0]


def reset():
    with conn() as c:
        c.execute("""truncate transitions, side_effects, request_dedup, offers, rides, drivers
                     restart identity cascade""")


def seed(ride_id, driver_ids, expires_in="30 seconds", generation=1):
    """Crée des chauffeurs (si absents), une course 'dispatching' et une offre par chauffeur."""
    with conn() as c:
        for d in driver_ids:
            c.execute("insert into drivers(id) values (%s) on conflict do nothing", (d,))
        c.execute("insert into rides(id, dispatch_generation) values (%s, %s)", (ride_id, generation))
        offers = {}
        for d in driver_ids:
            oid = c.execute(
                "insert into offers(ride_id, driver_id, dispatch_generation, expires_at) "
                "values (%s,%s,%s, clock_timestamp() + %s::interval) returning id",
                (ride_id, d, generation, expires_in)).fetchone()[0]
            offers[d] = oid
        return offers


def snapshot(ride_id):
    with conn() as c:
        c.row_factory = psycopg.rows.dict_row
        return {
            "ride": c.execute("select * from rides where id=%s", (ride_id,)).fetchone(),
            "offers_by_status": {r["status"]: r["n"] for r in c.execute(
                "select status, count(*) n from offers where ride_id=%s group by 1", (ride_id,))},
            "accepted": c.execute(
                "select id, driver_id, dispatch_generation from offers where ride_id=%s and status='accepted'",
                (ride_id,)).fetchall(),
            "transitions": c.execute(
                "select from_status, to_status, driver_id, request_id from transitions where ride_id=%s order by id",
                (ride_id,)).fetchall(),
            "side_effects": c.execute(
                "select request_id, kind from side_effects where ride_id=%s order by id", (ride_id,)).fetchall(),
        }


def violations():
    with conn() as c:
        return c.execute("select ride_id, violation from invariant_violations order by 1").fetchall()


def run_sql(sql, params=(), isolation=None, app="worker", pre=None, barrier=None, post_sleep=0):
    """Exécute sql dans une transaction explicite. Retourne dict résultat/erreur/SQLSTATE/timings."""
    out = {"app": app}
    t0 = None
    c = conn(autocommit=False, app=app)
    try:
        if isolation:
            c.execute(f"set transaction isolation level {isolation}")
        if pre:
            for p in pre:
                c.execute(p)
        if barrier:
            barrier.wait()
        t0 = time.monotonic()
        out["t_start"] = time.time()
        row = c.execute(sql, params).fetchone()
        if post_sleep:
            time.sleep(post_sleep)
        c.commit()
        out["t_commit"] = time.time()
        out["result"] = row[0] if row else None
        out["sqlstate"] = None
    except psycopg.Error as e:
        out["t_err"] = time.time()
        out["result"] = None
        out["sqlstate"] = e.sqlstate
        out["error"] = (e.diag.message_primary or str(e)).strip()
        try:
            c.rollback()
        except Exception:
            pass
    finally:
        out["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1) if t0 else None
        c.close()
    return out


def concurrent(jobs):
    """jobs: liste de callables. Lance tout en parallèle, retourne les résultats dans l'ordre."""
    res = [None] * len(jobs)

    def wrap(i, f):
        res[i] = f()
    ths = [threading.Thread(target=wrap, args=(i, f)) for i, f in enumerate(jobs)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return res


def summarize(results):
    agg = {}
    for r in results:
        k = r["result"] if r["sqlstate"] is None else f'{r["sqlstate"]} {r.get("error","")[:40]}'
        agg[k] = agg.get(k, 0) + 1
    return agg
