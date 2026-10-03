"""01-C (request_id différents, même opération) et 01-D (même request_id répété)."""
import threading, time
from lib import *


def counts(c, ride):
    return {
        "transitions": c.execute("select count(*) from transitions where ride_id=%s", (ride,)).fetchone()[0],
        "side_effects": c.execute("select count(*) from side_effects where ride_id=%s", (ride,)).fetchone()[0],
        "accepted_offers": c.execute("select count(*) from offers where ride_id=%s and status='accepted'", (ride,)).fetchone()[0],
    }


def test_C(log, iters=100):
    log.w(f"\n## 01-C : même chauffeur / même offre, 2 request_id différents, concurrent, accept_dedup_atomic, {iters} itérations")
    reset()
    allr, per_ride = [], {}
    for i in range(iters):
        ride = 2000 + i
        offers = seed(ride, [1, 2, 3])
        b = threading.Barrier(2)
        res = concurrent([
            lambda: run_sql("select accept_dedup_atomic(%s,%s,%s)", (offers[1], 1, f"C-{ride}-a"), app="tapA", barrier=b),
            lambda: run_sql("select accept_dedup_atomic(%s,%s,%s)", (offers[1], 1, f"C-{ride}-b"), app="tapB", barrier=b),
        ])
        allr += res
        with conn() as c:
            k = json.dumps(counts(c, ride))
        per_ride[k] = per_ride.get(k, 0) + 1
    log.j("réponses agrégées", summarize(allr))
    log.j("répartition {compteurs par course: nb courses}", per_ride)
    with conn() as c:
        log.j("request_dedup par statut", c.execute("select status, count(*) from request_dedup group by 1").fetchall())
    log.j("violations", violations())
    log.j("exemple état final", snapshot(2000))


def second_d3(b):
    b.wait()
    time.sleep(0.2)
    return run_sql("insert into request_dedup(request_id, operation, status) values ('D3','x','pending') returning 'INSERTED'",
                   app="second")


def test_D(log, iters=50, dup=10):
    log.w(f"\n## 01-D1 : même request_id envoyé {dup} fois en parallèle, accept_dedup_atomic, {iters} itérations")
    reset()
    allr, per_ride, max_ms = [], {}, 0
    for i in range(iters):
        ride = 3000 + i
        offers = seed(ride, [1, 2])
        b = threading.Barrier(dup)
        res = concurrent([(lambda: run_sql("select accept_dedup_atomic(%s,%s,%s)", (offers[1], 1, f"D-{ride}"),
                                           app="dup", barrier=b)) for _ in range(dup)])
        allr += res
        max_ms = max(max_ms, max(r["elapsed_ms"] or 0 for r in res))
        with conn() as c:
            k = json.dumps(counts(c, ride))
        per_ride[k] = per_ride.get(k, 0) + 1
    log.j("réponses agrégées", summarize(allr))
    log.j("répartition {compteurs par course: nb courses}", per_ride)
    log.w(f"durée max d'un appel (ms): {max_ms}")
    with conn() as c:
        log.j("request_dedup par statut", c.execute("select status, result, count(*) from request_dedup group by 1,2").fetchall())
    log.j("violations", violations())

    log.w("\n## 01-D2-seq : rejeu séquentiel du même request_id après commit (5 fois)")
    reset()
    offers = seed(3500, [1, 2])
    seq = [run_sql("select accept_dedup_atomic(%s,%s,%s)", (offers[1], 1, "D-seq"), app="seq") for _ in range(5)]
    log.j("réponses successives", [r["result"] or r["sqlstate"] for r in seq])
    log.j("état final", snapshot(3500))

    log.w("\n## 01-D3 : INSERT request_dedup SANS ON CONFLICT, 2 transactions, la 1re garde la transaction ouverte 1 s")
    reset()
    b = threading.Barrier(2)
    res = concurrent([
        lambda: run_sql("insert into request_dedup(request_id, operation, status) values ('D3','x','pending') returning 'INSERTED'",
                        app="first", barrier=b, post_sleep=1.0),
        lambda: second_d3(b),
    ])
    log.j("résultats", [{k: r[k] for k in ("app", "result", "sqlstate", "error", "elapsed_ms") if k in r} for r in res])

    log.w("\n## 01-D4 : 1re tentative refusée par le métier (offre expirée) puis rejeu du même request_id")
    reset()
    offers = seed(3600, [1], expires_in="-1 second")
    r1 = run_sql("select accept_dedup_atomic(%s,%s,%s)", (offers[1], 1, "D4"), app="try1")
    with conn() as c:
        c.execute("update offers set expires_at = clock_timestamp() + interval '30 s' where id=%s", (offers[1],))
    r2 = run_sql("select accept_dedup_atomic(%s,%s,%s)", (offers[1], 1, "D4"), app="try2")
    log.j("tentative 1", {k: r1.get(k) for k in ("result", "sqlstate", "error")})
    log.w("(entre les deux : l'offre est remise non expirée, pour voir si le rejeu ré-exécute le métier)")
    log.j("tentative 2", {k: r2.get(k) for k in ("result", "sqlstate", "error")})
    with conn() as c:
        log.j("request_dedup", c.execute("select request_id, status, result from request_dedup").fetchall())
    log.j("état final", snapshot(3600))


if __name__ == "__main__":
    test_C(Log("01-C"))
    test_D(Log("01-D"))
