# Scalingo starts the web process with this line.
# --no-access-log: do not write client IPs or URLs to stdout.
# IP stripping at the load balancer (Nginx/Caddy) should also be:
#   proxy_set_header X-Real-IP "";
#   proxy_set_header X-Forwarded-For "";
#   access_log off;
web: uvicorn app.main:app --host 0.0.0.0 --port $PORT --no-access-log
