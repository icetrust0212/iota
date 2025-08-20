cp .env src/miner/.env
cd src/miner
uv sync
# uv run --package miner main.py
pm2 start "uv run --package miner main.py" --name miner
pm2 start "uv run --package poller src/miner/launch_poller.py" --name poller
