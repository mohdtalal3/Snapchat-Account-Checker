# Snapchat Account Checker

A PyQt6 GUI tool for checking Snapchat accounts in bulk.

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

Create a `.env` file in the project folder:

```env
# Proxy configuration (leave empty to disable)
# Format: http://user:pass@host:port
PROXY=

# Max concurrent browser threads
MAX_THREADS=2

# Max accounts to process per run (0 = all)
MAX_ACCOUNTS=0
```

### 3. Prepare accounts file

Create a `accounts.txt` file with one account per line:

```
email@example.com:password123
another@email.com:pass456
```

- Lines starting with `#` are skipped
- Format must be `email:password`

### 4. Run the app

```bash
python run.py
```

## Usage

1. Click **Upload File** and select your `accounts.txt`
2. Adjust threads and max accounts if needed
3. Click **Start**
4. Watch the log panel for real-time results
5. Results are saved per run in `runs/run_YYYYMMDD_HHMMSS/`:
   - `hits.csv` — successful logins
   - `non_hits.csv` — failed logins
   - `run.log` — full timestamped log
6. Processed accounts (HIT/NON-HIT) are automatically removed from `accounts.txt`
7. Accounts that error are retried up to 3 times and left in the file if still failing

## Notes

- Each run opens a fresh Chrome browser per account via SeleniumBase
- Proxy is used if set in `.env`
- Errored accounts are NOT added to `non_hits.csv` — they stay in `accounts.txt` for retry
