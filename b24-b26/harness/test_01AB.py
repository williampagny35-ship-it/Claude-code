"""01-A (2 chauffeurs simultanés) et 01-B (forte concurrence)."""
import threading, sys
from lib import *

VARIANTS = [
    ("v_lock/RC",           "select accept_v_lock(%s,%s,%s)",  "read committed"),
    ("v_cond/RC",           "select accept_v_cond(%s,%s,%s)",  "read committed"),
    ("v_naive/RC (témoin)", "select accept_v_naive(%s,%s,%s)", "read committed"),
    ("v_naive/SERIALIZABLE","select accept_v_naive(%s,%s,%s)", "serializable"),
]


def scenario(log, name, n_drivers, iterations, ride_base):
    log.w(f"\n## {name} : {n_drivers} chauffeurs x {iterations} itérations par variante")
    for label, sql, iso in VARIANTS:
        reset()
        all_res, winners_hist = [], {}
        for it in range(iterations):
            ride = ride_base + it
            drivers = list(range(1, n_drivers + 1))
            offers = seed(ride, drivers)
            b = threading.Barrier(n_drivers)
            jobs = [(lambda d=d: run_sql(sql, (offers[d], d, f"req-{ride}-{d}"), iso,
                                         app=f"drv{d}", barrier=b)) for d in drivers]
            res = concurrent(jobs)
            all_res += res
            w = sum(1 for r in res if r["result"] == "ACCEPTED")
            winners_hist[w] = winners_hist.get(w, 0) + 1
        viol = violations()
        log.w(f"\n### {label} (isolation={iso})")
        log.j("réponses agrégées", summarize(all_res))
        log.j("histogramme {nb_gagnants_par_course: nb_courses}", winners_hist)
        log.j("violations invariants (ride_id, type) [20 premières]", viol[:20])
        log.w(f"nb violations: {len(viol)}")
        log.j("exemple état final course", snapshot(ride_base))
        if viol:
            log.j("état course en violation", snapshot(viol[0][0]))


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "AB"
    if "A" in which:
        scenario(Log("01-A"), "01-A", 2, 200, 1000)
    if "B" in which:
        scenario(Log("01-B"), "01-B", 50, 20, 5000)
