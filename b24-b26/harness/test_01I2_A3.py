"""01-I2-A3 ciblé : acceptation (verrou course, puis offre acceptée, puis UPDATE des autres offres -> 'lost')
vs sweeper multi-lignes (UPDATE offers ... -> 'expired', sans verrou course), sur la MÊME course,
les 7 offres concurrentes expirent pendant que l'acceptation (offre encore valide) s'exécute. Le sweeper n'existe peut-être pas dans NAATAL+ : scénario d'exploration."""
import threading, time, random
from lib import *
from test_01HI import SWEEP_SQL

log = Log("01-I2-A3")
agg = {}
reset()
for i in range(300):
    ride = 9800 + i
    offers = seed(ride, list(range(1, 9)), expires_in="0.30 seconds")
    b = threading.Barrier(2)
    d = random.randint(1, 8)
    with conn() as c:   # l'offre du chauffeur qui accepte reste valide ; les 7 autres expirent a t+0.30 s
        c.execute("update offers set expires_at = expires_at + interval '5 s' where id=%s", (offers[d],))
    def acc():
        b.wait(); time.sleep(0.30 + random.uniform(-0.004, 0.004))
        return run_sql("select accept_v_lock(%s,%s,%s,'statement','share')", (offers[d], d, f"A3-{ride}"), app="accept")
    def sw():
        b.wait(); time.sleep(0.30 + random.uniform(-0.004, 0.004))
        return run_sql(SWEEP_SQL + " ", app="sweeper")
    ra, rs = concurrent([acc, sw])
    k = f"accept={ra['result'] or ra['sqlstate']+' '+ra['error'][:28]} | sweeper={rs['result'] or rs['sqlstate'] or '0 ligne'}"
    agg[k] = agg.get(k, 0) + 1
for k, v in sorted(agg.items(), key=lambda x: -x[1]):
    log.w(f"  {v:4d} x {k}")
log.j("violations", violations())
with conn() as c:
    log.j("accepted_after_expiry", c.execute("select count(*) from offers where status='accepted' and decided_at > expires_at").fetchone())
    log.j("pg_stat_database.deadlocks (cumul base)", c.execute("select deadlocks from pg_stat_database where datname='b2426'").fetchone())
