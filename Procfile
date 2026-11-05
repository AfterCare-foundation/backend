# Scalingo starts the web process with this line.
# --no-access-log: the app writes no client IPs or URLs to stdout.
# The app also ignores X-Forwarded-For / X-Real-IP / Forwarded (strip_forwarded_client).
# Scalingo's own router terminates connections and sees client IPs. What it logs,
# and for how long, is a Scalingo setting and a data-processing-agreement question
# for the founders. Check it there; this file cannot change it.
web: uvicorn app.main:app --host 0.0.0.0 --port $PORT --no-access-log
