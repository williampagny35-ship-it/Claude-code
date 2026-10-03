"""01-E (acceptation vs redispatch) et 01-F (expiration réelle d'une offre)."""
import threading, time, random
from lib import *


def test_E(log, iters=200):
    for label, accept_sql in [("v_lock", "select accept_v_lock(%s,%s,%s)"),
                              ("v_cond", "select accept_v_cond(%s,%s,%s)")]:
        log.w(f"\n## 01-E [{label}] : acceptation (chauffeur 1) vs redispatch (-> chauffeurs 3,4), départ simultané, {iters} itérations, READ COMMITTED")
        reset()
        with conn() as c:
            for d in (3, 4):
                c.execute("insert into drivers(id) values (%s)", (d,))
        outcomes = {}
        for i in range(iters):
            ride = 6000 + i
            offers = seed(ride, [1, 2])
            b = threading.Barrier(2)
            ja, jr = random.random() * 0.004, random.random() * 0.004  # décalage aléatoire symétrique

            def do_accept():
                b.wait(); time.sleep(ja)
                return run_sql(accept_sql, (offers[1], 1, f"E-{ride}"), app="accept")

            def do_redispatch():
                b.wait(); time.sleep(jr)
                return run_sql("select redispatch(%s, array[3,4])", (ride,), app="redispatch")
            ra, rr = concurrent([do_accept, do_redispatch])
            s = snapshot(ride)
            key = (f"accept={ra['result'] or ra['sqlstate']+':'+ra['error'][:30]} | "
                   f"redispatch={rr['result'] or rr['sqlstate']+':'+rr['error'][:30]} | "
                   f"ride={s['ride']['status']} gen={s['ride']['dispatch_generation']} offres={s['offers_by_status']}")
            outcomes[key] = outcomes.get(key, 0) + 1
        for k, v in sorted(outcomes.items(), key=lambda x: -x[1]):
            log.w(f"  {v:4d} x  {k}")
        log.j("violations", violations())

    log.w("\n## 01-E-forcé : redispatch prend le verrou course et le garde 1 s ; l'acceptation arrive pendant ce temps")
    reset()
    with conn() as c:
        for d in (3, 4):
            c.execute("insert into drivers(id) values (%s)", (d,))
    offers = seed(6900, [1, 2])
    b = threading.Barrier(2)

    def late_accept():
        b.wait()
        time.sleep(0.2)
        return run_sql("select accept_v_lock(%s,1,'E-forced')", (offers[1],), app="accept")
    rr, ra = concurrent([
        lambda: run_sql("select redispatch(6900, array[3,4])", app="redispatch", barrier=b, post_sleep=1.0),
        late_accept])
    log.j("redispatch", {k: rr.get(k) for k in ("result", "sqlstate", "error", "elapsed_ms")})
    log.j("accept", {k: ra.get(k) for k in ("result", "sqlstate", "error", "elapsed_ms")})
    log.j("état final", snapshot(6900))
    log.j("violations", violations())


def offer_times(oid):
    with conn() as c:
        return c.execute("select status, expires_at, decided_at, decided_at > expires_at as decided_after_expiry "
                         "from offers where id=%s", (oid,)).fetchone()


def test_F(log):
    log.w("\n## 01-F1 : transaction explicite ouverte AVANT l'expiration, contrôle APRÈS l'expiration réelle")
    for clock in ("now", "statement", "clock"):
        reset()
        offers = seed(7000, [1], expires_in="2 seconds")
        c = conn(autocommit=False, app="F1")
        t = c.execute("select now(), statement_timestamp(), clock_timestamp(), "
                      "(select expires_at from offers where id=%s)", (offers[1],)).fetchone()
        time.sleep(3)
        t2 = c.execute("select now(), statement_timestamp(), clock_timestamp()").fetchone()
        try:
            r = c.execute("select accept_v_lock(%s,1,'F1',%s)", (offers[1], clock)).fetchone()[0]
            c.commit()
            err = None
        except psycopg.Error as e:
            r, err = None, f"{e.sqlstate} {e.diag.message_primary}"
            c.rollback()
        c.close()
        log.w(f"\n### horloge du contrôle = {clock}")
        log.j("début tx : now / statement / clock / expires_at", t)
        log.j("après 3 s : now / statement / clock", t2)
        log.j("résultat accept", r or err)
        log.j("offre (status, expires_at, decided_at, décidée après expiration ?)", offer_times(offers[1]))

    log.w("\n## 01-F2 : appel unique en autocommit (type RPC), démarré AVANT l'expiration, bloqué 3 s sur le verrou de la course, contrôle APRÈS l'expiration")
    for clock in ("now", "statement", "clock"):
        reset()
        offers = seed(7100, [1, 2], expires_in="1 second")
        b = threading.Barrier(2)

        def holder():
            # une autre transaction (ex. acceptation concurrente qui finira par échouer) tient la course 3 s puis ROLLBACK
            c = conn(autocommit=False, app="holder")
            c.execute("select 1 from rides where id=7100 for update")
            b.wait()
            time.sleep(3)
            c.rollback()
            c.close()
            return {"result": "HOLDER_ROLLED_BACK", "sqlstate": None}

        def acceptor():
            # vraie connexion autocommit : l'appel est sa propre transaction (comme un RPC PostgREST)
            b.wait()
            time.sleep(0.1)
            c = conn(autocommit=True, app="acceptor")
            t0 = time.monotonic()
            try:
                r = {"result": c.execute("select accept_v_lock(%s,1,'F2',%s)", (offers[1], clock)).fetchone()[0],
                     "sqlstate": None}
            except psycopg.Error as e:
                r = {"result": None, "sqlstate": e.sqlstate, "error": e.diag.message_primary}
            r["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
            c.close()
            return r
        _, ra = concurrent([holder, acceptor])
        log.w(f"\n### horloge du contrôle = {clock}")
        log.j("accept", {k: ra.get(k) for k in ("result", "sqlstate", "error", "elapsed_ms")})
        log.j("offre (status, expires_at, decided_at, décidée après expiration ?)", offer_times(offers[1]))
        log.j("violations", violations())


if __name__ == "__main__":
    test_E(Log("01-E"))
    test_F(Log("01-F"))
