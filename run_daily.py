# -*- coding: utf-8 -*-
"""Daily entry point. This is what Task Scheduler runs.

    python run_daily.py                 # full chain, publishes
    python run_daily.py --no-publish    # everything except the git push
    python run_daily.py --only site     # rebuild the page from existing data

Each step is isolated: a failure is recorded and the chain continues, so a
dead LLM relay or an unreachable TrendForce never costs you the day's token
collection. Exit code is non-zero if any step failed, so the scheduler's
"last run result" column stays meaningful.
"""
import argparse, datetime as dt, io, os, sys, traceback

HERE = os.path.dirname(os.path.abspath(__file__))
STEPS = ["collect", "analyze", "commentary", "site", "publish"]


def log(logf, msg):
    line = "%s  %s" % (dt.datetime.now().strftime("%H:%M:%S"), msg)
    print(line)
    logf.write(line + "\n")
    logf.flush()


def run_step(name, fn, logf, results):
    log(logf, "--- %s" % name)
    t0 = dt.datetime.now()
    try:
        fn()
        dur = (dt.datetime.now() - t0).total_seconds()
        results[name] = "ok"
        log(logf, "    %s ok (%.1fs)" % (name, dur))
    except Exception as e:
        results[name] = "FAILED: %s" % str(e)[:200]
        log(logf, "    %s FAILED: %s" % (name, e))
        logf.write(traceback.format_exc() + "\n")
        logf.flush()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(HERE, "data"))
    ap.add_argument("--state", default=os.path.join(HERE, "state"))
    ap.add_argument("--docs", default=os.path.join(HERE, "docs"))
    ap.add_argument("--window", default="week", choices=["day", "week", "month"])
    ap.add_argument("--perf-top", type=int, default=20,
                    help="how many top models to pull throughput for")
    ap.add_argument("--publish", action="store_true",
                    help="also commit and push (off by default — the dashboard is local)")
    ap.add_argument("--no-publish", action="store_true",
                    help="deprecated; publishing is already off unless --publish is given")
    ap.add_argument("--only", default=None, choices=STEPS,
                    help="run a single step")
    a = ap.parse_args()

    os.chdir(HERE)              # git and relative paths behave under Task Scheduler
    os.makedirs(a.state, exist_ok=True)
    os.makedirs(a.data, exist_ok=True)

    import tracker, analyze, build_site, shadeform, perf, equity, azure, replicas, marketshare
    tracker.load_env()

    briefp = os.path.join(a.state, "brief.json")
    commp = os.path.join(a.state, "commentary.jsonl")
    results = {}

    def step_collect():
        """Each collector is isolated. They were originally one try/except, and
        a single read timeout on OpenRouter's 530 KB model list took all eight
        down with it — a whole day's data lost to one transient stall. Now a
        failure is recorded and the rest still run; the step reports how many
        succeeded and only raises at the end."""
        subs = [
            ("tokens/day",   lambda: tracker.llm_tokens(a.data, "day")),
            ("tokens/week",  lambda: tracker.llm_tokens(a.data, "week")),
            ("market-share", lambda: marketshare.collect(a.data)),
            ("memory",       lambda: tracker.memory(a.data)),
            ("gpu/silicondata", lambda: tracker.gpu_rental(a.data)),
            ("gpu/neocloud", lambda: shadeform.collect(a.data)),
            ("gpu/hyperscaler", lambda: azure.collect(a.data)),
            ("perf",         lambda: perf.collect(a.data, top=a.perf_top)),
            ("replicas",     lambda: replicas.collect(a.data)),
            ("equity",       lambda: equity.collect(a.data)),
        ]
        if os.environ.get("OPENROUTER_API_KEY"):
            import apps
            subs.append(("apps",
                         lambda: apps.collect(a.data, os.environ["OPENROUTER_API_KEY"])))
        else:
            print("  - apps: no OPENROUTER_API_KEY, skipped "
                  "(run `python apps.py --preflight` after adding one)")

        failed_subs = []
        for name, fn in subs:
            try:
                fn()
            except Exception as e:
                failed_subs.append("%s (%s)" % (name, str(e)[:70]))
                print("  ! %s FAILED: %s" % (name, str(e)[:100]))
        if failed_subs:
            raise RuntimeError("%d/%d collectors failed: %s"
                               % (len(failed_subs), len(subs), "; ".join(failed_subs)))

    def step_analyze():
        sys.argv = ["analyze.py", "--data", a.data, "--out", briefp,
                    "--window", a.window]
        analyze.main()

    def step_commentary():
        import commentary
        sys.argv = ["commentary.py", "--brief", briefp, "--out", commp]
        rc = commentary.main()
        if rc:
            raise RuntimeError("commentary returned %s" % rc)

    def step_site():
        sys.argv = ["build_site.py", "--data", a.data, "--state", a.state,
                    "--out", a.docs, "--window", a.window]
        build_site.main()

    def step_publish():
        import publish
        sys.argv = ["publish.py"]
        rc = publish.main()
        if rc:
            raise RuntimeError("publish returned %s" % rc)

    plan = [("collect", step_collect), ("analyze", step_analyze),
            ("commentary", step_commentary), ("site", step_site)]
    if a.publish and not a.no_publish:
        plan.append(("publish", step_publish))
    if a.only:
        plan = [(n, f) for n, f in plan if n == a.only] or \
               [(a.only, {"publish": step_publish, "commentary": step_commentary}[a.only])]

    with io.open(os.path.join(a.state, "run.log"), "a", encoding="utf-8") as logf:
        logf.write("\n===== %s =====\n" % dt.datetime.now().isoformat(timespec="seconds"))
        log(logf, "Data Center Watch daily run")
        for name, fn in plan:
            run_step(name, fn, logf, results)

        failed = [k for k, v in results.items() if v != "ok"]
        log(logf, "done: %d ok, %d failed" % (len(results) - len(failed), len(failed)))
        for k in failed:
            log(logf, "   %s -> %s" % (k, results[k]))

    # The page is still worth publishing when only the commentary died — it
    # falls back to the previous view and labels itself stale.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
