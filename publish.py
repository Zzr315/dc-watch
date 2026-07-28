# -*- coding: utf-8 -*-
"""Commit the day's data and rebuilt page, then push to GitHub Pages.

Includes a hard safety gate: the staged file list is checked against patterns
for third-party copyrighted material and credentials before anything is
committed. The repository is public, so a .gitignore miss must not be able to
turn into a push.

    python publish.py            # add, verify, commit, push
    python publish.py --dry-run  # show what would be committed
"""
import argparse, datetime as dt, os, re, subprocess, sys

TRACKED = ["data", "docs", "state/commentary.jsonl"]

# Anything matching these must never reach a public remote. This list is the
# second line of defence: .gitignore stops the accident, this stops the
# `git add -f` and the pattern that .gitignore was missing. Keep the two in
# sync — a file type added to one and not the other is a hole.
FORBIDDEN = [
    (r"\.(pdf|xlsx?|xlsm|docx?|pptx?)$", "third-party research document"),
    (r"摩根大通", "J.P. Morgan report (no-redistribution clause)"),
    (r"天风", "Tianfeng Securities research workbook"),
    (r"(^|/)jpm\.txt$", "extracted JPM report text"),
    (r"(^|/)pg\d\d\.png$", "JPM page render"),
    (r"_zoom\.png$", "JPM figure crop"),
    (r"(^|/)mlcc_ref\.png$", "screenshot of another project's page"),
    (r"微信图片", "pasted chat image of third-party content"),
    (r"(^|/)\.env$", "credentials"),
    (r"(^|/)shot_\d*\.png$", "local screenshot"),
    (r"(^|/)run\.log$", "local run log"),
]


def git(*args, **kw):
    # core.quotepath=false keeps non-ASCII paths as UTF-8 instead of returning
    # them octal-escaped and wrapped in quotes ("\346\221\251...pdf"), which
    # would defeat both the Chinese-name and the trailing-".pdf" patterns below.
    r = subprocess.run(["git", "-c", "core.quotepath=false"] + list(args),
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **kw)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def unquote(p):
    """git still quotes paths containing spaces or specials; strip the wrapper
    so the anchored patterns match the real extension."""
    p = p.strip()
    if len(p) >= 2 and p[0] == '"' and p[-1] == '"':
        p = p[1:-1]
    return p


def ensure_repo():
    rc, out, _ = git("rev-parse", "--is-inside-work-tree")
    if rc != 0 or out != "true":
        raise SystemExit(
            "not a git repository.\n"
            "  one-time setup:\n"
            "    git init -b main\n"
            "    git remote add origin https://github.com/<you>/dc-watch.git\n"
            "  then in the repo's Settings -> Pages, set Source = main branch, /docs folder.")


def staged_files():
    rc, out, _ = git("diff", "--cached", "--name-only")
    return [unquote(x) for x in out.splitlines() if x.strip()]


def check_safe(files):
    """Belt and braces behind .gitignore. Matches on the raw path and on the
    basename, so a forbidden file cannot slip through on path shape alone."""
    bad = []
    for f in files:
        base = f.rsplit("/", 1)[-1]
        for pat, why in FORBIDDEN:
            if re.search(pat, f, re.IGNORECASE) or re.search(pat, base, re.IGNORECASE):
                bad.append((f, why))
                break
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--message", default=None)
    a = ap.parse_args()
    ensure_repo()

    existing = [p for p in TRACKED if os.path.exists(p)]
    if not existing:
        print("nothing to publish yet")
        return 0
    git("add", "--", *existing)

    files = staged_files()
    if not files:
        print("no changes to commit (data unchanged since last run)")
        return 0

    bad = check_safe(files)
    if bad:
        git("reset")            # unstage everything, publish nothing
        print("REFUSING TO PUBLISH - these files must never go to a public repo:")
        for f, why in bad:
            print("  %-56s %s" % (f, why))
        print("\nstaging area has been reset. Fix .gitignore, then re-run.")
        return 2

    print("staged %d file(s):" % len(files))
    for f in files[:12]:
        print("  " + f)
    if len(files) > 12:
        print("  ... and %d more" % (len(files) - 12))

    if a.dry_run:
        git("reset")
        print("\n--dry-run: nothing committed, staging reset")
        return 0

    msg = a.message or ("data: %s" % dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    rc, out, err = git("commit", "-m", msg)
    if rc != 0:
        print("commit failed:\n" + (err or out))
        return 1
    print("committed: %s" % msg)

    rc, out, err = git("push")
    if rc != 0:
        # A failed push is not fatal — the commit is safe locally and the next
        # run will carry it. Surface the cause rather than silently succeeding.
        print("push FAILED (commit is saved locally, will go out next run):")
        print("  " + (err or out).splitlines()[-1] if (err or out) else "  unknown error")
        return 1
    rc, url, _ = git("remote", "get-url", "origin")
    print("pushed to %s" % (url or "origin"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
