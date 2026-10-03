"""01-H (cohérence offres gagnantes/perdantes sous charge mixte) et 01-I2 (verrous / deadlocks)."""
import threading, time, random
from lib import *

SWEEP_SQL = ("update offers set status='expired', decided_at=clock_timestamp() "
             "where status='pending' and expires_at <= clock_timestamp() returning 'SWEPT'")


def offer_checks():
    """Invariants 01-H complémentaires à la vue invariant_violations."""
    q = {
        "lost_offer_on_non_assigned_ride":
            "select count(*) from offers o join rides r on r.id=o.ride_id where o.status='lost' and r.status<>'assigned'",
        "accepted_after_expiry":
            "select count(*) from offers where status='accepted' and decided_at > expires_at",
        "pending_offer_of_old_generation":
            "select count(*) from offers o join rides r on r.id=o.ride_id "
            "where o.status='pending' and o.dispatch_generation<>r.dispatch_generation",
        "dispatching_ride_with_accepted_offer":
            "select count(*) from offers o join rides r on r.id=o.ride_id where o.status='accepted' and r.status='dispatching'",
    }
    with conn() as c:
        out = {k: c.execute(v).fetchone()[0] for k, v in q.items()}
        out["offers_by_status"] = dict(c.execute("select status, count(*) from offers group by 1").fetchall())
        out["rides_by_status"] = dict(c.execute("select status, count(*) from rides group by 1").fetchall())
    return out


def workload(log, n_rides, n_drivers, duration, with_sweeper, redispatch_order, accept_clock,
             retry=False, label=""):
    reset()
    ride_ids = list(range(9000, 9000 + n_rides))
    for r in ride_ids:
        seed(r, list(range(1, n_drivers + 1)), expires_in=f"{random.uniform(0.2, 3):.2f} seconds")
    stop = time.time() + duration
    stats, lock = {}, threading.Lock()
    retries = {"n": 0, "gave_up": 0}

    def rec(r):
        k = r["result"] if r["sqlstate"] is None else f'{r["sqlstate"]} {r.get("error","")[:45]}'
        with lock:
            stats[k] = stats.get(k, 0) + 1

    def with_retry(fn):
        for attempt in range(5):
            r = fn()
            if r["sqlstate"] in ("40P01", "40001") and retry:
                rec(r)
                with lock:
                    retries["n"] += 1
                time.sleep(random.uniform(0.005, 0.03) * (2 ** attempt))
                continue
            return r
        with lock:
            retries["gave_up"] += 1
        return r

    def accept_worker():
        while time.time() < stop:
            ride, d = random.choice(ride_ids), random.randint(1, n_drivers + 2)
            with conn() as c:
                row = c.execute("select id from offers where ride_id=%s and driver_id=%s order by id desc limit 1",
                                (ride, d)).fetchone()
            if not row:
                continue
            rec(with_retry(lambda: run_sql("select accept_v_lock(%s,%s,%s,%s,'share')",
                                           (row[0], d, f"H-{random.random()}", accept_clock), app="accept")))

    def redispatch_worker():
        while time.time() < stop:
            ride = random.choice(ride_ids)
            rec(with_retry(lambda: run_sql("select redispatch(%s, array[%s,%s], %s)",
                                           (ride, n_drivers + 1, n_drivers + 2, redispatch_order), app="redispatch")))
            time.sleep(0.01)

    def disable_worker():
        while time.time() < stop:
            d = random.randint(1, n_drivers + 2)
            rec(with_retry(lambda: run_sql("select disable_flag(%s)", (d,), app="disable")))
            with conn() as c:
                c.execute("update drivers set eligible=true where id=%s", (d,))
            time.sleep(0.05)

    def sweeper():
        while time.time() < stop:
            rec(with_retry(lambda: run_sql(SWEEP_SQL, app="sweeper")))
            time.sleep(0.02)

    with conn() as c:
        for d in (n_drivers + 1, n_drivers + 2):
            c.execute("insert into drivers(id) values (%s) on conflict do nothing", (d,))
    workers = [accept_worker] * 12 + [redispatch_worker] * 3 + [disable_worker] * 1 + ([sweeper] * 2 if with_sweeper else [])
    concurrent(workers)
    log.w(f"\n### {label}")
    log.w(f"  paramètres : {n_rides} courses, {n_drivers} chauffeurs, {duration}s, sweeper={with_sweeper}, "
          f"ordre redispatch={redispatch_order}, horloge acceptation={accept_clock}, retry={retry}")
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        log.w(f"  {v:6d} x {k}")
    if retry:
        log.j("  retries", retries)
    v = violations()
    log.j("  violations vue invariant_violations (nb)", len(v))
    if v:
        log.j("  exemples", v[:10])
    log.j("  contrôles 01-H complémentaires", offer_checks())
    return stats, v


