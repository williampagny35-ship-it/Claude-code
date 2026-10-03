"""01-G2 : acceptation concurrente d'une modification de l'éligibilité du chauffeur.

Deux opérations d'éligibilité de maquette :
  disable_flag  : UPDATE drivers SET eligible=false
  disable_guard : refuse si le chauffeur a une course 'assigned', sinon passe eligible=false
Deux contrôles d'éligibilité côté acceptation :
  read  : SELECT simple de drivers (aucun verrou)
  share : SELECT ... FOR SHARE sur drivers
Les instants de COMMIT réels sont relevés avec track_commit_timestamp.
"""
import threading, time, random
from lib import *


def run_tx_capture_xid(sql, params, iso, app, barrier, delay):
    """Comme run_sql mais relève le txid pour retrouver l'horodatage de COMMIT."""
    out = {"app": app}
    c = conn(autocommit=False, app=app)
    try:
        c.execute(f"set transaction isolation level {iso}")
        barrier.wait()
        time.sleep(delay)
        t0 = time.monotonic()
        out["xid"] = c.execute("select txid_current()").fetchone()[0]
        out["result"] = c.execute(sql, params).fetchone()[0]
        c.commit()
        out["sqlstate"] = None
    except psycopg.Error as e:
        out["result"], out["sqlstate"], out["error"] = None, e.sqlstate, e.diag.message_primary
        c.rollback()
    out["elapsed_ms"] = round((time.monotonic() - t0) * 1000, 1)
    c.close()
    return out


def commit_ts(xid):
    with conn() as c:
        return c.execute("select pg_xact_commit_timestamp(((%s::bigint) %% 4294967296)::text::xid)", (xid,)).fetchone()[0]


def classify(ra, rd, disable_fn):
    with conn() as c:
        eligible = c.execute("select eligible from drivers where id=1").fetchone()[0]
        ride = c.execute("select status, driver_id from rides where id=8000").fetchone()
    ok_a, ok_d = ra["sqlstate"] is None, rd["sqlstate"] is None
    tag = f"accept={'OK' if ok_a else ra['sqlstate']+':'+ra['error'][:24]} | disable={'OK' if ok_d else rd['sqlstate']+':'+rd['error'][:24]}"
    anomaly = None
    if ok_a and ok_d:
        ca, cd = commit_ts(ra["xid"]), commit_ts(rd["xid"])
        order = "accept<disable" if ca < cd else "disable<accept"
        tag += f" | commits {order}"
        if disable_fn == "disable_guard":
            anomaly = "WRITE_SKEW: chauffeur inéligible ET course affectée, les deux opérations validées"
        elif order == "disable<accept":
            anomaly = "TEMPS_REEL: désactivation validée AVANT l'acceptation, acceptation quand même validée"
    tag += f" | final eligible={eligible} ride={ride[0]}"
    return tag, anomaly


def campaign(log, disable_fn, check, iso, iters, spread):
    log.w(f"\n### disable={disable_fn} | contrôle acceptation={check} | isolation={iso} | {iters} itérations | décalage aléatoire 0..{spread*1000:.0f} ms")
    outcomes, anomalies = {}, 0
    for i in range(iters):
        reset()
        offers = seed(8000, [1, 2])
        b = threading.Barrier(2)
        da, dd = random.random() * spread, random.random() * spread
        ra, rd = concurrent([
            lambda: run_tx_capture_xid("select accept_v_lock(%s,1,%s,'now',%s)", (offers[1], f"G-{i}", check), iso, "accept", b, da),
            lambda: run_tx_capture_xid(f"select {disable_fn}(1)", (), iso, "disable", b, dd),
        ])
        tag, anomaly = classify(ra, rd, disable_fn)
        if anomaly:
            anomalies += 1
            tag += f"  <== {anomaly}"
        outcomes[tag] = outcomes.get(tag, 0) + 1
    for k, v in sorted(outcomes.items(), key=lambda x: -x[1]):
        log.w(f"  {v:4d} x  {k}")
    log.w(f"  anomalies observées : {anomalies}/{iters}")


def forced(log, disable_fn, check, iso):
    """Entrelacement forcé : l'acceptation lit l'éligibilité, puis la désactivation est lancée
    et validée pendant que l'acceptation n'a pas encore fait COMMIT (fenêtre de 1 s)."""
    reset()
    offers = seed(8000, [1, 2])
    ca = conn(autocommit=False, app="accept")
    ca.execute(f"set transaction isolation level {iso}")
    xa = ca.execute("select txid_current()").fetchone()[0]
    ra = ca.execute("select accept_v_lock(%s,1,'G-forced','now',%s)", (offers[1], check)).fetchone()[0]
    res = {}

    def disabler():
        res["d"] = run_sql(f"select {disable_fn}(1)", (), iso, app="disable")
    t = threading.Thread(target=disabler)
    t.start()
    time.sleep(1.0)
    with conn() as c:
        waits = c.execute("select application_name, state, wait_event_type, wait_event from pg_stat_activity "
                          "where application_name in ('accept','disable')").fetchall()
    try:
        ca.commit()
        a_res = ra
    except psycopg.Error as e:
        a_res = f"{e.sqlstate} {e.diag.message_primary}"
    t.join()
    rd = res["d"]
    with conn() as c:
        fin = c.execute("select (select eligible from drivers where id=1), (select status from rides where id=8000)").fetchone()
    log.w(f"\n### FORCÉ disable={disable_fn} contrôle={check} isolation={iso}")
    log.j("  pg_stat_activity 1 s après lancement de la désactivation", waits)
    log.j("  acceptation", a_res)
    log.j("  désactivation", {k: rd.get(k) for k in ("result", "sqlstate", "error", "elapsed_ms")})
    log.j("  final (eligible, ride.status)", fin)


if __name__ == "__main__":
    log = Log("01-G2")
    log.w("## Entrelacements forcés")
    for dfn in ("disable_flag", "disable_guard"):
        for check in ("read", "share"):
            for iso in ("read committed", "repeatable read", "serializable"):
                forced(log, dfn, check, iso)
    log.w("\n## Campagnes à départ simultané (décalage aléatoire)")
    for dfn in ("disable_flag", "disable_guard"):
        for check in ("read", "share"):
            for iso in ("read committed", "serializable"):
                campaign(log, dfn, check, iso, 150, 0.004)
    log.j("violations (vue générique)", violations())
