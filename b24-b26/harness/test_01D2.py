"""01-D2 : crash après le métier, avant la finalisation de request_dedup.

Crashs RÉELS provoqués :
  * client (processus type Edge Function) tué par SIGKILL entre la tx métier et la tx de finalisation ;
  * serveur PostgreSQL arrêté en mode immediate (= crash, recovery WAL au redémarrage) ;
  * backend PostgreSQL tué par SIGKILL au milieu de la transaction (crash-restart du postmaster).
"""
import os, signal, subprocess, sys, time
from lib import *

CHILD = r'''
import sys, time
sys.path.insert(0, "%s")
from lib import conn
offer, req = int(sys.argv[1]), sys.argv[2]
c = conn()
print("BEGIN:", c.execute("select dedup_begin(%%s)", (req,)).fetchone()[0], flush=True)
print("BUSINESS:", c.execute("select accept_v_lock(%%s,1,%%s)", (offer, req)).fetchone()[0], flush=True)
print("READY_TO_FINISH", flush=True)
time.sleep(60)   # le parent tue le processus ici, avant dedup_finish
c.execute("select dedup_finish(%%s,'ACCEPTED')", (req,))
''' % os.path.dirname(os.path.abspath(__file__))


def pg(action):
    subprocess.run(["pg_ctlcluster", "16", "main"] + action, check=True, capture_output=True)


def wait_pg():
    for _ in range(60):
        try:
            with conn() as c:
                c.execute("select 1")
                return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("PostgreSQL n'est pas revenu")


def retry_policies(log, offer, req, ride):
    with conn() as c:
        log.j("request_dedup après crash", c.execute("select request_id, status, result from request_dedup").fetchall())
    log.j("état course après crash (avant retry)", snapshot(ride))
    r = run_sql("select dedup_begin(%s)", (req,), app="retry")
    log.j("retry -> dedup_begin", {k: r.get(k) for k in ("result", "sqlstate", "error")})
    log.w("Politique P1 : 'pending' = opération en cours -> le retry s'arrête là (rien d'autre n'est fait).")
    log.w("Politique P2 : 'pending' = ré-exécuter le métier :")
    r2 = run_sql("select accept_v_lock(%s,1,%s)", (offer, req), app="retry-P2")
    log.j("retry P2 -> accept_v_lock", {k: r2.get(k) for k in ("result", "sqlstate", "error")})
    log.j("état final", snapshot(ride))
    with conn() as c:
        log.j("request_dedup final", c.execute("select request_id, status, result from request_dedup").fetchall())
    log.j("violations", violations())


def scenario_client_kill(log):
    log.w("\n## 01-D2-a : stratégie 3 transactions, client tué (SIGKILL) entre tx métier et dedup_finish")
    reset()
    offers = seed(4000, [1, 2])
    p = subprocess.Popen([sys.executable, "-c", CHILD, str(offers[1]), "D2a"], stdout=subprocess.PIPE, text=True)
    for line in p.stdout:
        log.w("  child>", line.strip())
        if "READY_TO_FINISH" in line:
            break
    p.send_signal(signal.SIGKILL)
    p.wait()
    log.w(f"  child tué, code retour = {p.returncode}")
    time.sleep(0.5)
    retry_policies(log, offers[1], "D2a", 4000)


def scenario_server_crash_between(log):
    log.w("\n## 01-D2-b : stratégie 3 transactions, CRASH SERVEUR (pg stop -m immediate) entre tx métier et dedup_finish")
    reset()
    offers = seed(4100, [1, 2])
    c = conn()
    log.j("tx1 dedup_begin", c.execute("select dedup_begin('D2b')").fetchone()[0])
    log.j("tx2 accept_v_lock", c.execute("select accept_v_lock(%s,1,'D2b')", (offers[1],)).fetchone()[0])
    t = time.time()
    pg(["stop", "-m", "immediate"])
    log.w("  serveur arrêté en mode immediate (pas de checkpoint, recovery WAL au redémarrage)")
    try:
        c.execute("select dedup_finish('D2b','ACCEPTED')")
        log.w("  !! dedup_finish a réussi (inattendu)")
    except Exception as e:
        log.w(f"  dedup_finish échoue côté client : {type(e).__name__}: {str(e).strip()[:120]}")
    pg(["start"])
    wait_pg()
    log.w(f"  serveur redémarré en {time.time()-t:.1f}s")
    retry_policies(log, offers[1], "D2b", 4100)


def scenario_atomic_backend_kill(log):
    log.w("\n## 01-D2-c : stratégie atomique (dedup dans la tx métier), backend tué par SIGKILL AVANT le COMMIT")
    reset()
    offers = seed(4200, [1, 2])
    c = conn(autocommit=False, app="victim")
    pid = c.execute("select pg_backend_pid()").fetchone()[0]
    log.j("métier exécuté dans la tx (non validée)",
          c.execute("select accept_dedup_atomic(%s,1,'D2c')", (offers[1],)).fetchone()[0])
    os.kill(pid, signal.SIGKILL)
    log.w(f"  backend {pid} tué (SIGKILL) -> le postmaster réinitialise toutes les sessions (crash recovery)")
    try:
        c.commit()
        log.w("  !! COMMIT a réussi (inattendu)")
    except Exception as e:
        log.w(f"  COMMIT côté client : {type(e).__name__}: {str(e).strip()[:160]}")
    wait_pg()
    log.j("état après crash", snapshot(4200))
    with conn() as c2:
        log.j("request_dedup après crash", c2.execute("select * from request_dedup").fetchall())
    r = run_sql("select accept_dedup_atomic(%s,1,'D2c')", (offers[1],), app="retry")
    log.j("retry même request_id", {k: r.get(k) for k in ("result", "sqlstate", "error")})
    r = run_sql("select accept_dedup_atomic(%s,1,'D2c')", (offers[1],), app="retry2")
    log.j("2e retry même request_id", {k: r.get(k) for k in ("result", "sqlstate", "error")})
    log.j("état final", snapshot(4200))
    log.j("violations", violations())


def scenario_atomic_crash_after_commit(log):
    log.w("\n## 01-D2-d : stratégie atomique, COMMIT validé puis CRASH SERVEUR immédiat (réponse supposée perdue)")
    reset()
    offers = seed(4300, [1, 2])
    c = conn(autocommit=False)
    res = c.execute("select accept_dedup_atomic(%s,1,'D2d')", (offers[1],)).fetchone()[0]
    c.commit()
    pg(["stop", "-m", "immediate"])
    log.w(f"  métier={res}, COMMIT renvoyé, puis serveur arrêté en mode immediate")
    pg(["start"])
    wait_pg()
    r = run_sql("select accept_dedup_atomic(%s,1,'D2d')", (offers[1],), app="retry")
    log.j("retry même request_id après recovery", {k: r.get(k) for k in ("result", "sqlstate", "error")})
    log.j("état final", snapshot(4300))
    log.j("violations", violations())


if __name__ == "__main__":
    log = Log("01-D2")
    scenario_client_kill(log)
    scenario_server_crash_between(log)
    scenario_atomic_backend_kill(log)
    scenario_atomic_crash_after_commit(log)