def test_H(log):
    log.w("## 01-H : charge mixte concurrente (acceptations, redispatch, désactivations, expirations par sweeper)")
    workload(log, 30, 6, 20, True, "ride_first", "clock", retry=True, label="01-H charge mixte, horloge clock_timestamp()")
    workload(log, 30, 6, 20, True, "ride_first", "now", retry=True, label="01-H charge mixte, horloge now()")


def deadlock_generic(log):
    log.w("\n## 01-I2-B1 : deux transactions prennent course et chauffeur dans des ordres opposés")
    reset()
    seed(9500, [1])
    b = threading.Barrier(2)

    def t1():
        c = conn(autocommit=False, app="T1 ride->driver")
        t0 = time.monotonic()
        try:
            c.execute("update rides set version=version+1 where id=9500")
            b.wait(); time.sleep(0.1)
            c.execute("update drivers set eligible=eligible where id=1")
            c.commit(); return {"app": "T1", "result": "COMMIT", "ms": round((time.monotonic()-t0)*1000)}
        except psycopg.Error as e:
            c.rollback(); return {"app": "T1", "sqlstate": e.sqlstate, "error": e.diag.message_primary,
                                  "detail": e.diag.message_detail, "ms": round((time.monotonic()-t0)*1000)}

    def t2():
        c = conn(autocommit=False, app="T2 driver->ride")
        t0 = time.monotonic()
        try:
            c.execute("update drivers set eligible=eligible where id=1")
            b.wait(); time.sleep(0.1)
            c.execute("update rides set version=version+1 where id=9500")
            c.commit(); return {"app": "T2", "result": "COMMIT", "ms": round((time.monotonic()-t0)*1000)}
        except psycopg.Error as e:
            c.rollback(); return {"app": "T2", "sqlstate": e.sqlstate, "error": e.diag.message_primary,
                                  "detail": e.diag.message_detail, "ms": round((time.monotonic()-t0)*1000)}
    for r in concurrent([t1, t2]):
        log.j("  ", r)
    with conn() as c:
        log.j("  version finale course (1 seule des 2 incrémentations attendue)", c.execute("select version from rides where id=9500").fetchone())
        log.j("  pg_stat_database.deadlocks", c.execute("select deadlocks from pg_stat_database where datname='b2426'").fetchone())


def deadlock_accept_vs_redispatch(log, iters=100):
    log.w(f"\n## 01-I2-B2 : accept_v_lock (course -> offres) vs redispatch 'offers_first' (offres -> course), {iters} itérations")
    reset()
    with conn() as c:
        for d in (3, 4):
            c.execute("insert into drivers(id) values (%s)", (d,))
    agg = {}
    for i in range(iters):
        ride = 9600 + i
        offers = seed(ride, [1, 2])
        b = threading.Barrier(2)
        ra, rr = concurrent([
            lambda: run_sql("select accept_v_lock(%s,1,%s,'clock','share')", (offers[1], f"I-{ride}"), app="accept", barrier=b),
            lambda: run_sql("select redispatch(%s, array[3,4], 'offers_first')", (ride,), app="redispatch", barrier=b),
        ])
        k = (f"accept={ra['result'] or ra['sqlstate']+' '+ra['error'][:30]} | "
             f"redispatch={rr['result'] or rr['sqlstate']+' '+rr['error'][:30]}")
        agg[k] = agg.get(k, 0) + 1
    for k, v in sorted(agg.items(), key=lambda x: -x[1]):
        log.w(f"  {v:4d} x {k}")
    log.j("  violations", violations())
    with conn() as c:
        log.j("  pg_stat_database.deadlocks (cumul)", c.execute("select deadlocks from pg_stat_database where datname='b2426'").fetchone())


if __name__ == "__main__":
    import sys
    which = sys.argv[1] if len(sys.argv) > 1 else "HI"
    if "H" in which:
        test_H(Log("01-H"))
    if "I" in which:
        log = Log("01-I2")
        log.w("## 01-I2-A : chemin normal (ordre course -> offres -> chauffeur), sans retry, sans sweeper")
        workload(log, 20, 6, 20, False, "ride_first", "clock", retry=False, label="A1 chemin normal sans sweeper")
        log.w("\n## 01-I2-A2 : chemin normal + sweeper d'expiration (met à jour des offres sans verrouiller la course)")
        workload(log, 20, 6, 20, True, "ride_first", "clock", retry=False, label="A2 chemin normal + sweeper, sans retry")
        deadlock_generic(log)
        deadlock_accept_vs_redispatch(log)
        log.w("\n## 01-I2-B3 : récupération — même charge conflictuelle (redispatch offers_first + sweeper) AVEC retry 40P01/40001 (5 tentatives, backoff exponentiel)")
        workload(log, 20, 6, 20, True, "offers_first", "clock", retry=True, label="B3 conflictuel + retry")
