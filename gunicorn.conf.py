import os

# Read PORT in Python so we never depend on shell variable expansion, which is what
# crashed the Railway deploy ("'$PORT' is not a valid port number"). Works across the
# Dockerfile builder, the Procfile/Nixpacks builder, Render, and local runs.
bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

# Single worker only: multiple workers = multiple background pollers = duplicate alerts.
workers = 1
threads = 8
